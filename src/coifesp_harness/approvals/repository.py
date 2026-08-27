from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Iterator

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..errors import HarnessError, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security.models import Classification, ResourceLabel
from .models import ApprovalRecord, ApprovalStatus

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JSON = JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

APPROVAL_METADATA = MetaData()
APPROVAL_REQUESTS = Table(
    "approval_requests",
    APPROVAL_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("approval_id", String(128), primary_key=True),
    Column("requester_id", String(128), nullable=False),
    Column("tool_name", String(128), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("reason_digest", String(64), nullable=False),
    Column("origin", String(32), nullable=False),
    Column("review_projection", _JSON, nullable=True),
    Column("projection_digest", String(64), nullable=True),
    Column("classification", Integer, nullable=False),
    Column("compartments", _JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("required_approver_role", String(128), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("consumed_by_execution_id", String(128), nullable=True),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("length(request_digest) = 64", name="approval_request_digest"),
    CheckConstraint("length(reason_digest) = 64", name="approval_reason_digest"),
    CheckConstraint("origin IN ('manual','tool_managed')", name="approval_origin"),
    CheckConstraint(
        "(origin = 'tool_managed') = "
        "(review_projection IS NOT NULL AND projection_digest IS NOT NULL)",
        name="approval_projection",
    ),
    CheckConstraint(
        "projection_digest IS NULL OR length(projection_digest) = 64",
        name="approval_projection_digest",
    ),
    CheckConstraint(
        "status IN ('pending','approved','rejected','revoked','consumed')",
        name="approval_status",
    ),
    CheckConstraint("version > 0", name="approval_version"),
    CheckConstraint(
        "(status IN ('approved','rejected','revoked','consumed')) = "
        "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
        name="approval_decision",
    ),
    CheckConstraint(
        "(status = 'consumed') = "
        "(consumed_by_execution_id IS NOT NULL AND consumed_at IS NOT NULL)",
        name="approval_consumption",
    ),
)
Index(
    "ix_approval_requests_tenant_status_expiry",
    APPROVAL_REQUESTS.c.tenant_id,
    APPROVAL_REQUESTS.c.status,
    APPROVAL_REQUESTS.c.expires_at,
)


class ApprovalWorkflowError(HarnessError):
    """An approval command violated scope, state, or separation of duties."""


class SQLAlchemyApprovalRepository:
    def __init__(
        self,
        *,
        engine: Engine,
        audit_log: SQLAlchemyAuditLog | None = None,
        _bound_connection: Connection | None = None,
    ) -> None:
        self.engine = engine
        self.audit_log = audit_log
        self._bound_connection = _bound_connection

    def create_schema(self) -> None:
        APPROVAL_METADATA.create_all(self.engine)

    def using_connection(self, connection: Connection) -> "SQLAlchemyApprovalRepository":
        if connection.engine is not self.engine:
            raise ApprovalWorkflowError("bound connection belongs to a different engine")
        return SQLAlchemyApprovalRepository(
            engine=self.engine,
            audit_log=self.audit_log,
            _bound_connection=connection,
        )

    def create(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        requester_id: str,
        tool_name: str,
        request_digest: str,
        reason: str,
        label: ResourceLabel,
        required_approver_role: str,
        expires_at: datetime,
        origin: str = "manual",
        review_projection: dict | None = None,
    ) -> ApprovalRecord:
        for name, value in (
            ("tenant_id", tenant_id),
            ("approval_id", approval_id),
            ("requester_id", requester_id),
            ("tool_name", tool_name),
            ("required_approver_role", required_approver_role),
        ):
            self._identifier(name, value)
        if not _DIGEST.fullmatch(request_digest):
            raise ApprovalWorkflowError("request digest is invalid")
        if not reason.strip() or len(reason.encode("utf-8")) > 8_192:
            raise ApprovalWorkflowError("approval reason is required and bounded")
        now = datetime.now(UTC)
        expiry = self._aware(expires_at)
        if not now < expiry <= now + timedelta(days=366):
            raise ApprovalWorkflowError("approval expiry is invalid")
        reason_digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        if origin not in {"manual", "tool_managed"}:
            raise ApprovalWorkflowError("approval origin is invalid")
        projection_digest = None
        if review_projection is not None:
            if (
                review_projection.get("schema") != "coifesp.approval-review.v1"
                or review_projection.get("tool_name") != tool_name
                or not isinstance(review_projection.get("fields"), list)
            ):
                raise ApprovalWorkflowError("approval review projection schema is invalid")
            projection_json = json.dumps(
                review_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(projection_json.encode("utf-8")) > 65_536:
                raise ApprovalWorkflowError("approval review projection exceeds its size limit")
            projection_digest = hashlib.sha256(projection_json.encode("utf-8")).hexdigest()
        if (origin == "tool_managed") != (review_projection is not None):
            raise ApprovalWorkflowError("tool-managed approvals require a review projection")
        if label.owner_tenant_id != tenant_id:
            raise ApprovalWorkflowError("approval label must be owned by the requester tenant")
        for compartment in label.compartments:
            self._identifier("compartment", compartment)
        values = {
            "tenant_id": tenant_id,
            "approval_id": approval_id,
            "requester_id": requester_id,
            "tool_name": tool_name,
            "request_digest": request_digest,
            "reason_digest": reason_digest,
            "origin": origin,
            "review_projection": review_projection,
            "projection_digest": projection_digest,
            "classification": int(label.classification),
            "compartments": sorted(label.compartments),
            "status": ApprovalStatus.PENDING.value,
            "required_approver_role": required_approver_role,
            "expires_at": expiry,
            "created_at": now,
            "version": 1,
            "decided_by": None,
            "decided_at": None,
            "consumed_by_execution_id": None,
            "consumed_at": None,
        }
        with self._transaction(tenant_id) as connection:
            dialect_insert = (
                postgresql_insert(APPROVAL_REQUESTS)
                if connection.dialect.name == "postgresql"
                else sqlite_insert(APPROVAL_REQUESTS)
            )
            inserted = connection.execute(
                dialect_insert.values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "approval_id"],
                )
                .returning(APPROVAL_REQUESTS.c.approval_id)
            ).scalar_one_or_none()
            if inserted is None:
                existing = self._row(connection, tenant_id, approval_id, lock=False)
                if not self._same_request(existing, values):
                    raise ApprovalWorkflowError(
                        "approval ID was reused with different request content"
                    )
                return self._record(existing)
            self._audit(
                connection,
                tenant_id=tenant_id,
                actor_id=requester_id,
                approval_id=approval_id,
                outcome="pending",
                event_type="approval.requested",
                details={"tool_name": tool_name, "request_digest": request_digest},
            )
            return self._load(connection, tenant_id, approval_id)

    def decide(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        approver_id: str,
        approver_roles: frozenset[str],
        approver_clearance: Classification,
        approver_compartments: frozenset[str],
        approve: bool,
        expected_version: int,
    ) -> ApprovalRecord:
        self._identifier("approver_id", approver_id)
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, approval_id, lock=True)
            if row["status"] != ApprovalStatus.PENDING.value:
                raise ApprovalWorkflowError("approval request is not pending")
            if int(row["version"]) != expected_version:
                raise ApprovalWorkflowError("approval request version conflict")
            if self._aware(row["expires_at"]) <= now:
                raise ApprovalWorkflowError("approval request has expired")
            if row["requester_id"] == approver_id:
                raise ApprovalWorkflowError("requester cannot approve their own operation")
            if row["required_approver_role"] not in approver_roles:
                raise ApprovalWorkflowError("required approver role is missing")
            if approver_clearance < Classification(int(row["classification"])):
                raise ApprovalWorkflowError("approver clearance is insufficient")
            if not frozenset(row["compartments"]).issubset(approver_compartments):
                raise ApprovalWorkflowError("approver lacks one or more required compartments")
            target = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            connection.execute(
                update(APPROVAL_REQUESTS)
                .where(
                    and_(
                        APPROVAL_REQUESTS.c.tenant_id == tenant_id,
                        APPROVAL_REQUESTS.c.approval_id == approval_id,
                        APPROVAL_REQUESTS.c.version == expected_version,
                    )
                )
                .values(
                    status=target.value,
                    decided_by=approver_id,
                    decided_at=now,
                    version=expected_version + 1,
                )
            )
            self._audit(
                connection,
                tenant_id=tenant_id,
                actor_id=approver_id,
                approval_id=approval_id,
                outcome=target.value,
                event_type=f"approval.{target.value}",
                details={},
            )
            return self._load(connection, tenant_id, approval_id)

    def revoke(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        actor_id: str,
        actor_roles: frozenset[str],
        expected_version: int,
    ) -> ApprovalRecord:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, approval_id, lock=True)
            if row["status"] not in {
                ApprovalStatus.PENDING.value,
                ApprovalStatus.APPROVED.value,
            }:
                raise ApprovalWorkflowError("approval cannot be revoked in its current state")
            if int(row["version"]) != expected_version:
                raise ApprovalWorkflowError("approval request version conflict")
            if actor_id != row["requester_id"] and row["required_approver_role"] not in actor_roles:
                raise ApprovalWorkflowError("principal cannot revoke this approval")
            connection.execute(
                update(APPROVAL_REQUESTS)
                .where(
                    and_(
                        APPROVAL_REQUESTS.c.tenant_id == tenant_id,
                        APPROVAL_REQUESTS.c.approval_id == approval_id,
                    )
                )
                .values(
                    status=ApprovalStatus.REVOKED.value,
                    decided_by=actor_id,
                    decided_at=now,
                    version=expected_version + 1,
                )
            )
            self._audit(
                connection,
                tenant_id=tenant_id,
                actor_id=actor_id,
                approval_id=approval_id,
                outcome="revoked",
                event_type="approval.revoked",
                details={},
            )
            return self._load(connection, tenant_id, approval_id)

    def consume(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        requester_id: str,
        tool_name: str,
        request_digest: str,
        execution_id: str,
        input_label: ResourceLabel,
        required_origin: str | None = None,
    ) -> ApprovalRecord:
        self._identifier("execution_id", execution_id)
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, approval_id, lock=True)
            if required_origin is not None and row["origin"] != required_origin:
                raise ApprovalWorkflowError("approval origin is not valid for this operation")
            if row["status"] == ApprovalStatus.CONSUMED.value:
                if (
                    row["consumed_by_execution_id"] == execution_id
                    and row["requester_id"] == requester_id
                    and row["tool_name"] == tool_name
                    and row["request_digest"] == request_digest
                    and int(row["classification"]) == int(input_label.classification)
                    and frozenset(row["compartments"]) == input_label.compartments
                ):
                    return self._record(row)
                raise ApprovalWorkflowError("approval has already been consumed")
            if row["status"] != ApprovalStatus.APPROVED.value:
                raise ApprovalWorkflowError("approval is not approved")
            if self._aware(row["expires_at"]) <= now:
                raise ApprovalWorkflowError("approval has expired")
            if (
                row["requester_id"] != requester_id
                or row["tool_name"] != tool_name
                or row["request_digest"] != request_digest
                or row["tenant_id"] != input_label.owner_tenant_id
                or int(row["classification"]) != int(input_label.classification)
                or frozenset(row["compartments"]) != input_label.compartments
            ):
                raise ApprovalWorkflowError("approval does not match this exact operation")
            connection.execute(
                update(APPROVAL_REQUESTS)
                .where(
                    and_(
                        APPROVAL_REQUESTS.c.tenant_id == tenant_id,
                        APPROVAL_REQUESTS.c.approval_id == approval_id,
                        APPROVAL_REQUESTS.c.status == ApprovalStatus.APPROVED.value,
                    )
                )
                .values(
                    status=ApprovalStatus.CONSUMED.value,
                    consumed_by_execution_id=execution_id,
                    consumed_at=now,
                    version=int(row["version"]) + 1,
                )
            )
            self._audit(
                connection,
                tenant_id=tenant_id,
                actor_id=requester_id,
                approval_id=approval_id,
                outcome="consumed",
                event_type="approval.consumed",
                details={
                    "tool_name": tool_name,
                    "request_digest": request_digest,
                    "execution_id": execution_id,
                },
            )
            return self._load(connection, tenant_id, approval_id)

    def get(self, *, tenant_id: str, approval_id: str) -> ApprovalRecord:
        with self._transaction(tenant_id) as connection:
            return self._load(connection, tenant_id, approval_id)

    def list_pending(
        self,
        *,
        tenant_id: str,
        required_roles: frozenset[str],
        limit: int = 100,
    ) -> tuple[ApprovalRecord, ...]:
        if not 1 <= limit <= 500:
            raise ApprovalWorkflowError("approval list limit is invalid")
        if not required_roles:
            return ()
        with self._transaction(tenant_id) as connection:
            rows = (
                connection.execute(
                    select(APPROVAL_REQUESTS)
                    .where(
                        and_(
                            APPROVAL_REQUESTS.c.tenant_id == tenant_id,
                            APPROVAL_REQUESTS.c.status == ApprovalStatus.PENDING.value,
                            APPROVAL_REQUESTS.c.expires_at > datetime.now(UTC),
                            APPROVAL_REQUESTS.c.required_approver_role.in_(required_roles),
                        )
                    )
                    .order_by(APPROVAL_REQUESTS.c.created_at)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return tuple(self._record(row) for row in rows)

    def _load(self, connection: Connection, tenant_id: str, approval_id: str) -> ApprovalRecord:
        return self._record(self._row(connection, tenant_id, approval_id, lock=False))

    @staticmethod
    def _same_request(row, values: dict) -> bool:
        return all(
            (
                row["requester_id"] == values["requester_id"],
                row["tool_name"] == values["tool_name"],
                row["request_digest"] == values["request_digest"],
                row["reason_digest"] == values["reason_digest"],
                row["origin"] == values["origin"],
                row["projection_digest"] == values["projection_digest"],
                int(row["classification"]) == values["classification"],
                frozenset(row["compartments"]) == frozenset(values["compartments"]),
                row["required_approver_role"] == values["required_approver_role"],
            )
        )

    @staticmethod
    def _record(row) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            tenant_id=row["tenant_id"],
            requester_id=row["requester_id"],
            tool_name=row["tool_name"],
            request_digest=row["request_digest"],
            reason_digest=row["reason_digest"],
            origin=row["origin"],
            review_projection=(
                dict(row["review_projection"]) if row["review_projection"] is not None else None
            ),
            projection_digest=row["projection_digest"],
            classification=Classification(int(row["classification"])),
            compartments=frozenset(row["compartments"]),
            status=ApprovalStatus(row["status"]),
            required_approver_role=row["required_approver_role"],
            expires_at=SQLAlchemyApprovalRepository._aware(row["expires_at"]),
            created_at=SQLAlchemyApprovalRepository._aware(row["created_at"]),
            version=int(row["version"]),
            decided_by=row["decided_by"],
            decided_at=(
                SQLAlchemyApprovalRepository._aware(row["decided_at"])
                if row["decided_at"]
                else None
            ),
            consumed_by_execution_id=row["consumed_by_execution_id"],
            consumed_at=(
                SQLAlchemyApprovalRepository._aware(row["consumed_at"])
                if row["consumed_at"]
                else None
            ),
        )

    @staticmethod
    def _row(
        connection: Connection,
        tenant_id: str,
        approval_id: str,
        *,
        lock: bool,
    ):
        statement = select(APPROVAL_REQUESTS).where(
            and_(
                APPROVAL_REQUESTS.c.tenant_id == tenant_id,
                APPROVAL_REQUESTS.c.approval_id == approval_id,
            )
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("approval is absent or hidden")
        return row

    def _audit(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        actor_id: str,
        approval_id: str,
        event_type: str,
        outcome: str,
        details: dict,
    ) -> None:
        if self.audit_log is not None:
            self.audit_log.append_in_transaction(
                connection,
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type=event_type,
                    actor_id=actor_id,
                    outcome=outcome,
                    details={"approval_id": approval_id, **details},
                    correlation_id=approval_id,
                    event_id=str(uuid.uuid4()),
                ),
            )

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[Connection]:
        self._identifier("tenant_id", tenant_id)
        if self._bound_connection is not None:
            self._set_tenant(self._bound_connection, tenant_id)
            yield self._bound_connection
            return
        with self.engine.begin() as connection:
            self._set_tenant(connection, tenant_id)
            yield connection

    @staticmethod
    def _set_tenant(connection: Connection, tenant_id: str) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
        elif connection.dialect.name != "sqlite":
            raise ApprovalWorkflowError("approval storage supports PostgreSQL and SQLite only")

    @staticmethod
    def _identifier(name: str, value: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ApprovalWorkflowError(f"{name} is invalid")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
