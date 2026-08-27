from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..collaboration.repository import GOVERNANCE_ASSIGNMENTS, GOVERNANCE_METADATA
from ..errors import GovernanceConflictError, GovernanceError
from ..postgres_audit import SQLAlchemyAuditLog
from .models import (
    ChangeImpact,
    Compatibility,
    ContractDependency,
    ContractKind,
    ContractRecord,
    ContractRelease,
    ImpactSeverity,
    ImpactState,
)

CONTRACT_METADATA = GOVERNANCE_METADATA
TENANTS = JSON().with_variant(ARRAY(String(128)), "postgresql")
JSON_OBJECT = JSON().with_variant(JSONB(), "postgresql")

CONTRACTS = Table(
    "collaboration_contracts", CONTRACT_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("contract_id", String(128), primary_key=True),
    Column("producer_tenant_id", String(128), nullable=False),
    Column("producer_assignment_id", String(128), nullable=False),
    Column("name", String(256), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("visible_to_tenants", TENANTS, nullable=False),
    Column("aggregate_version", BigInteger, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["program_id"], ["governance_programs.program_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(
        ["program_id", "producer_assignment_id"],
        ["governance_assignments.program_id", "governance_assignments.assignment_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("kind IN ('openapi','json_schema','protobuf','asyncapi','data','document','generic')", name="kind"),
    CheckConstraint("aggregate_version >= 1", name="aggregate_version"),
)

CONTRACT_RELEASES = Table(
    "collaboration_contract_releases", CONTRACT_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("contract_id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("content_digest", String(64), nullable=False),
    Column("artifact_ref", String(1024), nullable=False),
    Column("compatibility", String(32), nullable=False),
    Column("predecessor_version", String(64), nullable=True),
    Column("visible_to_tenants", TENANTS, nullable=False),
    Column("released_by", String(128), nullable=False),
    Column("released_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["program_id", "contract_id"], ["collaboration_contracts.program_id", "collaboration_contracts.contract_id"], ondelete="RESTRICT"),
    CheckConstraint("length(content_digest) = 64", name="digest"),
    CheckConstraint("compatibility IN ('compatible','breaking','unknown')", name="compatibility"),
)

CONTRACT_DEPENDENCIES = Table(
    "collaboration_contract_dependencies", CONTRACT_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("dependency_id", String(128), primary_key=True),
    Column("contract_id", String(128), nullable=False),
    Column("consumer_tenant_id", String(128), nullable=False),
    Column("consumer_assignment_id", String(128), nullable=False),
    Column("version_constraint", String(256), nullable=False),
    Column("baseline_version", String(64), nullable=False),
    Column("baseline_digest", String(64), nullable=False),
    Column("visible_to_tenants", TENANTS, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["program_id", "contract_id"], ["collaboration_contracts.program_id", "collaboration_contracts.contract_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(["program_id", "consumer_assignment_id"], ["governance_assignments.program_id", "governance_assignments.assignment_id"], ondelete="RESTRICT"),
    UniqueConstraint("program_id", "contract_id", "consumer_assignment_id", name="uq_contract_dependency_consumer"),
    CheckConstraint("length(baseline_digest) = 64", name="baseline_digest"),
)

CHANGE_IMPACTS = Table(
    "collaboration_change_impacts", CONTRACT_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("impact_id", String(128), primary_key=True),
    Column("dependency_id", String(128), nullable=False),
    Column("contract_id", String(128), nullable=False),
    Column("consumer_tenant_id", String(128), nullable=False),
    Column("consumer_assignment_id", String(128), nullable=False),
    Column("from_version", String(64), nullable=False),
    Column("to_version", String(64), nullable=False),
    Column("from_digest", String(64), nullable=False),
    Column("to_digest", String(64), nullable=False),
    Column("severity", Integer, nullable=False),
    Column("compatibility", String(32), nullable=False),
    Column("state", String(32), nullable=False),
    Column("state_version", BigInteger, nullable=False),
    Column("consumer_note", Text, nullable=True),
    Column("remediation", Text, nullable=True),
    Column("acknowledged_by", String(128), nullable=True),
    Column("accepted_by", String(128), nullable=True),
    Column("visible_to_tenants", TENANTS, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["program_id", "dependency_id"], ["collaboration_contract_dependencies.program_id", "collaboration_contract_dependencies.dependency_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(["program_id", "contract_id", "to_version"], ["collaboration_contract_releases.program_id", "collaboration_contract_releases.contract_id", "collaboration_contract_releases.version"], ondelete="RESTRICT"),
    UniqueConstraint("program_id", "dependency_id", "to_version", name="uq_change_impact_release"),
    CheckConstraint("length(from_digest)=64 AND length(to_digest)=64", name="digests"),
    CheckConstraint("severity BETWEEN 0 AND 2", name="severity"),
    CheckConstraint("state_version >= 1", name="state_version"),
    CheckConstraint("compatibility IN ('compatible','breaking','unknown')", name="compatibility"),
    CheckConstraint("state IN ('pending','acknowledged','blocked','remediation_proposed','accepted')", name="state"),
)
Index("ix_change_impacts_assignment_state", CHANGE_IMPACTS.c.consumer_assignment_id, CHANGE_IMPACTS.c.state)

CONTRACT_EVENTS = Table(
    "collaboration_contract_events", CONTRACT_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("actor_tenant_id", String(128), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("payload", JSON_OBJECT, nullable=False),
    Column("visible_to_tenants", TENANTS, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("program_id", "event_id", name="uq_contract_event_id"),
)

CONTRACT_OUTBOX = Table(
    "collaboration_contract_outbox", CONTRACT_METADATA,
    Column("message_id", String(128), primary_key=True),
    Column("program_id", String(128), nullable=False),
    Column("event_sequence", BigInteger, nullable=False),
    Column("producer_tenant_id", String(128), nullable=False),
    Column("recipient_tenant_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["program_id", "event_sequence"], ["collaboration_contract_events.program_id", "collaboration_contract_events.sequence"], ondelete="RESTRICT"),
    UniqueConstraint("program_id", "event_sequence", "recipient_tenant_id", name="uq_contract_outbox_recipient"),
    CheckConstraint("status IN ('pending','published','dead_letter')", name="status"),
)

CONTRACT_COMMANDS = Table(
    "collaboration_contract_commands", CONTRACT_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("program_id", String(128), nullable=False),
    Column("command_type", String(128), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("result_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(request_digest)=64", name="request_digest"),
)

ALL_CONTRACT_TABLES = (CONTRACTS, CONTRACT_RELEASES, CONTRACT_DEPENDENCIES, CHANGE_IMPACTS, CONTRACT_EVENTS, CONTRACT_OUTBOX, CONTRACT_COMMANDS)


@dataclass(frozen=True, slots=True)
class CommandClaim:
    duplicate: bool
    result_id: str | None


class SQLAlchemyContractRepository:
    def __init__(self, *, engine: Engine, audit_log: SQLAlchemyAuditLog) -> None:
        if audit_log.engine is not engine:
            raise ValueError("contracts and audit must share one database engine")
        self.engine = engine
        self.audit_log = audit_log

    def create_schema(self) -> None:
        CONTRACT_METADATA.create_all(
            self.engine,
            tables=list(ALL_CONTRACT_TABLES),
            checkfirst=True,
        )

    @contextmanager
    def transaction(self, tenant_id: str) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            self.set_tenant(connection, tenant_id)
            yield connection

    @staticmethod
    def set_tenant(connection: Connection, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("tenant_id is invalid")
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant": tenant_id})
        elif connection.dialect.name != "sqlite":
            raise GovernanceError("contract persistence supports PostgreSQL and SQLite only")

    def claim(self, connection: Connection, *, tenant_id: str, key: str, program_id: str, command_type: str, digest: str) -> CommandClaim:
        if not key or len(key) > 128:
            raise ValueError("idempotency key is invalid")
        values = dict(tenant_id=tenant_id, idempotency_key=key, program_id=program_id, command_type=command_type, request_digest=digest, result_id=None, created_at=datetime.now(UTC))
        base = pg_insert(CONTRACT_COMMANDS) if connection.dialect.name == "postgresql" else sqlite_insert(CONTRACT_COMMANDS)
        connection.execute(base.values(**values).on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"]))
        row = connection.execute(select(CONTRACT_COMMANDS).where(and_(CONTRACT_COMMANDS.c.tenant_id == tenant_id, CONTRACT_COMMANDS.c.idempotency_key == key))).mappings().one()
        if row["program_id"] != program_id or row["command_type"] != command_type or row["request_digest"] != digest:
            raise GovernanceConflictError("idempotency key was reused with a different command")
        return CommandClaim(row["result_id"] is not None, row["result_id"])

    @staticmethod
    def complete(connection: Connection, *, tenant_id: str, key: str, result_id: str) -> None:
        connection.execute(update(CONTRACT_COMMANDS).where(and_(CONTRACT_COMMANDS.c.tenant_id == tenant_id, CONTRACT_COMMANDS.c.idempotency_key == key, CONTRACT_COMMANDS.c.result_id.is_(None))).values(result_id=result_id))

    def append_event(self, connection: Connection, *, event: AuditEvent, subject_id: str, visibility: frozenset[str], payload: dict) -> None:
        normalized = event.normalized()
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:namespace),hashtext(:program))"),
                {"namespace": "coifesp-contract-events-v1", "program": normalized.correlation_id},
            )
        last = connection.execute(select(CONTRACT_EVENTS.c.sequence).where(CONTRACT_EVENTS.c.program_id == normalized.correlation_id).order_by(CONTRACT_EVENTS.c.sequence.desc()).limit(1).with_for_update()).scalar_one_or_none()
        sequence = int(last or 0) + 1
        connection.execute(insert(CONTRACT_EVENTS).values(program_id=normalized.correlation_id, sequence=sequence, event_id=normalized.event_id, event_type=normalized.event_type, actor_id=normalized.actor_id, actor_tenant_id=normalized.tenant_id, subject_id=subject_id, payload=payload, visible_to_tenants=sorted(visibility), occurred_at=datetime.fromisoformat(normalized.occurred_at)))
        import hashlib
        for recipient in visibility:
            message_id = hashlib.sha256(f"{normalized.correlation_id}\n{normalized.event_id}\n{recipient}".encode()).hexdigest()
            connection.execute(insert(CONTRACT_OUTBOX).values(message_id=message_id, program_id=normalized.correlation_id, event_sequence=sequence, producer_tenant_id=normalized.tenant_id, recipient_tenant_id=recipient, status="pending", created_at=datetime.now(UTC)))
        self.audit_log.append_in_transaction(connection, normalized)

    def get_contract(self, connection: Connection, *, program_id: str, contract_id: str) -> ContractRecord | None:
        row = connection.execute(select(CONTRACTS).where(and_(CONTRACTS.c.program_id == program_id, CONTRACTS.c.contract_id == contract_id))).mappings().one_or_none()
        return None if row is None else ContractRecord(contract_id=row["contract_id"], program_id=row["program_id"], producer_tenant_id=row["producer_tenant_id"], producer_assignment_id=row["producer_assignment_id"], name=row["name"], kind=ContractKind(row["kind"]), visible_to_tenants=frozenset(row["visible_to_tenants"]), created_by=row["created_by"], created_at=row["created_at"])

    def get_release(self, connection: Connection, *, program_id: str, contract_id: str, version: str) -> ContractRelease | None:
        row = connection.execute(select(CONTRACT_RELEASES).where(and_(CONTRACT_RELEASES.c.program_id == program_id, CONTRACT_RELEASES.c.contract_id == contract_id, CONTRACT_RELEASES.c.version == version))).mappings().one_or_none()
        return None if row is None else ContractRelease(contract_id=row["contract_id"], version=row["version"], content_digest=row["content_digest"], artifact_ref=row["artifact_ref"], compatibility=Compatibility(row["compatibility"]), predecessor_version=row["predecessor_version"], released_by=row["released_by"], released_at=row["released_at"])

    def list_impacts(self, connection: Connection, *, program_id: str, assignment_id: str) -> tuple[ChangeImpact, ...]:
        rows = connection.execute(select(CHANGE_IMPACTS).where(and_(CHANGE_IMPACTS.c.program_id == program_id, CHANGE_IMPACTS.c.consumer_assignment_id == assignment_id)).order_by(CHANGE_IMPACTS.c.created_at)).mappings().all()
        return tuple(self._impact(row) for row in rows)

    @staticmethod
    def _impact(row) -> ChangeImpact:
        return ChangeImpact(impact_id=row["impact_id"], dependency_id=row["dependency_id"], contract_id=row["contract_id"], consumer_tenant_id=row["consumer_tenant_id"], consumer_assignment_id=row["consumer_assignment_id"], from_version=row["from_version"], to_version=row["to_version"], from_digest=row["from_digest"], to_digest=row["to_digest"], severity=ImpactSeverity(row["severity"]), compatibility=Compatibility(row["compatibility"]), state=ImpactState(row["state"]), consumer_note=row["consumer_note"], remediation=row["remediation"], acknowledged_by=row["acknowledged_by"], accepted_by=row["accepted_by"], created_at=row["created_at"], updated_at=row["updated_at"])
