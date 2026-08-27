"""Create tenant-isolated durable task execution queues.

Revision ID: 20260730_06
Revises: 20260730_05
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_06"
down_revision: str | None = "20260730_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "execution_tasks",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("program_id", sa.String(length=128), nullable=True),
        sa.Column("assignment_id", sa.String(length=128), nullable=True),
        sa.Column("queue", sa.String(length=128), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("result", _jsonb(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name=op.f("ck_execution_tasks_request_digest"),
        ),
        sa.CheckConstraint(
            "priority BETWEEN -1000 AND 1000",
            name=op.f("ck_execution_tasks_priority"),
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 100",
            name=op.f("ck_execution_tasks_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND max_attempts",
            name=op.f("ck_execution_tasks_attempt_count"),
        ),
        sa.CheckConstraint(
            "status IN "
            "('queued','leased','running','retry_wait','succeeded','failed','cancelled')",
            name=op.f("ck_execution_tasks_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('leased','running') AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status NOT IN ('leased','running') AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_execution_tasks_lease_state"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','cancelled')) = " "(completed_at IS NOT NULL)",
            name=op.f("ck_execution_tasks_terminal_completion"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "task_id", name=op.f("pk_execution_tasks")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_execution_task_idempotency",
        ),
    )
    op.create_index(
        "ix_execution_tasks_claim",
        "execution_tasks",
        ["tenant_id", "queue", "status", "available_at", "priority"],
        unique=False,
    )
    op.create_table(
        "execution_task_dependencies",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("dependency_id", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "task_id <> dependency_id",
            name=op.f("ck_execution_task_dependencies_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["execution_tasks.tenant_id", "execution_tasks.task_id"],
            name="fk_execution_dependency_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dependency_id"],
            ["execution_tasks.tenant_id", "execution_tasks.task_id"],
            name="fk_execution_dependency_target",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "task_id",
            "dependency_id",
            name=op.f("pk_execution_task_dependencies"),
        ),
    )
    op.create_table(
        "execution_task_events",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("details", _jsonb(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence > 0",
            name=op.f("ck_execution_task_events_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["execution_tasks.tenant_id", "execution_tasks.task_id"],
            name="fk_execution_event_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "task_id",
            "sequence",
            name=op.f("pk_execution_task_events"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_execution_event_id",
        ),
    )

    for table_name in (
        "execution_tasks",
        "execution_task_dependencies",
        "execution_task_events",
    ):
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_select
            ON {table_name} FOR SELECT
            USING (
                tenant_id = current_setting('coifesp.tenant_id', true)
            )
            """)
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_insert
            ON {table_name} FOR INSERT
            WITH CHECK (
                tenant_id = current_setting('coifesp.tenant_id', true)
            )
            """)

    for table_name in ("execution_tasks", "execution_task_dependencies"):
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_update
            ON {table_name} FOR UPDATE
            USING (
                tenant_id = current_setting('coifesp.tenant_id', true)
            )
            WITH CHECK (
                tenant_id = current_setting('coifesp.tenant_id', true)
            )
            """)

    op.execute("""
        CREATE FUNCTION coifesp_reject_execution_event_mutation_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'execution task events are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_execution_task_events_reject_update_delete
        BEFORE UPDATE OR DELETE ON execution_task_events
        FOR EACH ROW
        EXECUTE FUNCTION coifesp_reject_execution_event_mutation_v1()
        """)
    op.execute("""
        CREATE TRIGGER trg_execution_task_events_reject_truncate
        BEFORE TRUNCATE ON execution_task_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION coifesp_reject_execution_event_mutation_v1()
        """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_task_events_reject_truncate "
        "ON execution_task_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_task_events_reject_update_delete "
        "ON execution_task_events"
    )
    op.execute("DROP FUNCTION IF EXISTS coifesp_reject_execution_event_mutation_v1()")
    op.drop_table("execution_task_events")
    op.drop_table("execution_task_dependencies")
    op.drop_index("ix_execution_tasks_claim", table_name="execution_tasks")
    op.drop_table("execution_tasks")
