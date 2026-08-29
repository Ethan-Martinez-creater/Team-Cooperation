from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC

from sqlalchemy import (
    JSON,
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
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from ..errors import GovernanceConflictError, ResourceNotFound
from ..product.repository import PROJECTS
from .budget import (
    ProjectExecutionReservation,
    ProjectExecutionReservationStatus,
    ProjectExecutionUsage,
)
from .commands import (
    ProjectOrchestrationDecision,
    ProjectOrchestrationDecisionStatus,
    ProjectProcessCommand,
    ProjectProcessCommandStatus,
    ProjectProcessCommandType,
    ProjectProcessOutboxEntry,
    ProjectProcessOutboxStatus,
)
from .gates import (
    ProjectGate,
    ProjectGateStatus,
    ProjectInputRequest,
    ProjectInputRequestStatus,
)
from .models import (
    ProjectProcess,
    ProjectProcessEvent,
    ProjectProcessPhase,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
)

PROJECT_PROCESS_METADATA = MetaData()
OBJECT = JSON().with_variant(JSONB(), "postgresql")

PROJECT_EXECUTION_POLICIES = Table(
    "project_execution_policies",
    PROJECT_PROCESS_METADATA,
    Column("policy_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("max_agent_runs", Integer, nullable=False),
    Column("max_total_tokens", Integer, nullable=False),
    Column("max_model_cost_microusd", Integer, nullable=False),
    Column("max_replans", Integer, nullable=False),
    Column("max_generated_tasks", Integer, nullable=False),
    Column("max_active_agent_runs", Integer, nullable=False),
    Column("max_active_runs_per_team", Integer, nullable=False),
    Column("max_specialist_depth", Integer, nullable=False),
    Column("max_specialist_runs_per_task", Integer, nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("version >= 1", name="project_policy_version"),
    CheckConstraint(
        "max_agent_runs >= 0 AND max_total_tokens >= 0 AND "
        "max_model_cost_microusd >= 0 AND max_replans >= 0 AND "
        "max_generated_tasks >= 0 AND max_active_agent_runs >= 0 AND "
        "max_active_runs_per_team >= 0 AND max_specialist_depth >= 0 AND "
        "max_specialist_runs_per_task >= 0",
        name="project_policy_nonnegative_limits",
    ),
)

PROJECT_PROCESSES = Table(
    "project_processes",
    PROJECT_PROCESS_METADATA,
    Column("process_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("wait_reason", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("root_goal_id", String(128), nullable=True),
    Column("active_plan_id", String(128), nullable=True),
    Column("execution_policy_id", String(128), nullable=False),
    Column("execution_policy_version", Integer, nullable=False),
    Column("started_by", String(128), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_event_sequence", Integer, nullable=False),
    Column("last_orchestration_sequence", Integer, nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("version >= 1", name="project_process_version"),
    CheckConstraint("last_event_sequence >= 0", name="project_process_event_sequence"),
    CheckConstraint(
        "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR "
        "(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
        name="project_process_lease_complete",
    ),
    CheckConstraint(
        "phase IN ('INTAKE','ANALYSIS','PLANNING','EXECUTION','INTEGRATION',"
        "'VERIFICATION','DELIVERY','TERMINAL')",
        name="project_process_phase",
    ),
    CheckConstraint(
        "status IN ('READY','RUNNING','WAITING','BLOCKED','COMPLETED','FAILED','CANCELLED')",
        name="project_process_status",
    ),
    CheckConstraint(
        "wait_reason IN ('NONE','HUMAN_INPUT','HUMAN_APPROVAL','TEAM_RESPONSE',"
        "'AGENT_RUN','TOOL_JOB','DEPENDENCY','VERIFICATION','SCHEDULE')",
        name="project_process_wait_reason",
    ),
    CheckConstraint(
        "((status IN ('WAITING','BLOCKED')) AND wait_reason <> 'NONE') OR "
        "((status NOT IN ('WAITING','BLOCKED')) AND wait_reason = 'NONE')",
        name="project_process_wait_consistency",
    ),
    CheckConstraint(
        "(phase = 'TERMINAL' AND status IN ('COMPLETED','FAILED','CANCELLED')) OR "
        "(phase <> 'TERMINAL' AND status NOT IN ('COMPLETED','FAILED','CANCELLED'))",
        name="project_process_terminal_consistency",
    ),
    UniqueConstraint("process_id", "project_id", name="uq_project_process_project"),
    ForeignKeyConstraint(
        ["execution_policy_id", "execution_policy_version"],
        ["project_execution_policies.policy_id", "project_execution_policies.version"],
    ),
)
Index("ix_project_process_project", PROJECT_PROCESSES.c.project_id)
_ACTIVE_PROCESS = PROJECT_PROCESSES.c.status.not_in(["COMPLETED", "FAILED", "CANCELLED"])
Index(
    "uq_project_process_one_active",
    PROJECT_PROCESSES.c.project_id,
    unique=True,
    sqlite_where=_ACTIVE_PROCESS,
    postgresql_where=_ACTIVE_PROCESS,
)

PROJECT_PLANNER_INTENTS = Table(
    "project_planner_intents",
    PROJECT_PROCESS_METADATA,
    Column("planner_intent_id", String(128), primary_key=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("owner_team_id", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("based_on_process_version", Integer, nullable=False),
    Column("based_on_event_sequence", Integer, nullable=False),
    Column("graph_snapshot_digest", String(71), nullable=False),
    Column("status", String(16), nullable=False),
    Column("run_id", String(128), nullable=True, unique=True),
    Column("decision_id", String(128), nullable=True, unique=True),
    Column("error_code", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("projected_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    CheckConstraint(
        "based_on_process_version >= 1", name="planner_intent_process_version"
    ),
    CheckConstraint(
        "based_on_event_sequence >= 0", name="planner_intent_event_sequence"
    ),
    CheckConstraint(
        "status IN ('PENDING','RUNNING','PROJECTED','STALE','REJECTED','FAILED','CANCELLED')",
        name="planner_intent_status",
    ),
    CheckConstraint(
        "(status IN ('PENDING','RUNNING') AND projected_at IS NULL) OR "
        "(status NOT IN ('PENDING','RUNNING') AND projected_at IS NOT NULL)",
        name="planner_intent_terminal_time",
    ),
)
Index(
    "ix_project_planner_intent_status",
    PROJECT_PLANNER_INTENTS.c.status,
    PROJECT_PLANNER_INTENTS.c.created_at,
)

PROJECT_EXECUTION_USAGE = Table(
    "project_execution_usage",
    PROJECT_PROCESS_METADATA,
    Column("process_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("agent_runs_started", Integer, nullable=False, default=0),
    Column("agent_runs_completed", Integer, nullable=False, default=0),
    Column("total_tokens", Integer, nullable=False, default=0),
    Column("model_cost_microusd", Integer, nullable=False, default=0),
    Column("replan_count", Integer, nullable=False, default=0),
    Column("generated_task_count", Integer, nullable=False, default=0),
    Column("active_agent_runs", Integer, nullable=False, default=0),
    Column("version", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    CheckConstraint(
        "agent_runs_started >= 0 AND agent_runs_completed >= 0 AND "
        "total_tokens >= 0 AND model_cost_microusd >= 0 AND replan_count >= 0 AND "
        "generated_task_count >= 0 AND active_agent_runs >= 0 AND version >= 1",
        name="project_execution_usage_nonnegative",
    ),
    CheckConstraint(
        "agent_runs_completed <= agent_runs_started",
        name="project_execution_usage_completed_bound",
    ),
    CheckConstraint(
        "active_agent_runs <= agent_runs_started - agent_runs_completed",
        name="project_execution_usage_active_bound",
    ),
)

PROJECT_EXECUTION_RESERVATIONS = Table(
    "project_execution_reservations",
    PROJECT_PROCESS_METADATA,
    Column("reservation_id", String(128), primary_key=True),
    Column("reservation_key", String(256), nullable=False, unique=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("work_node_id", String(128), nullable=False),
    Column("team_id", String(128), nullable=False),
    Column("policy_id", String(128), nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("execution_attempt", Integer, nullable=False),
    Column("specialist_depth", Integer, nullable=False, default=0),
    Column("reserved_tokens", Integer, nullable=False),
    Column("reserved_model_cost_microusd", Integer, nullable=False),
    Column("agent_run_id", String(128), nullable=True, unique=True),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True), nullable=True),
    Column("terminal_event_id", String(128), nullable=True, unique=True),
    Column("terminal_total_tokens", Integer, nullable=True),
    Column("terminal_model_cost_microusd", Integer, nullable=True),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    ForeignKeyConstraint(
        ["policy_id", "policy_version"],
        ["project_execution_policies.policy_id", "project_execution_policies.version"],
    ),
    CheckConstraint("execution_attempt >= 1", name="project_reservation_attempt"),
    CheckConstraint("specialist_depth >= 0", name="project_reservation_depth"),
    CheckConstraint(
        "reserved_tokens >= 0 AND reserved_model_cost_microusd >= 0",
        name="project_reservation_reserved_budget",
    ),
    CheckConstraint(
        "status IN ('RESERVED','SETTLED','RELEASED')",
        name="project_reservation_status",
    ),
    CheckConstraint(
        "(status = 'RESERVED' AND settled_at IS NULL) OR "
        "(status IN ('SETTLED','RELEASED') AND settled_at IS NOT NULL)",
        name="project_reservation_settlement",
    ),
    CheckConstraint(
        "terminal_total_tokens IS NULL OR terminal_total_tokens >= 0",
        name="project_reservation_tokens",
    ),
    CheckConstraint(
        "terminal_model_cost_microusd IS NULL OR terminal_model_cost_microusd >= 0",
        name="project_reservation_cost",
    ),
)
Index(
    "ix_project_reservation_active_team",
    PROJECT_EXECUTION_RESERVATIONS.c.process_id,
    PROJECT_EXECUTION_RESERVATIONS.c.team_id,
    sqlite_where=PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
    postgresql_where=PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
)

PROJECT_INPUT_REQUESTS = Table(
    "project_input_requests",
    PROJECT_PROCESS_METADATA,
    Column("request_id", String(128), primary_key=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("work_node_id", String(128), nullable=True),
    Column("requested_by_run_id", String(128), nullable=True),
    Column("requested_by_agent_id", String(128), nullable=False),
    Column("question", Text, nullable=False),
    Column("input_schema_json", OBJECT, nullable=False),
    Column("context_projection_json", OBJECT, nullable=False),
    Column("status", String(16), nullable=False),
    Column("response_json", OBJECT, nullable=True),
    Column("version", Integer, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("answered_by", String(128), nullable=True),
    Column("answered_at", DateTime(timezone=True), nullable=True),
    Column("resolution_idempotency_key", String(256), nullable=True, unique=True),
    Column("resolution_event_id", String(128), nullable=True, unique=True),
    Column("resolution_sha256", String(64), nullable=True),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    CheckConstraint("status IN ('OPEN','ANSWERED','CANCELLED','EXPIRED')", name="project_input_status"),
    CheckConstraint("version >= 1", name="project_input_version"),
    CheckConstraint("length(question) > 0", name="project_input_question"),
    CheckConstraint(
        "(status = 'OPEN' AND answered_at IS NULL AND resolution_idempotency_key IS NULL "
        "AND resolution_event_id IS NULL) OR "
        "(status <> 'OPEN' AND answered_at IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
        "AND resolution_event_id IS NOT NULL)",
        name="project_input_resolution",
    ),
)
Index("ix_project_input_open", PROJECT_INPUT_REQUESTS.c.process_id, PROJECT_INPUT_REQUESTS.c.status)

PROJECT_GATES = Table(
    "project_gates",
    PROJECT_PROCESS_METADATA,
    Column("gate_id", String(128), primary_key=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("gate_type", String(64), nullable=False),
    Column("subject_type", String(64), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("required_roles_json", OBJECT, nullable=False),
    Column("allowed_decisions_json", OBJECT, nullable=False),
    Column("status", String(16), nullable=False),
    Column("decision", String(64), nullable=True),
    Column("reason", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("resolution_idempotency_key", String(256), nullable=True, unique=True),
    Column("resolution_event_id", String(128), nullable=True, unique=True),
    Column("resolution_sha256", String(64), nullable=True),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    CheckConstraint("status IN ('OPEN','DECIDED','CANCELLED','EXPIRED')", name="project_gate_status"),
    CheckConstraint("version >= 1", name="project_gate_version"),
    CheckConstraint(
        "(status = 'OPEN' AND decided_at IS NULL AND resolution_idempotency_key IS NULL "
        "AND resolution_event_id IS NULL) OR "
        "(status <> 'OPEN' AND decided_at IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
        "AND resolution_event_id IS NOT NULL)",
        name="project_gate_resolution",
    ),
)
Index("ix_project_gate_open", PROJECT_GATES.c.process_id, PROJECT_GATES.c.status)

PROJECT_PROCESS_EVENTS = Table(
    "project_process_events",
    PROJECT_PROCESS_METADATA,
    Column("event_id", String(128), primary_key=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("transition_key", String(128), nullable=True),
    Column("schema_version", String(32), nullable=False),
    Column("subject_type", String(64), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("source_aggregate_version", Integer, nullable=True),
    Column("process_version_before", Integer, nullable=False),
    Column("process_version_after", Integer, nullable=True),
    Column("initiated_by", String(128), nullable=False),
    Column("executed_as", String(128), nullable=False),
    Column("correlation_id", String(128), nullable=False),
    Column("causation_id", String(128), nullable=True),
    Column("payload_json", OBJECT, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("process_id", "sequence", name="uq_project_process_event_sequence"),
    UniqueConstraint(
        "process_id", "idempotency_key", name="uq_project_process_event_idempotency"
    ),
    CheckConstraint("sequence >= 1", name="project_process_event_positive_sequence"),
    CheckConstraint("length(payload_sha256) = 64", name="project_process_event_digest"),
    CheckConstraint(
        "(process_version_after IS NULL AND transition_key IS NULL) OR "
        "(process_version_after = process_version_before + 1 AND transition_key IS NOT NULL)",
        name="project_process_event_transition",
    ),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
)
Index(
    "ix_project_process_events_project_sequence",
    PROJECT_PROCESS_EVENTS.c.project_id,
    PROJECT_PROCESS_EVENTS.c.sequence,
)

PROJECT_ORCHESTRATION_DECISIONS = Table(
    "project_orchestration_decisions",
    PROJECT_PROCESS_METADATA,
    Column("decision_id", String(128), primary_key=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("based_on_process_version", Integer, nullable=False),
    Column("based_on_event_sequence", Integer, nullable=False),
    Column("graph_snapshot_digest", String(71), nullable=False),
    Column("command_batch_digest", String(64), nullable=False),
    Column("decision_json", OBJECT, nullable=False),
    Column("decision_digest", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    CheckConstraint(
        "based_on_process_version >= 1",
        name="project_decision_process_version",
    ),
    CheckConstraint(
        "based_on_event_sequence >= 0",
        name="project_decision_event_sequence",
    ),
    CheckConstraint(
        "length(command_batch_digest) = 64",
        name="project_decision_batch_digest",
    ),
    CheckConstraint(
        "length(decision_digest) = 64",
        name="project_decision_digest",
    ),
    CheckConstraint(
        "status IN ('PENDING','APPLIED','STALE','REJECTED')",
        name="project_decision_status",
    ),
    CheckConstraint(
        "(status = 'PENDING' AND applied_at IS NULL) OR "
        "(status <> 'PENDING' AND applied_at IS NOT NULL)",
        name="project_decision_terminal_time",
    ),
)
Index(
    "ix_project_decision_process_status",
    PROJECT_ORCHESTRATION_DECISIONS.c.process_id,
    PROJECT_ORCHESTRATION_DECISIONS.c.status,
)

PROJECT_PROCESS_COMMANDS = Table(
    "project_process_commands",
    PROJECT_PROCESS_METADATA,
    Column("command_id", String(128), primary_key=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("decision_id", String(128), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("request_json", OBJECT, nullable=False),
    Column("based_on_process_version", Integer, nullable=False),
    Column("based_on_event_sequence", Integer, nullable=False),
    Column("graph_snapshot_digest", String(71), nullable=False),
    Column("status", String(16), nullable=False),
    Column("result_subject_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    UniqueConstraint("process_id", "decision_id", "command_id", name="uq_process_command_decision"),
    CheckConstraint("length(request_digest) = 64", name="project_command_digest"),
    CheckConstraint("based_on_process_version >= 1", name="project_command_process_version"),
    CheckConstraint("based_on_event_sequence >= 0", name="project_command_event_sequence"),
    CheckConstraint(
        "command_type IN ('propose_task','propose_dependency','propose_risk',"
        "'propose_decision','request_rework','request_replan','request_human_input',"
        "'request_human_gate')",
        name="project_command_type",
    ),
    CheckConstraint(
        "status IN ('PENDING','APPLIED','REJECTED','STALE')",
        name="project_command_status",
    ),
    CheckConstraint(
        "(status = 'PENDING' AND applied_at IS NULL) OR "
        "(status <> 'PENDING' AND applied_at IS NOT NULL)",
        name="project_command_terminal_time",
    ),
)
Index("ix_project_command_process_status", PROJECT_PROCESS_COMMANDS.c.process_id, PROJECT_PROCESS_COMMANDS.c.status)

PROJECT_PROCESS_OUTBOX = Table(
    "project_process_outbox",
    PROJECT_PROCESS_METADATA,
    Column("outbox_id", String(160), primary_key=True),
    Column("event_id", String(128), nullable=False, unique=True),
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    ForeignKeyConstraint(["event_id"], ["project_process_events.event_id"]),
    ForeignKeyConstraint(
        ["process_id", "project_id"],
        ["project_processes.process_id", "project_processes.project_id"],
    ),
    CheckConstraint("status IN ('PENDING','PUBLISHING','PUBLISHED','FAILED')", name="project_outbox_status"),
    CheckConstraint("attempt_count >= 0", name="project_outbox_attempt_count"),
    CheckConstraint(
        "(status = 'PUBLISHING' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status <> 'PUBLISHING' AND lease_owner IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL)",
        name="project_outbox_lease",
    ),
    CheckConstraint(
        "(status = 'PUBLISHED' AND published_at IS NOT NULL) OR "
        "(status <> 'PUBLISHED' AND published_at IS NULL)",
        name="project_outbox_publish_time",
    ),
)
Index("ix_project_outbox_pending", PROJECT_PROCESS_OUTBOX.c.status, PROJECT_PROCESS_OUTBOX.c.available_at)


class SQLAlchemyProjectProcessRepository:
    def __init__(
        self,
        engine: Engine,
        *,
        event_listener: Callable[[Connection, ProjectProcessEvent], None] | None = None,
    ) -> None:
        self.engine = engine
        self.event_listener = event_listener

    def set_event_listener(
        self,
        event_listener: Callable[[Connection, ProjectProcessEvent], None] | None,
    ) -> None:
        self.event_listener = event_listener

    def create_schema(self) -> None:
        PROJECT_PROCESS_METADATA.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    @staticmethod
    def require_project(connection: Connection, project_id: str) -> None:
        found = connection.execute(
            select(PROJECTS.c.project_id).where(PROJECTS.c.project_id == project_id)
        ).scalar_one_or_none()
        if found is None:
            raise ResourceNotFound("project is unavailable")

    @staticmethod
    def process(connection: Connection, process_id: str) -> ProjectProcess:
        row = (
            connection.execute(
                select(PROJECT_PROCESSES).where(PROJECT_PROCESSES.c.process_id == process_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project process is unavailable")
        return SQLAlchemyProjectProcessRepository._process(row)

    def append_event(self, connection: Connection, values: dict) -> ProjectProcessEvent:
        existing = (
            connection.execute(
                select(PROJECT_PROCESS_EVENTS).where(
                    (PROJECT_PROCESS_EVENTS.c.event_id == values["event_id"])
                    | and_(
                        PROJECT_PROCESS_EVENTS.c.process_id == values["process_id"],
                        PROJECT_PROCESS_EVENTS.c.idempotency_key
                        == values["idempotency_key"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["payload_sha256"] != values["payload_sha256"] or any(
                existing[field] != values[field]
                for field in (
                    "process_id",
                    "project_id",
                    "event_type",
                    "idempotency_key",
                    "transition_key",
                    "subject_type",
                    "subject_id",
                )
            ):
                raise GovernanceConflictError(
                    "project process event id was reused with different content"
                )
            return SQLAlchemyProjectProcessRepository._event(existing)
        connection.execute(PROJECT_PROCESS_EVENTS.insert().values(**values))
        connection.execute(
            PROJECT_PROCESS_OUTBOX.insert().values(
                outbox_id=f"outbox:{values['event_id']}",
                event_id=values["event_id"],
                process_id=values["process_id"],
                project_id=values["project_id"],
                status=ProjectProcessOutboxStatus.PENDING.value,
                attempt_count=0,
                available_at=values["occurred_at"],
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                published_at=None,
                last_error=None,
            )
        )
        event = SQLAlchemyProjectProcessRepository._event(values)
        if self.event_listener is not None:
            self.event_listener(connection, event)
        return event

    @staticmethod
    def event(connection: Connection, event_id: str) -> ProjectProcessEvent | None:
        row = (
            connection.execute(
                select(PROJECT_PROCESS_EVENTS).where(PROJECT_PROCESS_EVENTS.c.event_id == event_id)
            )
            .mappings()
            .one_or_none()
        )
        return SQLAlchemyProjectProcessRepository._event(row) if row else None

    @staticmethod
    def events(connection: Connection, process_id: str) -> tuple[ProjectProcessEvent, ...]:
        rows = (
            connection.execute(
                select(PROJECT_PROCESS_EVENTS)
                .where(PROJECT_PROCESS_EVENTS.c.process_id == process_id)
                .order_by(PROJECT_PROCESS_EVENTS.c.sequence)
            )
            .mappings()
            .all()
        )
        return tuple(SQLAlchemyProjectProcessRepository._event(row) for row in rows)

    @staticmethod
    def usage(connection: Connection, process_id: str) -> ProjectExecutionUsage:
        row = (
            connection.execute(
                select(PROJECT_EXECUTION_USAGE).where(
                    PROJECT_EXECUTION_USAGE.c.process_id == process_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project execution usage is unavailable")
        return ProjectExecutionUsage(
            row["process_id"],
            row["project_id"],
            row["agent_runs_started"],
            row["agent_runs_completed"],
            row["total_tokens"],
            row["model_cost_microusd"],
            row["replan_count"],
            row["generated_task_count"],
            row["active_agent_runs"],
            row["version"],
        )

    @staticmethod
    def reservation(connection: Connection, reservation_id: str) -> ProjectExecutionReservation:
        row = (
            connection.execute(
                select(PROJECT_EXECUTION_RESERVATIONS).where(
                    PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == reservation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project execution reservation is unavailable")
        return SQLAlchemyProjectProcessRepository._reservation(row)

    @staticmethod
    def input_request(connection: Connection, request_id: str) -> ProjectInputRequest:
        row = (
            connection.execute(
                select(PROJECT_INPUT_REQUESTS).where(PROJECT_INPUT_REQUESTS.c.request_id == request_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project input request is unavailable")
        return SQLAlchemyProjectProcessRepository._input_request(row)

    @staticmethod
    def gate(connection: Connection, gate_id: str) -> ProjectGate:
        row = (
            connection.execute(select(PROJECT_GATES).where(PROJECT_GATES.c.gate_id == gate_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project gate is unavailable")
        return SQLAlchemyProjectProcessRepository._gate(row)

    @staticmethod
    def command(connection: Connection, command_id: str) -> ProjectProcessCommand | None:
        row = (
            connection.execute(
                select(PROJECT_PROCESS_COMMANDS).where(
                    PROJECT_PROCESS_COMMANDS.c.command_id == command_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return SQLAlchemyProjectProcessRepository._command(row) if row else None

    @staticmethod
    def decision(connection: Connection, decision_id: str) -> ProjectOrchestrationDecision | None:
        row = (
            connection.execute(
                select(PROJECT_ORCHESTRATION_DECISIONS).where(
                    PROJECT_ORCHESTRATION_DECISIONS.c.decision_id == decision_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return SQLAlchemyProjectProcessRepository._decision(row) if row else None

    # The explicit name is useful at call sites where ``decision`` is also a
    # local variable, and keeps the repository API self-documenting.
    orchestration_decision = decision

    @staticmethod
    def decisions_for_process(
        connection: Connection, process_id: str
    ) -> tuple[ProjectOrchestrationDecision, ...]:
        rows = (
            connection.execute(
                select(PROJECT_ORCHESTRATION_DECISIONS)
                .where(PROJECT_ORCHESTRATION_DECISIONS.c.process_id == process_id)
                .order_by(
                    PROJECT_ORCHESTRATION_DECISIONS.c.created_at,
                    PROJECT_ORCHESTRATION_DECISIONS.c.decision_id,
                )
            )
            .mappings()
            .all()
        )
        return tuple(SQLAlchemyProjectProcessRepository._decision(row) for row in rows)

    @staticmethod
    def commands_for_decision(
        connection: Connection, decision_id: str
    ) -> tuple[ProjectProcessCommand, ...]:
        rows = (
            connection.execute(
                select(PROJECT_PROCESS_COMMANDS)
                .where(PROJECT_PROCESS_COMMANDS.c.decision_id == decision_id)
                .order_by(PROJECT_PROCESS_COMMANDS.c.command_id)
            )
            .mappings()
            .all()
        )
        return tuple(SQLAlchemyProjectProcessRepository._command(row) for row in rows)

    @staticmethod
    def outbox_entry(connection: Connection, outbox_id: str) -> ProjectProcessOutboxEntry | None:
        row = (
            connection.execute(
                select(PROJECT_PROCESS_OUTBOX).where(
                    PROJECT_PROCESS_OUTBOX.c.outbox_id == outbox_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return SQLAlchemyProjectProcessRepository._outbox(row) if row else None

    @staticmethod
    def canonical_payload(payload: dict) -> tuple[dict, str]:
        if not isinstance(payload, dict):
            raise TypeError("project process event payload must be an object")
        forbidden = {
            "prompt",
            "secret",
            "raw_tool_arguments",
            "raw_tool_args",
            "api_key",
            "access_token",
        }

        def inspect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).strip().lower() in forbidden:
                        raise ValueError("project process event payload contains forbidden data")
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(payload)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("project process event payload is too large")
        return payload, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _process(row) -> ProjectProcess:
        def aware(value):
            return (
                value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value
            )

        return ProjectProcess(
            row["process_id"],
            row["project_id"],
            ProjectProcessPhase(row["phase"]),
            ProjectProcessStatus(row["status"]),
            ProjectProcessWaitReason(row["wait_reason"]),
            row["version"],
            row["root_goal_id"],
            row["active_plan_id"],
            row["execution_policy_id"],
            row["execution_policy_version"],
            row["started_by"],
            aware(row["started_at"]),
            aware(row["updated_at"]),
            row["last_event_sequence"],
            row["last_orchestration_sequence"],
            row["lease_owner"],
            row["lease_token"],
            aware(row["lease_expires_at"]),
            aware(row["completed_at"]),
        )

    @staticmethod
    def _event(row) -> ProjectProcessEvent:
        occurred = row["occurred_at"]
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        return ProjectProcessEvent(
            row["event_id"],
            row["process_id"],
            row["project_id"],
            row["sequence"],
            row["event_type"],
            row["idempotency_key"],
            row["transition_key"],
            row["schema_version"],
            row["subject_type"],
            row["subject_id"],
            row["source_aggregate_version"],
            row["process_version_before"],
            row["process_version_after"],
            row["initiated_by"],
            row["executed_as"],
            row["correlation_id"],
            row["causation_id"],
            dict(row["payload_json"]),
            row["payload_sha256"],
            occurred,
        )

    @staticmethod
    def _reservation(row) -> ProjectExecutionReservation:
        created = row["created_at"]
        settled = row["settled_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if settled is not None and settled.tzinfo is None:
            settled = settled.replace(tzinfo=UTC)
        return ProjectExecutionReservation(
            row["reservation_id"], row["reservation_key"], row["process_id"],
            row["project_id"], row["work_node_id"], row["team_id"], row["policy_id"],
            row["policy_version"], row["execution_attempt"], row["specialist_depth"],
            row["reserved_tokens"], row["reserved_model_cost_microusd"], row["agent_run_id"],
            ProjectExecutionReservationStatus(row["status"]), created, settled,
            row["terminal_event_id"], row["terminal_total_tokens"],
            row["terminal_model_cost_microusd"],
        )

    @staticmethod
    def _input_request(row) -> ProjectInputRequest:
        created = row["created_at"]
        answered = row["answered_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if answered is not None and answered.tzinfo is None:
            answered = answered.replace(tzinfo=UTC)
        return ProjectInputRequest(
            row["request_id"], row["process_id"], row["project_id"], row["work_node_id"],
            row["requested_by_run_id"], row["requested_by_agent_id"], row["question"],
            dict(row["input_schema_json"]), dict(row["context_projection_json"]),
            ProjectInputRequestStatus(row["status"]),
            dict(row["response_json"]) if row["response_json"] is not None else None,
            row["version"], row["created_by"], created, row["answered_by"], answered,
        )

    @staticmethod
    def _gate(row) -> ProjectGate:
        created = row["created_at"]
        decided = row["decided_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if decided is not None and decided.tzinfo is None:
            decided = decided.replace(tzinfo=UTC)
        return ProjectGate(
            row["gate_id"], row["process_id"], row["project_id"], row["gate_type"],
            row["subject_type"], row["subject_id"], tuple(row["required_roles_json"]),
            tuple(row["allowed_decisions_json"]), ProjectGateStatus(row["status"]),
            row["decision"], row["reason"], row["version"], row["created_by"], created,
            row["decided_by"], decided,
        )

    @staticmethod
    def _command(row) -> ProjectProcessCommand:
        created = row["created_at"]
        applied = row["applied_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if applied is not None and applied.tzinfo is None:
            applied = applied.replace(tzinfo=UTC)
        return ProjectProcessCommand(
            row["command_id"], row["process_id"], row["project_id"], row["decision_id"],
            ProjectProcessCommandType(row["command_type"]), row["request_digest"],
            row["based_on_process_version"], row["based_on_event_sequence"],
            row["graph_snapshot_digest"], ProjectProcessCommandStatus(row["status"]),
            row["result_subject_id"], created, applied,
            dict(row["request_json"] or {}),
        )

    @staticmethod
    def _decision(row) -> ProjectOrchestrationDecision:
        created = row["created_at"]
        applied = row["applied_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if applied is not None and applied.tzinfo is None:
            applied = applied.replace(tzinfo=UTC)
        return ProjectOrchestrationDecision(
            row["decision_id"],
            row["process_id"],
            row["project_id"],
            row["reason"],
            row["based_on_process_version"],
            row["based_on_event_sequence"],
            row["graph_snapshot_digest"],
            row["command_batch_digest"],
            dict(row["decision_json"] or {}),
            row["decision_digest"],
            ProjectOrchestrationDecisionStatus(row["status"]),
            created,
            applied,
        )

    @staticmethod
    def _outbox(row) -> ProjectProcessOutboxEntry:
        def aware(value):
            return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

        return ProjectProcessOutboxEntry(
            row["outbox_id"], row["event_id"], row["process_id"], row["project_id"],
            ProjectProcessOutboxStatus(row["status"]), row["attempt_count"],
            aware(row["available_at"]), row["lease_owner"], row["lease_token"],
            aware(row["lease_expires_at"]), aware(row["published_at"]), row["last_error"],
        )
