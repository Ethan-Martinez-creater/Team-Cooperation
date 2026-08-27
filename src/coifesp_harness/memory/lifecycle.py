from __future__ import annotations

import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import (Boolean, CheckConstraint, Column, DateTime, ForeignKeyConstraint,
    MetaData, String, Table, Text, and_, delete, insert, select, text, update)
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..errors import MemoryConflictError, MemoryUnavailableError, PolicyDenied
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Principal
from .models import MemoryScope, MemoryStatus
from .repository import MEMORY_METADATA, MEMORY_RECORDS

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LIFECYCLE_METADATA = MEMORY_METADATA

MEMORY_LEGAL_HOLDS = Table(
    "memory_legal_holds", LIFECYCLE_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("hold_id", String(128), primary_key=True),
    Column("memory_id", String(64), nullable=False),
    Column("reason_digest", String(64), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("released_by", String(128), nullable=True),
    Column("released_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(["tenant_id", "memory_id"],
        ["memory_records.tenant_id", "memory_records.memory_id"], ondelete="CASCADE"),
    CheckConstraint("length(reason_digest)=64", name="reason_digest"),
    CheckConstraint("(active AND released_by IS NULL AND released_at IS NULL) OR (NOT active AND released_by IS NOT NULL AND released_at IS NOT NULL)", name="release"),
)

MEMORY_DELETION_REQUESTS = Table(
    "memory_deletion_requests", LIFECYCLE_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("request_id", String(128), primary_key=True),
    Column("memory_id", String(64), nullable=False),
    Column("requester_id", String(128), nullable=False),
    Column("reason_digest", String(64), nullable=False),
    Column("previous_status", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decision_reason_digest", String(64), nullable=True),
    Column("content_fingerprint", String(64), nullable=False),
    CheckConstraint("status IN ('pending','rejected','purged')", name="status"),
    CheckConstraint("previous_status IN ('active','quarantined')", name="previous_status"),
    CheckConstraint("length(reason_digest)=64 AND length(content_fingerprint)=64", name="digests"),
    CheckConstraint("decision_reason_digest IS NULL OR length(decision_reason_digest)=64", name="decision_digest"),
    CheckConstraint("(status='pending' AND decided_by IS NULL AND decided_at IS NULL) OR (status<>'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)", name="decision"),
)


@dataclass(frozen=True, slots=True)
class MemoryDeletionRecord:
    request_id: str
    memory_id: str
    status: str


class MemoryLifecycleService:
    def __init__(self, *, engine: Engine, audit_log: SQLAlchemyAuditLog) -> None:
        if audit_log.engine is not engine:
            raise ValueError("memory lifecycle and audit must share one engine")
        self.engine = engine
        self.audit_log = audit_log

    def create_schema(self) -> None:
        LIFECYCLE_METADATA.create_all(self.engine)

    def request_deletion(self, *, principal: Principal, request_id: str,
                         memory_id: str, expected_version: int, reason: str) -> MemoryDeletionRecord:
        self._identifier(request_id)
        digest = self._reason(reason)
        now = datetime.now(UTC)
        with self._transaction(principal.tenant_id) as connection:
            row = connection.execute(select(MEMORY_RECORDS).where(and_(
                MEMORY_RECORDS.c.tenant_id == principal.tenant_id,
                MEMORY_RECORDS.c.memory_id == memory_id)).with_for_update()).mappings().one_or_none()
            if row is None:
                raise MemoryUnavailableError("memory is not available")
            self._authorize_request(principal, row)
            existing = connection.execute(select(MEMORY_DELETION_REQUESTS).where(and_(
                MEMORY_DELETION_REQUESTS.c.tenant_id == principal.tenant_id,
                MEMORY_DELETION_REQUESTS.c.request_id == request_id))).mappings().one_or_none()
            if existing is not None:
                if existing["memory_id"] != memory_id or existing["reason_digest"] != digest:
                    raise MemoryConflictError("deletion request id was reused")
                return self._record(existing)
            if int(row["version"]) != expected_version or row["status"] == MemoryStatus.REVOKED.value:
                raise MemoryConflictError("memory deletion version or state conflict")
            connection.execute(update(MEMORY_RECORDS).where(and_(
                MEMORY_RECORDS.c.tenant_id == principal.tenant_id,
                MEMORY_RECORDS.c.memory_id == memory_id)).values(
                    status=MemoryStatus.REVOKED.value, version=expected_version + 1))
            values = dict(tenant_id=principal.tenant_id, request_id=request_id,
                memory_id=memory_id, requester_id=principal.principal_id,
                reason_digest=digest, previous_status=row["status"], status="pending", requested_at=now,
                decided_by=None, decided_at=None, decision_reason_digest=None,
                content_fingerprint=row["content_fingerprint"])
            connection.execute(insert(MEMORY_DELETION_REQUESTS).values(**values))
            self._audit(connection, principal, request_id, memory_id,
                        "memory.deletion_requested", "pending")
            return self._record(values)

    def decide_deletion(self, *, principal: Principal, request_id: str,
                        approve: bool, reason: str) -> MemoryDeletionRecord:
        self._privacy_officer(principal)
        decision_digest = self._reason(reason)
        now = datetime.now(UTC)
        with self._transaction(principal.tenant_id) as connection:
            row = connection.execute(select(MEMORY_DELETION_REQUESTS).where(and_(
                MEMORY_DELETION_REQUESTS.c.tenant_id == principal.tenant_id,
                MEMORY_DELETION_REQUESTS.c.request_id == request_id)).with_for_update()).mappings().one_or_none()
            if row is None:
                raise MemoryUnavailableError("deletion request is not available")
            if row["status"] != "pending" or row["requester_id"] == principal.principal_id:
                raise MemoryConflictError("deletion decision violates state or separation of duties")
            if approve:
                hold = connection.execute(select(MEMORY_LEGAL_HOLDS.c.hold_id).where(and_(
                    MEMORY_LEGAL_HOLDS.c.tenant_id == principal.tenant_id,
                    MEMORY_LEGAL_HOLDS.c.memory_id == row["memory_id"],
                    MEMORY_LEGAL_HOLDS.c.active.is_(True)))).scalar_one_or_none()
                if hold is not None:
                    raise MemoryConflictError("memory is under legal hold")
                connection.execute(delete(MEMORY_RECORDS).where(and_(
                    MEMORY_RECORDS.c.tenant_id == principal.tenant_id,
                    MEMORY_RECORDS.c.memory_id == row["memory_id"])))
                target = "purged"
            else:
                connection.execute(update(MEMORY_RECORDS).where(and_(
                    MEMORY_RECORDS.c.tenant_id == principal.tenant_id,
                    MEMORY_RECORDS.c.memory_id == row["memory_id"])).values(
                        status=row["previous_status"],
                        version=MEMORY_RECORDS.c.version + 1))
                target = "rejected"
            connection.execute(update(MEMORY_DELETION_REQUESTS).where(and_(
                MEMORY_DELETION_REQUESTS.c.tenant_id == principal.tenant_id,
                MEMORY_DELETION_REQUESTS.c.request_id == request_id)).values(
                    status=target, decided_by=principal.principal_id, decided_at=now,
                    decision_reason_digest=decision_digest))
            self._audit(connection, principal, request_id, row["memory_id"],
                        "memory.deletion_decided", target)
            return MemoryDeletionRecord(request_id, row["memory_id"], target)

    def create_hold(self, *, principal: Principal, hold_id: str,
                    memory_id: str, reason: str) -> None:
        self._privacy_officer(principal)
        self._identifier(hold_id)
        now = datetime.now(UTC)
        with self._transaction(principal.tenant_id) as connection:
            if connection.execute(select(MEMORY_RECORDS.c.memory_id).where(and_(
                MEMORY_RECORDS.c.tenant_id == principal.tenant_id,
                MEMORY_RECORDS.c.memory_id == memory_id))).scalar_one_or_none() is None:
                raise MemoryUnavailableError("memory is not available")
            connection.execute(insert(MEMORY_LEGAL_HOLDS).values(
                tenant_id=principal.tenant_id, hold_id=hold_id, memory_id=memory_id,
                reason_digest=self._reason(reason), active=True, created_by=principal.principal_id,
                created_at=now, released_by=None, released_at=None))
            self._audit(connection, principal, hold_id, memory_id,
                        "memory.legal_hold_created", "active")

    def release_hold(self, *, principal: Principal, hold_id: str, reason: str) -> None:
        self._privacy_officer(principal)
        self._reason(reason)
        now = datetime.now(UTC)
        with self._transaction(principal.tenant_id) as connection:
            row = connection.execute(select(MEMORY_LEGAL_HOLDS).where(and_(
                MEMORY_LEGAL_HOLDS.c.tenant_id == principal.tenant_id,
                MEMORY_LEGAL_HOLDS.c.hold_id == hold_id)).with_for_update()).mappings().one_or_none()
            if row is None:
                raise MemoryUnavailableError("legal hold is not available")
            if not row["active"] or row["created_by"] == principal.principal_id:
                raise MemoryConflictError("legal hold release violates state or separation of duties")
            connection.execute(update(MEMORY_LEGAL_HOLDS).where(and_(
                MEMORY_LEGAL_HOLDS.c.tenant_id == principal.tenant_id,
                MEMORY_LEGAL_HOLDS.c.hold_id == hold_id)).values(
                    active=False, released_by=principal.principal_id, released_at=now))
            self._audit(connection, principal, hold_id, row["memory_id"],
                        "memory.legal_hold_released", "released")

    @staticmethod
    def _authorize_request(principal: Principal, row) -> None:
        if principal.is_service:
            raise PolicyDenied("service identities cannot request memory deletion")
        scope = MemoryScope(row["scope"])
        if scope is MemoryScope.USER_PRIVATE and row["owner_principal_id"] == principal.principal_id:
            return
        if scope is MemoryScope.SESSION and row["created_by"] == principal.principal_id:
            return
        if "memory_curator" not in principal.roles:
            raise PolicyDenied("shared memory deletion requires memory curator")
        if int(row["classification"]) > int(principal.clearance) or not set(
            row["compartments"]
        ).issubset(principal.compartments):
            raise PolicyDenied("memory curator lacks clearance or compartment")

    @staticmethod
    def _privacy_officer(principal: Principal) -> None:
        if principal.is_service or "memory_privacy_officer" not in principal.roles:
            raise PolicyDenied("memory privacy officer role is required")

    def _audit(self, connection, principal, request_id, memory_id, event_type, outcome):
        self.audit_log.append_in_transaction(connection, AuditEvent(
            tenant_id=principal.tenant_id, event_type=event_type,
            actor_id=principal.principal_id, outcome=outcome,
            details={"request_id": request_id, "memory_id": memory_id},
            correlation_id=request_id, event_id=str(uuid.uuid4())))

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant": tenant_id})
            elif connection.dialect.name != "sqlite":
                raise RuntimeError("memory lifecycle supports PostgreSQL and SQLite only")
            yield connection

    @staticmethod
    def _identifier(value: str) -> None:
        if _ID.fullmatch(value) is None:
            raise ValueError("lifecycle identifier is invalid")

    @staticmethod
    def _reason(value: str) -> str:
        if not value.strip() or len(value) > 2_000:
            raise ValueError("lifecycle reason is invalid")
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _record(row) -> MemoryDeletionRecord:
        return MemoryDeletionRecord(row["request_id"], row["memory_id"], row["status"])
