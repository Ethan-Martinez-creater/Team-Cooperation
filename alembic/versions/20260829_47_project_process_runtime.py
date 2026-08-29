"""Project process runtime, budget, human waits, commands and outbox.

Revision ID: 20260829_47
Revises: 20260829_46
Create Date: 2026-08-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260829_47"
down_revision: str | None = "20260829_46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_execution_policies",
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("max_agent_runs", sa.Integer(), nullable=False),
        sa.Column("max_total_tokens", sa.Integer(), nullable=False),
        sa.Column("max_model_cost_microusd", sa.Integer(), nullable=False),
        sa.Column("max_replans", sa.Integer(), nullable=False),
        sa.Column("max_generated_tasks", sa.Integer(), nullable=False),
        sa.Column("max_active_agent_runs", sa.Integer(), nullable=False),
        sa.Column("max_active_runs_per_team", sa.Integer(), nullable=False),
        sa.Column("max_specialist_depth", sa.Integer(), nullable=False),
        sa.Column("max_specialist_runs_per_task", sa.Integer(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("policy_id", "version"),
        sa.CheckConstraint("version >= 1", name="project_policy_version"),
        sa.CheckConstraint(
            "max_agent_runs >= 0 AND max_total_tokens >= 0 AND "
            "max_model_cost_microusd >= 0 AND max_replans >= 0 AND "
            "max_generated_tasks >= 0 AND max_active_agent_runs >= 0 AND "
            "max_active_runs_per_team >= 0 AND max_specialist_depth >= 0 AND "
            "max_specialist_runs_per_task >= 0",
            name="project_policy_nonnegative_limits",
        ),
    )

    op.create_table(
        "project_processes",
        sa.Column("process_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("wait_reason", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("root_goal_id", sa.String(128), nullable=True),
        sa.Column("active_plan_id", sa.String(128), nullable=True),
        sa.Column("execution_policy_id", sa.String(128), nullable=False),
        sa.Column("execution_policy_version", sa.Integer(), nullable=False),
        sa.Column("started_by", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False),
        sa.Column("last_orchestration_sequence", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_policy_id", "execution_policy_version"],
            ["project_execution_policies.policy_id", "project_execution_policies.version"],
        ),
        sa.UniqueConstraint("process_id", "project_id", name="uq_project_process_project"),
        sa.CheckConstraint("version >= 1", name="project_process_version"),
        sa.CheckConstraint("last_event_sequence >= 0", name="project_process_event_sequence"),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="project_process_lease_complete",
        ),
        sa.CheckConstraint(
            "phase IN ('INTAKE','ANALYSIS','PLANNING','EXECUTION','INTEGRATION',"
            "'VERIFICATION','DELIVERY','TERMINAL')",
            name="project_process_phase",
        ),
        sa.CheckConstraint(
            "status IN ('READY','RUNNING','WAITING','BLOCKED','COMPLETED','FAILED','CANCELLED')",
            name="project_process_status",
        ),
        sa.CheckConstraint(
            "wait_reason IN ('NONE','HUMAN_INPUT','HUMAN_APPROVAL','TEAM_RESPONSE',"
            "'AGENT_RUN','TOOL_JOB','DEPENDENCY','VERIFICATION','SCHEDULE')",
            name="project_process_wait_reason",
        ),
        sa.CheckConstraint(
            "((status IN ('WAITING','BLOCKED')) AND wait_reason <> 'NONE') OR "
            "((status NOT IN ('WAITING','BLOCKED')) AND wait_reason = 'NONE')",
            name="project_process_wait_consistency",
        ),
        sa.CheckConstraint(
            "(phase = 'TERMINAL' AND status IN ('COMPLETED','FAILED','CANCELLED')) OR "
            "(phase <> 'TERMINAL' AND status NOT IN ('COMPLETED','FAILED','CANCELLED'))",
            name="project_process_terminal_consistency",
        ),
    )
    op.create_index("ix_project_process_project", "project_processes", ["project_id"])
    op.create_index(
        "uq_project_process_one_active",
        "project_processes",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('COMPLETED','FAILED','CANCELLED')"),
        sqlite_where=sa.text("status NOT IN ('COMPLETED','FAILED','CANCELLED')"),
    )

    op.create_table(
        "project_execution_usage",
        sa.Column("process_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("agent_runs_started", sa.Integer(), nullable=False),
        sa.Column("agent_runs_completed", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("model_cost_microusd", sa.Integer(), nullable=False),
        sa.Column("replan_count", sa.Integer(), nullable=False),
        sa.Column("generated_task_count", sa.Integer(), nullable=False),
        sa.Column("active_agent_runs", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.CheckConstraint(
            "agent_runs_started >= 0 AND agent_runs_completed >= 0 AND total_tokens >= 0 "
            "AND model_cost_microusd >= 0 AND replan_count >= 0 AND generated_task_count >= 0 "
            "AND active_agent_runs >= 0 AND version >= 1",
            name="project_execution_usage_nonnegative",
        ),
        sa.CheckConstraint(
            "agent_runs_completed <= agent_runs_started",
            name="project_execution_usage_completed_bound",
        ),
        sa.CheckConstraint(
            "active_agent_runs <= agent_runs_started - agent_runs_completed",
            name="project_execution_usage_active_bound",
        ),
    )

    op.create_table(
        "project_execution_reservations",
        sa.Column("reservation_id", sa.String(128), primary_key=True),
        sa.Column("reservation_key", sa.String(256), nullable=False, unique=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("work_node_id", sa.String(128), nullable=False),
        sa.Column("team_id", sa.String(128), nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("execution_attempt", sa.Integer(), nullable=False),
        sa.Column("specialist_depth", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_model_cost_microusd", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.String(128), nullable=True, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_event_id", sa.String(128), nullable=True, unique=True),
        sa.Column("terminal_total_tokens", sa.Integer(), nullable=True),
        sa.Column("terminal_model_cost_microusd", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["project_execution_policies.policy_id", "project_execution_policies.version"],
        ),
        sa.CheckConstraint("execution_attempt >= 1", name="project_reservation_attempt"),
        sa.CheckConstraint("specialist_depth >= 0", name="project_reservation_depth"),
        sa.CheckConstraint(
            "reserved_tokens >= 0 AND reserved_model_cost_microusd >= 0",
            name="project_reservation_reserved_budget",
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED','SETTLED','RELEASED')",
            name="project_reservation_status",
        ),
        sa.CheckConstraint(
            "(status = 'RESERVED' AND settled_at IS NULL) OR "
            "(status IN ('SETTLED','RELEASED') AND settled_at IS NOT NULL)",
            name="project_reservation_settlement",
        ),
        sa.CheckConstraint(
            "terminal_total_tokens IS NULL OR terminal_total_tokens >= 0",
            name="project_reservation_tokens",
        ),
        sa.CheckConstraint(
            "terminal_model_cost_microusd IS NULL OR terminal_model_cost_microusd >= 0",
            name="project_reservation_cost",
        ),
    )
    op.create_index(
        "ix_project_reservation_active_team",
        "project_execution_reservations",
        ["process_id", "team_id"],
        postgresql_where=sa.text("status = 'RESERVED'"),
        sqlite_where=sa.text("status = 'RESERVED'"),
    )

    _create_human_tables()
    _create_event_command_outbox_tables()


def _create_human_tables() -> None:
    op.create_table(
        "project_input_requests",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("work_node_id", sa.String(128), nullable=True),
        sa.Column("requested_by_run_id", sa.String(128), nullable=True),
        sa.Column("requested_by_agent_id", sa.String(128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("context_projection_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_by", sa.String(128), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_idempotency_key", sa.String(256), nullable=True, unique=True),
        sa.Column("resolution_event_id", sa.String(128), nullable=True, unique=True),
        sa.Column("resolution_sha256", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.CheckConstraint("status IN ('OPEN','ANSWERED','CANCELLED','EXPIRED')", name="project_input_status"),
        sa.CheckConstraint("version >= 1", name="project_input_version"),
        sa.CheckConstraint("length(question) > 0", name="project_input_question"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND answered_at IS NULL AND resolution_idempotency_key IS NULL "
            "AND resolution_event_id IS NULL) OR "
            "(status <> 'OPEN' AND answered_at IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
            "AND resolution_event_id IS NOT NULL)",
            name="project_input_resolution",
        ),
    )
    op.create_index("ix_project_input_open", "project_input_requests", ["process_id", "status"])

    op.create_table(
        "project_gates",
        sa.Column("gate_id", sa.String(128), primary_key=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("gate_type", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("required_roles_json", sa.JSON(), nullable=False),
        sa.Column("allowed_decisions_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_idempotency_key", sa.String(256), nullable=True, unique=True),
        sa.Column("resolution_event_id", sa.String(128), nullable=True, unique=True),
        sa.Column("resolution_sha256", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.CheckConstraint("status IN ('OPEN','DECIDED','CANCELLED','EXPIRED')", name="project_gate_status"),
        sa.CheckConstraint("version >= 1", name="project_gate_version"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND decided_at IS NULL AND resolution_idempotency_key IS NULL "
            "AND resolution_event_id IS NULL) OR "
            "(status <> 'OPEN' AND decided_at IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
            "AND resolution_event_id IS NOT NULL)",
            name="project_gate_resolution",
        ),
    )
    op.create_index("ix_project_gate_open", "project_gates", ["process_id", "status"])


def _create_event_command_outbox_tables() -> None:
    op.create_table(
        "project_process_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("transition_key", sa.String(128), nullable=True),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("source_aggregate_version", sa.Integer(), nullable=True),
        sa.Column("process_version_before", sa.Integer(), nullable=False),
        sa.Column("process_version_after", sa.Integer(), nullable=True),
        sa.Column("initiated_by", sa.String(128), nullable=False),
        sa.Column("executed_as", sa.String(128), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("causation_id", sa.String(128), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.UniqueConstraint("process_id", "sequence", name="uq_project_process_event_sequence"),
        sa.UniqueConstraint("process_id", "idempotency_key", name="uq_project_process_event_idempotency"),
        sa.CheckConstraint("sequence >= 1", name="project_process_event_positive_sequence"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="project_process_event_digest"),
        sa.CheckConstraint(
            "(process_version_after IS NULL AND transition_key IS NULL) OR "
            "(process_version_after = process_version_before + 1 AND transition_key IS NOT NULL)",
            name="project_process_event_transition",
        ),
    )
    op.create_index(
        "ix_project_process_events_project_sequence",
        "project_process_events",
        ["project_id", "sequence"],
    )

    op.create_table(
        "project_process_commands",
        sa.Column("command_id", sa.String(128), primary_key=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("decision_id", sa.String(128), nullable=False),
        sa.Column("command_type", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("based_on_process_version", sa.Integer(), nullable=False),
        sa.Column("based_on_event_sequence", sa.Integer(), nullable=False),
        sa.Column("graph_snapshot_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_subject_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.UniqueConstraint("process_id", "decision_id", "command_id", name="uq_process_command_decision"),
        sa.CheckConstraint("length(request_digest) = 64", name="project_command_digest"),
        sa.CheckConstraint("based_on_process_version >= 1", name="project_command_process_version"),
        sa.CheckConstraint("based_on_event_sequence >= 0", name="project_command_event_sequence"),
        sa.CheckConstraint(
            "command_type IN ('propose_task','propose_dependency','propose_risk',"
            "'propose_decision','request_rework','request_replan','request_human_input',"
            "'request_human_gate')",
            name="project_command_type",
        ),
        sa.CheckConstraint("status IN ('PENDING','APPLIED','REJECTED','STALE')", name="project_command_status"),
        sa.CheckConstraint(
            "(status = 'PENDING' AND applied_at IS NULL) OR "
            "(status <> 'PENDING' AND applied_at IS NOT NULL)",
            name="project_command_terminal_time",
        ),
    )
    op.create_index("ix_project_command_process_status", "project_process_commands", ["process_id", "status"])

    op.create_table(
        "project_process_outbox",
        sa.Column("outbox_id", sa.String(160), primary_key=True),
        sa.Column("event_id", sa.String(128), sa.ForeignKey("project_process_events.event_id"), nullable=False, unique=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.CheckConstraint("status IN ('PENDING','PUBLISHING','PUBLISHED','FAILED')", name="project_outbox_status"),
        sa.CheckConstraint("attempt_count >= 0", name="project_outbox_attempt_count"),
        sa.CheckConstraint(
            "(status = 'PUBLISHING' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'PUBLISHING' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="project_outbox_lease",
        ),
        sa.CheckConstraint(
            "(status = 'PUBLISHED' AND published_at IS NOT NULL) OR "
            "(status <> 'PUBLISHED' AND published_at IS NULL)",
            name="project_outbox_publish_time",
        ),
    )
    op.create_index("ix_project_outbox_pending", "project_process_outbox", ["status", "available_at"])


def downgrade() -> None:
    op.drop_index("ix_project_outbox_pending", table_name="project_process_outbox")
    op.drop_table("project_process_outbox")
    op.drop_index("ix_project_command_process_status", table_name="project_process_commands")
    op.drop_table("project_process_commands")
    op.drop_index("ix_project_process_events_project_sequence", table_name="project_process_events")
    op.drop_table("project_process_events")
    op.drop_index("ix_project_gate_open", table_name="project_gates")
    op.drop_table("project_gates")
    op.drop_index("ix_project_input_open", table_name="project_input_requests")
    op.drop_table("project_input_requests")
    op.drop_index("ix_project_reservation_active_team", table_name="project_execution_reservations")
    op.drop_table("project_execution_reservations")
    op.drop_table("project_execution_usage")
    op.drop_index("uq_project_process_one_active", table_name="project_processes")
    op.drop_index("ix_project_process_project", table_name="project_processes")
    op.drop_table("project_processes")
    op.drop_table("project_execution_policies")
