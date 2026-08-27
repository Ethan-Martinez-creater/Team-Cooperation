from __future__ import annotations

import hashlib
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
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
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

from ..audit import AuditEvent, AuditSink
from ..errors import GovernanceConflictError, GovernanceError
from ..idempotency import ClaimStatus
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification
from .governance import GovernanceBoard
from .governance_models import (
    AssignmentState,
    BoardMember,
    CollaborationRole,
    DiscussionItem,
    DiscussionKind,
    PlanRecord,
    PlanState,
    TaskAssignment,
)

GOVERNANCE_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
)

TENANT_LIST = JSON().with_variant(ARRAY(String(128)), "postgresql")
JSON_OBJECT = JSON().with_variant(JSONB(), "postgresql")

GOVERNANCE_PROGRAMS = Table(
    "governance_programs",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("owner_tenant_id", String(128), nullable=False),
    Column("title", String(256), nullable=False),
    Column("objective", Text, nullable=False),
    Column("classification", Integer, nullable=False),
    Column("compartments", JSON_OBJECT, nullable=False),
    Column("participant_tenant_ids", TENANT_LIST, nullable=False),
    Column("aggregate_version", BigInteger, nullable=False),
    Column("last_event_sequence", BigInteger, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("classification BETWEEN 0 AND 3", name="classification"),
    CheckConstraint("aggregate_version >= 0", name="aggregate_version"),
    CheckConstraint("last_event_sequence >= 0", name="event_sequence"),
)

GOVERNANCE_MEMBERS = Table(
    "governance_members",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("principal_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("role", String(32), nullable=False),
    Column("added_by", String(128), nullable=False),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    Column("joined_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["program_id"],
        ["governance_programs.program_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "role IN ('lead', 'contributor', 'reviewer', 'observer')",
        name="role",
    ),
)
Index(
    "ix_governance_members_tenant_role",
    GOVERNANCE_MEMBERS.c.tenant_id,
    GOVERNANCE_MEMBERS.c.role,
)

GOVERNANCE_PLANS = Table(
    "governance_plans",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("plan_id", String(128), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("title", String(256), nullable=False),
    Column("objective", Text, nullable=False),
    Column("lead_id", String(128), nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("state", String(32), nullable=False),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["program_id"],
        ["governance_programs.program_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("version > 0", name="positive_version"),
    CheckConstraint("length(content_digest) = 64", name="digest"),
    CheckConstraint(
        "state IN ('draft', 'discussion', 'approved', 'rejected', 'superseded')",
        name="state",
    ),
)
Index(
    "ix_governance_plans_program_state",
    GOVERNANCE_PLANS.c.program_id,
    GOVERNANCE_PLANS.c.state,
)

GOVERNANCE_PLAN_DELIVERABLES = Table(
    "governance_plan_deliverables",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("plan_id", String(128), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("content", Text, nullable=False),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    ForeignKeyConstraint(
        ["program_id", "plan_id"],
        ["governance_plans.program_id", "governance_plans.plan_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("position >= 0", name="position"),
)

GOVERNANCE_PLAN_APPROVERS = Table(
    "governance_plan_approvers",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("plan_id", String(128), primary_key=True),
    Column("principal_id", String(128), primary_key=True),
    Column("approved_digest", String(64), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    ForeignKeyConstraint(
        ["program_id", "plan_id"],
        ["governance_plans.program_id", "governance_plans.plan_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "approved_digest IS NULL OR length(approved_digest) = 64",
        name="approved_digest",
    ),
)

GOVERNANCE_DISCUSSION_ITEMS = Table(
    "governance_discussion_items",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("item_id", String(128), primary_key=True),
    Column("plan_id", String(128), nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("blocking", Boolean, nullable=False),
    Column("resolved", Boolean, nullable=False),
    Column("resolved_by", String(128), nullable=True),
    Column("resolution", Text, nullable=True),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["program_id", "plan_id"],
        ["governance_plans.program_id", "governance_plans.plan_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "program_id",
        "plan_id",
        "item_id",
        name="uq_governance_discussion_plan_item",
    ),
    CheckConstraint(
        "kind IN ('comment', 'proposal', 'risk', 'objection')",
        name="kind",
    ),
    CheckConstraint(
        "(resolved AND resolved_by IS NOT NULL AND resolution IS NOT NULL) "
        "OR (NOT resolved AND resolved_by IS NULL AND resolution IS NULL)",
        name="resolution",
    ),
)
Index(
    "ix_governance_discussion_plan",
    GOVERNANCE_DISCUSSION_ITEMS.c.program_id,
    GOVERNANCE_DISCUSSION_ITEMS.c.plan_id,
)

GOVERNANCE_ASSIGNMENTS = Table(
    "governance_assignments",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("assignment_id", String(128), primary_key=True),
    Column("plan_id", String(128), nullable=False),
    Column("plan_digest", String(64), nullable=False),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("deliverable_contract", Text, nullable=False),
    Column("proposed_by", String(128), nullable=False),
    Column("assignee_id", String(128), nullable=False),
    Column("state", String(32), nullable=False),
    Column("response_reason", Text, nullable=True),
    Column("verification_note", Text, nullable=True),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["program_id", "plan_id"],
        ["governance_plans.program_id", "governance_plans.plan_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("length(plan_digest) = 64", name="plan_digest"),
    CheckConstraint(
        "state IN ('proposed', 'accepted', 'in_progress', 'submitted', " "'verified', 'declined')",
        name="state",
    ),
)
Index(
    "ix_governance_assignments_assignee_state",
    GOVERNANCE_ASSIGNMENTS.c.assignee_id,
    GOVERNANCE_ASSIGNMENTS.c.state,
)

GOVERNANCE_ASSIGNMENT_DEPENDENCIES = Table(
    "governance_assignment_dependencies",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("assignment_id", String(128), primary_key=True),
    Column("dependency_id", String(128), primary_key=True),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    ForeignKeyConstraint(
        ["program_id", "assignment_id"],
        ["governance_assignments.program_id", "governance_assignments.assignment_id"],
        name="fk_governance_assignment_dependency_source",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["program_id", "dependency_id"],
        ["governance_assignments.program_id", "governance_assignments.assignment_id"],
        name="fk_governance_assignment_dependency_target",
        ondelete="RESTRICT",
    ),
    CheckConstraint("assignment_id <> dependency_id", name="not_self"),
)

GOVERNANCE_ASSIGNMENT_ARTIFACTS = Table(
    "governance_assignment_artifacts",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("assignment_id", String(128), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("artifact_ref", String(1024), nullable=False),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    ForeignKeyConstraint(
        ["program_id", "assignment_id"],
        ["governance_assignments.program_id", "governance_assignments.assignment_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("position >= 0", name="position"),
)

GOVERNANCE_EVENTS = Table(
    "governance_events",
    GOVERNANCE_METADATA,
    Column("program_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("actor_tenant_id", String(128), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("payload", JSON_OBJECT, nullable=False),
    Column("visible_to_tenants", TENANT_LIST, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("audit_event_id", String(128), nullable=False),
    ForeignKeyConstraint(
        ["program_id"],
        ["governance_programs.program_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "program_id",
        "event_id",
        name="uq_governance_events_program_event",
    ),
    CheckConstraint("sequence > 0", name="positive_sequence"),
)
Index(
    "ix_governance_events_program_occurred",
    GOVERNANCE_EVENTS.c.program_id,
    GOVERNANCE_EVENTS.c.occurred_at,
)

GOVERNANCE_OUTBOX = Table(
    "governance_outbox",
    GOVERNANCE_METADATA,
    Column("message_id", String(128), primary_key=True),
    Column("program_id", String(128), nullable=False),
    Column("event_sequence", BigInteger, nullable=False),
    Column("producer_tenant_id", String(128), nullable=False),
    Column("recipient_tenant_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("envelope", JSON_OBJECT, nullable=True),
    Column("envelope_digest", String(64), nullable=True),
    Column("last_error_code", String(128), nullable=True),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["program_id", "event_sequence"],
        ["governance_events.program_id", "governance_events.sequence"],
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "status IN ('pending', 'claimed', 'published', 'dead_letter')",
        name="status",
    ),
    CheckConstraint(
        "max_attempts BETWEEN 1 AND 100 AND " "attempt_count BETWEEN 0 AND max_attempts",
        name="attempt_count",
    ),
    CheckConstraint(
        "(status = 'claimed' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status <> 'claimed' AND lease_owner IS NULL "
        "AND lease_token IS NULL AND lease_expires_at IS NULL)",
        name="claimed_lease",
    ),
    CheckConstraint(
        "(status = 'published' AND published_at IS NOT NULL "
        "AND envelope IS NOT NULL AND envelope_digest IS NOT NULL) "
        "OR (status <> 'published' AND published_at IS NULL)",
        name="published_envelope",
    ),
    UniqueConstraint(
        "program_id",
        "event_sequence",
        "recipient_tenant_id",
        name="uq_governance_outbox_event_recipient",
    ),
)
Index(
    "ix_governance_outbox_producer_status_available",
    GOVERNANCE_OUTBOX.c.producer_tenant_id,
    GOVERNANCE_OUTBOX.c.status,
    GOVERNANCE_OUTBOX.c.available_at,
)

COLLABORATION_INBOX = Table(
    "collaboration_inbox",
    GOVERNANCE_METADATA,
    Column("recipient_tenant_id", String(128), primary_key=True),
    Column("message_id", String(128), primary_key=True),
    Column("sender_tenant_id", String(128), nullable=False),
    Column("envelope", JSON_OBJECT, nullable=False),
    Column("envelope_digest", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("handler_key", String(128), nullable=True),
    Column("result_digest", String(64), nullable=True),
    Column("last_error_code", String(128), nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("length(envelope_digest) = 64", name="envelope_digest"),
    CheckConstraint(
        "result_digest IS NULL OR length(result_digest) = 64",
        name="result_digest",
    ),
    CheckConstraint(
        "status IN ('received', 'claimed', 'processed', 'rejected')",
        name="status",
    ),
    CheckConstraint(
        "max_attempts BETWEEN 1 AND 100 AND " "attempt_count BETWEEN 0 AND max_attempts",
        name="attempt_count",
    ),
    CheckConstraint(
        "(status = 'claimed' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status <> 'claimed' AND lease_owner IS NULL "
        "AND lease_token IS NULL AND lease_expires_at IS NULL)",
        name="claimed_lease",
    ),
    CheckConstraint(
        "(status IN ('processed','rejected')) = (completed_at IS NOT NULL)",
        name="completion",
    ),
)
Index(
    "ix_collaboration_inbox_recipient_status_available",
    COLLABORATION_INBOX.c.recipient_tenant_id,
    COLLABORATION_INBOX.c.status,
    COLLABORATION_INBOX.c.available_at,
)

GOVERNANCE_COMMANDS = Table(
    "governance_commands",
    GOVERNANCE_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("program_id", String(128), nullable=False),
    Column("command_type", String(128), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_version", BigInteger, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("length(request_digest) = 64", name="request_digest"),
    CheckConstraint(
        "status IN ('started', 'completed')",
        name="status",
    ),
    CheckConstraint(
        "(status = 'started' AND result_version IS NULL AND completed_at IS NULL) "
        "OR (status = 'completed' AND result_version IS NOT NULL "
        "AND completed_at IS NOT NULL)",
        name="completion",
    ),
)
Index(
    "ix_governance_commands_program_created",
    GOVERNANCE_COMMANDS.c.program_id,
    GOVERNANCE_COMMANDS.c.created_at,
)

ALL_GOVERNANCE_TABLES = (
    GOVERNANCE_PROGRAMS,
    GOVERNANCE_MEMBERS,
    GOVERNANCE_PLANS,
    GOVERNANCE_PLAN_DELIVERABLES,
    GOVERNANCE_PLAN_APPROVERS,
    GOVERNANCE_DISCUSSION_ITEMS,
    GOVERNANCE_ASSIGNMENTS,
    GOVERNANCE_ASSIGNMENT_DEPENDENCIES,
    GOVERNANCE_ASSIGNMENT_ARTIFACTS,
    GOVERNANCE_EVENTS,
    GOVERNANCE_OUTBOX,
    COLLABORATION_INBOX,
    GOVERNANCE_COMMANDS,
)


@dataclass(frozen=True, slots=True)
class GovernanceCommandClaim:
    status: ClaimStatus
    result_version: int | None


class SQLAlchemyGovernanceRepository:
    """Normalized governance aggregate repository with atomic audit/outbox writes."""

    def __init__(
        self,
        *,
        engine: Engine,
        audit_log: SQLAlchemyAuditLog,
    ) -> None:
        if audit_log.engine is not engine:
            raise ValueError("governance and audit must share one database engine")
        self.engine = engine
        self.audit_log = audit_log

    def create_schema(self) -> None:
        """Development bootstrap. Production uses reviewed Alembic migrations."""
        GOVERNANCE_METADATA.create_all(self.engine, tables=list(ALL_GOVERNANCE_TABLES))

    def claim_command_in_transaction(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        idempotency_key: str,
        program_id: str,
        command_type: str,
        request_digest: str,
    ) -> GovernanceCommandClaim:
        """Claim a command in the same transaction as its state transition."""
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or not program_id
            or len(program_id) > 128
            or not command_type
            or len(command_type) > 128
            or len(request_digest) != 64
        ):
            raise ValueError("governance command claim is invalid")
        self._set_tenant_context(connection, tenant_id)
        values = {
            "tenant_id": tenant_id,
            "idempotency_key": idempotency_key,
            "program_id": program_id,
            "command_type": command_type,
            "request_digest": request_digest,
            "status": "started",
            "result_version": None,
            "created_at": datetime.now(UTC),
            "completed_at": None,
        }
        if connection.dialect.name == "postgresql":
            statement = (
                postgresql_insert(GOVERNANCE_COMMANDS)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(GOVERNANCE_COMMANDS.c.idempotency_key)
            )
        elif connection.dialect.name == "sqlite":
            statement = (
                sqlite_insert(GOVERNANCE_COMMANDS)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(GOVERNANCE_COMMANDS.c.idempotency_key)
            )
        else:
            raise GovernanceError("governance persistence supports PostgreSQL and SQLite only")
        inserted = connection.execute(statement).scalar_one_or_none()
        if inserted is not None:
            return GovernanceCommandClaim(ClaimStatus.CLAIMED, None)

        existing = (
            connection.execute(
                select(GOVERNANCE_COMMANDS).where(
                    and_(
                        GOVERNANCE_COMMANDS.c.tenant_id == tenant_id,
                        GOVERNANCE_COMMANDS.c.idempotency_key == idempotency_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise GovernanceError("governance command claim disappeared")
        if (
            existing["program_id"] != program_id
            or existing["command_type"] != command_type
            or existing["request_digest"] != request_digest
        ):
            return GovernanceCommandClaim(ClaimStatus.CONFLICT, None)
        if existing["status"] != "completed" or existing["result_version"] is None:
            raise GovernanceError("incomplete governance command claim was committed")
        return GovernanceCommandClaim(
            ClaimStatus.DUPLICATE,
            int(existing["result_version"]),
        )

    def complete_command_in_transaction(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        idempotency_key: str,
        result_version: int,
    ) -> None:
        if result_version < 1:
            raise ValueError("governance command result version is invalid")
        self._set_tenant_context(connection, tenant_id)
        result = connection.execute(
            update(GOVERNANCE_COMMANDS)
            .where(
                and_(
                    GOVERNANCE_COMMANDS.c.tenant_id == tenant_id,
                    GOVERNANCE_COMMANDS.c.idempotency_key == idempotency_key,
                    GOVERNANCE_COMMANDS.c.status == "started",
                )
            )
            .values(
                status="completed",
                result_version=result_version,
                completed_at=datetime.now(UTC),
            )
        )
        if result.rowcount != 1:
            raise GovernanceError("governance command claim could not be completed")

    def create(
        self,
        *,
        board: GovernanceBoard,
        actor_id: str,
        events: tuple[AuditEvent, ...],
    ) -> int:
        actor = self._actor(board, actor_id)
        with self._tenant_transaction(actor.tenant_id) as connection:
            return self.create_in_transaction(
                connection,
                board=board,
                actor_id=actor_id,
                events=events,
            )

    def create_in_transaction(
        self,
        connection: Connection,
        *,
        board: GovernanceBoard,
        actor_id: str,
        events: tuple[AuditEvent, ...],
    ) -> int:
        """Create using a caller-owned transaction for atomic composition."""
        if board.aggregate_version != 0:
            raise GovernanceConflictError("new governance program must start at version zero")
        actor = self._actor(board, actor_id)
        self._set_tenant_context(connection, actor.tenant_id)
        now = datetime.now(UTC)
        normalized_events = tuple(event.normalized() for event in events)
        try:
            connection.execute(
                insert(GOVERNANCE_PROGRAMS).values(
                    program_id=board.program_id,
                    owner_tenant_id=board.owner_tenant_id,
                    title=board.title,
                    objective=board.objective,
                    classification=int(board.classification),
                    compartments=sorted(board.compartments),
                    participant_tenant_ids=sorted(board.participant_tenant_ids),
                    aggregate_version=1,
                    last_event_sequence=len(normalized_events),
                    created_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._persist_state(
                connection,
                board=board,
                actor_id=actor_id,
                now=now,
            )
            self._persist_events(
                connection,
                board=board,
                events=normalized_events,
                starting_sequence=0,
                now=now,
            )
            self._append_audit(connection, normalized_events)
        except SQLAlchemyIntegrityError as exc:
            raise GovernanceConflictError(
                "governance program or child identifier already exists"
            ) from exc
        board.aggregate_version = 1
        return 1

    def load(
        self,
        *,
        tenant_id: str,
        program_id: str,
        audit: AuditSink,
        connection: Connection | None = None,
    ) -> GovernanceBoard | None:
        if connection is None:
            transaction = self._tenant_transaction(tenant_id)
        else:
            self._set_tenant_context(connection, tenant_id)
            transaction = nullcontext(connection)
        with transaction as active_connection:
            root = (
                active_connection.execute(
                    select(GOVERNANCE_PROGRAMS).where(
                        GOVERNANCE_PROGRAMS.c.program_id == program_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if root is None or tenant_id not in root["participant_tenant_ids"]:
                return None
            members = self._visible_rows(
                active_connection,
                GOVERNANCE_MEMBERS,
                program_id,
                tenant_id,
            )
            plans = self._visible_rows(
                active_connection,
                GOVERNANCE_PLANS,
                program_id,
                tenant_id,
            )
            deliverables = self._visible_rows(
                active_connection,
                GOVERNANCE_PLAN_DELIVERABLES,
                program_id,
                tenant_id,
            )
            approvers = self._visible_rows(
                active_connection,
                GOVERNANCE_PLAN_APPROVERS,
                program_id,
                tenant_id,
            )
            discussions = self._visible_rows(
                active_connection,
                GOVERNANCE_DISCUSSION_ITEMS,
                program_id,
                tenant_id,
            )
            assignments = self._visible_rows(
                active_connection,
                GOVERNANCE_ASSIGNMENTS,
                program_id,
                tenant_id,
            )
            dependencies = self._visible_rows(
                active_connection,
                GOVERNANCE_ASSIGNMENT_DEPENDENCIES,
                program_id,
                tenant_id,
            )
            artifacts = self._visible_rows(
                active_connection,
                GOVERNANCE_ASSIGNMENT_ARTIFACTS,
                program_id,
                tenant_id,
            )

        board = GovernanceBoard(
            program_id=root["program_id"],
            owner_tenant_id=root["owner_tenant_id"],
            title=root["title"],
            objective=root["objective"],
            classification=Classification(root["classification"]),
            compartments=frozenset(root["compartments"]),
            audit=audit,
            aggregate_version=int(root["aggregate_version"]),
        )
        board.members = {
            row["principal_id"]: BoardMember(
                principal_id=row["principal_id"],
                tenant_id=row["tenant_id"],
                role=CollaborationRole(row["role"]),
            )
            for row in members
        }

        deliverables_by_plan: dict[str, list[tuple[int, str]]] = {}
        for row in deliverables:
            deliverables_by_plan.setdefault(row["plan_id"], []).append(
                (row["position"], row["content"])
            )
        approvers_by_plan: dict[str, set[str]] = {}
        approvals_by_plan: dict[str, dict[str, str]] = {}
        for row in approvers:
            approvers_by_plan.setdefault(row["plan_id"], set()).add(row["principal_id"])
            if row["approved_digest"] is not None:
                approvals_by_plan.setdefault(row["plan_id"], {})[row["principal_id"]] = row[
                    "approved_digest"
                ]

        board.plans = {}
        for row in plans:
            plan_id = row["plan_id"]
            plan = PlanRecord(
                plan_id=plan_id,
                program_id=program_id,
                version=row["version"],
                title=row["title"],
                objective=row["objective"],
                deliverables=tuple(
                    content for _, content in sorted(deliverables_by_plan.get(plan_id, []))
                ),
                lead_id=row["lead_id"],
                required_approvers=frozenset(approvers_by_plan.get(plan_id, set())),
                visible_to_tenants=frozenset(row["visible_to_tenants"]),
                content_digest=row["content_digest"],
                state=PlanState(row["state"]),
                approvals=approvals_by_plan.get(plan_id, {}),
            )
            board.plans[plan_id] = plan
        for row in discussions:
            plan = board.plans.get(row["plan_id"])
            if plan is None:
                raise GovernanceError("visible discussion references a hidden plan")
            plan.discussion_items.append(
                DiscussionItem(
                    item_id=row["item_id"],
                    plan_id=row["plan_id"],
                    author_id=row["author_id"],
                    kind=DiscussionKind(row["kind"]),
                    content=row["content"],
                    blocking=row["blocking"],
                    resolved=row["resolved"],
                    resolved_by=row["resolved_by"],
                    resolution=row["resolution"],
                )
            )

        dependencies_by_assignment: dict[str, list[str]] = {}
        for row in dependencies:
            dependencies_by_assignment.setdefault(row["assignment_id"], []).append(
                row["dependency_id"]
            )
        artifacts_by_assignment: dict[str, list[tuple[int, str]]] = {}
        for row in artifacts:
            artifacts_by_assignment.setdefault(row["assignment_id"], []).append(
                (row["position"], row["artifact_ref"])
            )
        board.assignments = {
            row["assignment_id"]: TaskAssignment(
                assignment_id=row["assignment_id"],
                plan_id=row["plan_id"],
                plan_digest=row["plan_digest"],
                title=row["title"],
                description=row["description"],
                deliverable_contract=row["deliverable_contract"],
                proposed_by=row["proposed_by"],
                assignee_id=row["assignee_id"],
                dependencies=tuple(
                    sorted(dependencies_by_assignment.get(row["assignment_id"], []))
                ),
                visible_to_tenants=frozenset(row["visible_to_tenants"]),
                state=AssignmentState(row["state"]),
                response_reason=row["response_reason"],
                artifact_refs=tuple(
                    value
                    for _, value in sorted(artifacts_by_assignment.get(row["assignment_id"], []))
                ),
                verification_note=row["verification_note"],
            )
            for row in assignments
        }
        return board

    def save(
        self,
        *,
        board: GovernanceBoard,
        expected_version: int,
        actor_id: str,
        events: tuple[AuditEvent, ...],
    ) -> int:
        if board.aggregate_version != expected_version:
            raise GovernanceConflictError("governance aggregate version is stale")
        if not events:
            raise GovernanceError("a governance state change requires an audit event")
        actor = self._actor(board, actor_id)
        normalized_events = tuple(event.normalized() for event in events)
        now = datetime.now(UTC)
        with self._tenant_transaction(actor.tenant_id) as connection:
            root = (
                connection.execute(
                    select(
                        GOVERNANCE_PROGRAMS.c.aggregate_version,
                        GOVERNANCE_PROGRAMS.c.last_event_sequence,
                    )
                    .where(GOVERNANCE_PROGRAMS.c.program_id == board.program_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if root is None:
                raise GovernanceError("governance program is absent or hidden")
            if int(root["aggregate_version"]) != expected_version:
                raise GovernanceConflictError("governance aggregate version is stale")
            new_version = expected_version + 1
            result = connection.execute(
                update(GOVERNANCE_PROGRAMS)
                .where(
                    and_(
                        GOVERNANCE_PROGRAMS.c.program_id == board.program_id,
                        GOVERNANCE_PROGRAMS.c.aggregate_version == expected_version,
                    )
                )
                .values(
                    title=board.title,
                    objective=board.objective,
                    classification=int(board.classification),
                    compartments=sorted(board.compartments),
                    participant_tenant_ids=sorted(board.participant_tenant_ids),
                    aggregate_version=new_version,
                    last_event_sequence=(int(root["last_event_sequence"]) + len(normalized_events)),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise GovernanceConflictError("governance aggregate version is stale")
            self._persist_state(
                connection,
                board=board,
                actor_id=actor_id,
                now=now,
            )
            self._persist_events(
                connection,
                board=board,
                events=normalized_events,
                starting_sequence=int(root["last_event_sequence"]),
                now=now,
            )
            self._append_audit(connection, normalized_events)
        board.aggregate_version = new_version
        return new_version

    def save_in_transaction(
        self,
        connection: Connection,
        *,
        board: GovernanceBoard,
        expected_version: int,
        actor_id: str,
        events: tuple[AuditEvent, ...],
    ) -> int:
        """Save using a caller-owned transaction for atomic composition."""
        if board.aggregate_version != expected_version:
            raise GovernanceConflictError("governance aggregate version is stale")
        if not events:
            raise GovernanceError("a governance state change requires an audit event")
        actor = self._actor(board, actor_id)
        self._set_tenant_context(connection, actor.tenant_id)
        normalized_events = tuple(event.normalized() for event in events)
        now = datetime.now(UTC)
        root = (
            connection.execute(
                select(
                    GOVERNANCE_PROGRAMS.c.aggregate_version,
                    GOVERNANCE_PROGRAMS.c.last_event_sequence,
                )
                .where(GOVERNANCE_PROGRAMS.c.program_id == board.program_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if root is None:
            raise GovernanceError("governance program is absent or hidden")
        if int(root["aggregate_version"]) != expected_version:
            raise GovernanceConflictError("governance aggregate version is stale")
        new_version = expected_version + 1
        result = connection.execute(
            update(GOVERNANCE_PROGRAMS)
            .where(
                and_(
                    GOVERNANCE_PROGRAMS.c.program_id == board.program_id,
                    GOVERNANCE_PROGRAMS.c.aggregate_version == expected_version,
                )
            )
            .values(
                title=board.title,
                objective=board.objective,
                classification=int(board.classification),
                compartments=sorted(board.compartments),
                participant_tenant_ids=sorted(board.participant_tenant_ids),
                aggregate_version=new_version,
                last_event_sequence=(int(root["last_event_sequence"]) + len(normalized_events)),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise GovernanceConflictError("governance aggregate version is stale")
        self._persist_state(
            connection,
            board=board,
            actor_id=actor_id,
            now=now,
        )
        self._persist_events(
            connection,
            board=board,
            events=normalized_events,
            starting_sequence=int(root["last_event_sequence"]),
            now=now,
        )
        self._append_audit(connection, normalized_events)
        board.aggregate_version = new_version
        return new_version

    def _persist_state(
        self,
        connection: Connection,
        *,
        board: GovernanceBoard,
        actor_id: str,
        now: datetime,
    ) -> None:
        participant_tenants = sorted(board.participant_tenant_ids)
        for member in board.members.values():
            self._upsert(
                connection,
                GOVERNANCE_MEMBERS,
                {
                    "program_id": board.program_id,
                    "principal_id": member.principal_id,
                    "tenant_id": member.tenant_id,
                    "role": member.role.value,
                    "added_by": actor_id,
                    "visible_to_tenants": participant_tenants,
                    "joined_at": now,
                },
                key_columns=("program_id", "principal_id"),
                update_columns=("role", "visible_to_tenants"),
            )
        for plan in board.plans.values():
            visibility = sorted(plan.visible_to_tenants)
            self._upsert(
                connection,
                GOVERNANCE_PLANS,
                {
                    "program_id": board.program_id,
                    "plan_id": plan.plan_id,
                    "version": plan.version,
                    "title": plan.title,
                    "objective": plan.objective,
                    "lead_id": plan.lead_id,
                    "content_digest": plan.content_digest,
                    "state": plan.state.value,
                    "visible_to_tenants": visibility,
                    "created_at": now,
                    "updated_at": now,
                },
                key_columns=("program_id", "plan_id"),
                update_columns=("state", "updated_at"),
            )
            for position, content in enumerate(plan.deliverables):
                self._insert_once(
                    connection,
                    GOVERNANCE_PLAN_DELIVERABLES,
                    {
                        "program_id": board.program_id,
                        "plan_id": plan.plan_id,
                        "position": position,
                        "content": content,
                        "visible_to_tenants": visibility,
                    },
                    key_columns=("program_id", "plan_id", "position"),
                )
            for principal_id in plan.required_approvers:
                approved_digest = plan.approvals.get(principal_id)
                self._upsert(
                    connection,
                    GOVERNANCE_PLAN_APPROVERS,
                    {
                        "program_id": board.program_id,
                        "plan_id": plan.plan_id,
                        "principal_id": principal_id,
                        "approved_digest": approved_digest,
                        "approved_at": now if approved_digest else None,
                        "visible_to_tenants": visibility,
                    },
                    key_columns=("program_id", "plan_id", "principal_id"),
                    update_columns=("approved_digest", "approved_at"),
                )
            for item in plan.discussion_items:
                self._upsert(
                    connection,
                    GOVERNANCE_DISCUSSION_ITEMS,
                    {
                        "program_id": board.program_id,
                        "item_id": item.item_id,
                        "plan_id": plan.plan_id,
                        "author_id": item.author_id,
                        "kind": item.kind.value,
                        "content": item.content,
                        "blocking": item.blocking,
                        "resolved": item.resolved,
                        "resolved_by": item.resolved_by,
                        "resolution": item.resolution,
                        "visible_to_tenants": visibility,
                        "created_at": now,
                        "resolved_at": now if item.resolved else None,
                    },
                    key_columns=("program_id", "item_id"),
                    update_columns=(
                        "resolved",
                        "resolved_by",
                        "resolution",
                        "resolved_at",
                    ),
                )
        for assignment in board.assignments.values():
            visibility = sorted(assignment.visible_to_tenants)
            self._upsert(
                connection,
                GOVERNANCE_ASSIGNMENTS,
                {
                    "program_id": board.program_id,
                    "assignment_id": assignment.assignment_id,
                    "plan_id": assignment.plan_id,
                    "plan_digest": assignment.plan_digest,
                    "title": assignment.title,
                    "description": assignment.description,
                    "deliverable_contract": assignment.deliverable_contract,
                    "proposed_by": assignment.proposed_by,
                    "assignee_id": assignment.assignee_id,
                    "state": assignment.state.value,
                    "response_reason": assignment.response_reason,
                    "verification_note": assignment.verification_note,
                    "visible_to_tenants": visibility,
                    "created_at": now,
                    "updated_at": now,
                },
                key_columns=("program_id", "assignment_id"),
                update_columns=(
                    "state",
                    "response_reason",
                    "verification_note",
                    "updated_at",
                ),
            )
            for dependency_id in assignment.dependencies:
                self._insert_once(
                    connection,
                    GOVERNANCE_ASSIGNMENT_DEPENDENCIES,
                    {
                        "program_id": board.program_id,
                        "assignment_id": assignment.assignment_id,
                        "dependency_id": dependency_id,
                        "visible_to_tenants": visibility,
                    },
                    key_columns=("program_id", "assignment_id", "dependency_id"),
                )
            for position, artifact_ref in enumerate(assignment.artifact_refs):
                self._insert_once(
                    connection,
                    GOVERNANCE_ASSIGNMENT_ARTIFACTS,
                    {
                        "program_id": board.program_id,
                        "assignment_id": assignment.assignment_id,
                        "position": position,
                        "artifact_ref": artifact_ref,
                        "visible_to_tenants": visibility,
                    },
                    key_columns=("program_id", "assignment_id", "position"),
                )

    def _persist_events(
        self,
        connection: Connection,
        *,
        board: GovernanceBoard,
        events: tuple[AuditEvent, ...],
        starting_sequence: int,
        now: datetime,
    ) -> None:
        for offset, event in enumerate(events, start=1):
            sequence = starting_sequence + offset
            visibility = sorted(self._event_visibility(board, event))
            subject_id = str(event.details.get("subject_id", board.program_id))
            connection.execute(
                insert(GOVERNANCE_EVENTS).values(
                    program_id=board.program_id,
                    sequence=sequence,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    actor_id=event.actor_id,
                    actor_tenant_id=event.tenant_id,
                    subject_id=subject_id,
                    payload=event.details,
                    visible_to_tenants=visibility,
                    occurred_at=datetime.fromisoformat(event.occurred_at).astimezone(UTC),
                    audit_event_id=event.event_id,
                )
            )
            for recipient_tenant_id in visibility:
                message_id = hashlib.sha256(
                    (f"{board.program_id}\n{event.event_id}\n" f"{recipient_tenant_id}").encode(
                        "utf-8"
                    )
                ).hexdigest()
                connection.execute(
                    insert(GOVERNANCE_OUTBOX).values(
                        message_id=message_id,
                        program_id=board.program_id,
                        event_sequence=sequence,
                        producer_tenant_id=event.tenant_id,
                        recipient_tenant_id=recipient_tenant_id,
                        status="pending",
                        attempt_count=0,
                        max_attempts=10,
                        available_at=now,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        envelope=None,
                        envelope_digest=None,
                        last_error_code=None,
                        published_at=None,
                        created_at=now,
                    )
                )

    def _append_audit(
        self,
        connection: Connection,
        events: tuple[AuditEvent, ...],
    ) -> None:
        for event in events:
            self.audit_log.append_in_transaction(connection, event)

    @staticmethod
    def _event_visibility(
        board: GovernanceBoard,
        event: AuditEvent,
    ) -> frozenset[str]:
        subject_id = str(event.details.get("subject_id", ""))
        plan = board.plans.get(subject_id)
        if plan is not None:
            return plan.visible_to_tenants
        assignment = board.assignments.get(subject_id)
        if assignment is not None:
            return assignment.visible_to_tenants
        for candidate in board.plans.values():
            if any(item.item_id == subject_id for item in candidate.discussion_items):
                return candidate.visible_to_tenants
        return board.participant_tenant_ids

    @staticmethod
    def _actor(board: GovernanceBoard, actor_id: str) -> BoardMember:
        actor = board.members.get(actor_id)
        if actor is None:
            raise GovernanceError("actor is not a governance board member")
        return actor

    @staticmethod
    def _visible_rows(
        connection: Connection,
        table: Table,
        program_id: str,
        tenant_id: str,
    ) -> list:
        rows = (
            connection.execute(select(table).where(table.c.program_id == program_id))
            .mappings()
            .all()
        )
        return [row for row in rows if tenant_id in row["visible_to_tenants"]]

    @staticmethod
    def _upsert(
        connection: Connection,
        table: Table,
        values: dict,
        *,
        key_columns: tuple[str, ...],
        update_columns: tuple[str, ...],
    ) -> None:
        if connection.dialect.name == "postgresql":
            base = postgresql_insert(table).values(**values)
        elif connection.dialect.name == "sqlite":
            base = sqlite_insert(table).values(**values)
        else:
            raise GovernanceError("governance persistence supports PostgreSQL and SQLite only")
        statement = base.on_conflict_do_update(
            index_elements=list(key_columns),
            set_={column: getattr(base.excluded, column) for column in update_columns},
        )
        connection.execute(statement)

    @staticmethod
    def _insert_once(
        connection: Connection,
        table: Table,
        values: dict,
        *,
        key_columns: tuple[str, ...],
    ) -> None:
        if connection.dialect.name == "postgresql":
            statement = (
                postgresql_insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(key_columns))
            )
        elif connection.dialect.name == "sqlite":
            statement = (
                sqlite_insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(key_columns))
            )
        else:
            raise GovernanceError("governance persistence supports PostgreSQL and SQLite only")
        connection.execute(statement)

    @contextmanager
    def _tenant_transaction(self, tenant_id: str) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            yield connection

    def list_program_summaries(
        self, *, tenant_id: str, principal_id: str, limit: int = 100
    ) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 200:
            raise ValueError("program list limit is invalid")
        with self._tenant_transaction(tenant_id) as connection:
            rows = connection.execute(
                select(
                    GOVERNANCE_PROGRAMS.c.program_id,
                    GOVERNANCE_PROGRAMS.c.owner_tenant_id,
                    GOVERNANCE_PROGRAMS.c.title,
                    GOVERNANCE_PROGRAMS.c.classification,
                    GOVERNANCE_PROGRAMS.c.aggregate_version,
                    GOVERNANCE_PROGRAMS.c.updated_at,
                    GOVERNANCE_MEMBERS.c.role,
                )
                .join(
                    GOVERNANCE_MEMBERS,
                    GOVERNANCE_MEMBERS.c.program_id == GOVERNANCE_PROGRAMS.c.program_id,
                )
                .where(
                    and_(
                        GOVERNANCE_MEMBERS.c.tenant_id == tenant_id,
                        GOVERNANCE_MEMBERS.c.principal_id == principal_id,
                    )
                )
                .order_by(
                    GOVERNANCE_PROGRAMS.c.updated_at.desc(),
                    GOVERNANCE_PROGRAMS.c.program_id,
                )
                .limit(limit)
            ).mappings().all()
        return tuple(dict(row) for row in rows)

    def list_assignment_summaries(
        self, *, tenant_id: str, principal_id: str, limit: int = 200
    ) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("assignment list limit is invalid")
        with self._tenant_transaction(tenant_id) as connection:
            rows = connection.execute(
                select(
                    GOVERNANCE_ASSIGNMENTS.c.program_id,
                    GOVERNANCE_ASSIGNMENTS.c.assignment_id,
                    GOVERNANCE_ASSIGNMENTS.c.plan_id,
                    GOVERNANCE_ASSIGNMENTS.c.title,
                    GOVERNANCE_ASSIGNMENTS.c.description,
                    GOVERNANCE_ASSIGNMENTS.c.deliverable_contract,
                    GOVERNANCE_ASSIGNMENTS.c.proposed_by,
                    GOVERNANCE_ASSIGNMENTS.c.assignee_id,
                    GOVERNANCE_ASSIGNMENTS.c.state,
                    GOVERNANCE_ASSIGNMENTS.c.visible_to_tenants,
                    GOVERNANCE_ASSIGNMENTS.c.updated_at,
                    GOVERNANCE_PROGRAMS.c.title.label("program_title"),
                    GOVERNANCE_PROGRAMS.c.aggregate_version,
                )
                .join(
                    GOVERNANCE_PROGRAMS,
                    GOVERNANCE_PROGRAMS.c.program_id == GOVERNANCE_ASSIGNMENTS.c.program_id,
                )
                .where(
                    and_(
                        (GOVERNANCE_ASSIGNMENTS.c.assignee_id == principal_id)
                        | (GOVERNANCE_ASSIGNMENTS.c.proposed_by == principal_id),
                    )
                )
                .order_by(
                    GOVERNANCE_ASSIGNMENTS.c.updated_at.desc(),
                    GOVERNANCE_ASSIGNMENTS.c.assignment_id,
                )
                .limit(limit)
            ).mappings().all()
        result = []
        for row in rows:
            if tenant_id not in row["visible_to_tenants"]:
                continue
            item = dict(row)
            item.pop("visible_to_tenants", None)
            result.append(item)
        return tuple(result)

    @staticmethod
    def _set_tenant_context(connection: Connection, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("tenant_id is invalid")
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
                {"tenant_id": tenant_id},
            )
        elif connection.dialect.name != "sqlite":
            raise GovernanceError("governance persistence supports PostgreSQL and SQLite only")
