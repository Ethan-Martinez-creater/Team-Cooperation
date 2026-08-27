"""Add encrypted, leased, tenant-isolated durable Agent runs.

Revision ID: 20260730_12
Revises: 20260730_11
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_12"
down_revision: str | None = "20260730_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("pending_call_id", sa.String(length=128), nullable=True),
        sa.Column("pending_approval_id", sa.String(length=128), nullable=True),
        sa.Column("checkpoint_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("checkpoint_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("checkpoint_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_key_id", sa.String(length=128), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name=op.f("ck_agent_runs_request_digest"),
        ),
        sa.CheckConstraint(
            "length(checkpoint_fingerprint) = 64",
            name=op.f("ck_agent_runs_checkpoint_fingerprint"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_agent_runs_version")),
        sa.CheckConstraint(
            "turns >= 0 AND tool_calls >= 0 AND total_tokens >= 0",
            name=op.f("ck_agent_runs_usage_nonnegative"),
        ),
        sa.CheckConstraint(
            "status IN ('queued','leased','running','awaiting_approval',"
            "'completed','failed','cancelled')",
            name=op.f("ck_agent_runs_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('leased','running') AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status NOT IN ('leased','running') AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_agent_runs_lease_state"),
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_approval') = "
            "(pending_call_id IS NOT NULL AND pending_approval_id IS NOT NULL)",
            name=op.f("ck_agent_runs_pending_approval"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed','failed','cancelled')) = (completed_at IS NOT NULL)",
            name=op.f("ck_agent_runs_completion"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "run_id", name=op.f("pk_agent_runs")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_agent_run_idempotency",
        ),
    )
    op.create_index(
        "ix_agent_runs_claim",
        "agent_runs",
        ["tenant_id", "status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "agent_run_events",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence > 0",
            name=op.f("ck_agent_run_events_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.run_id"],
            name="fk_agent_run_event_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "run_id",
            "sequence",
            name=op.f("pk_agent_run_events"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_agent_run_event_id",
        ),
    )
    op.create_index(
        "ix_agent_run_events_stream",
        "agent_run_events",
        ["tenant_id", "run_id", "sequence"],
        unique=False,
    )
    for table_name in ("agent_runs", "agent_run_events"):
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_select
            ON {table_name} FOR SELECT
            USING (tenant_id = current_setting('coifesp.tenant_id', true))
            """)
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_insert
            ON {table_name} FOR INSERT
            WITH CHECK (tenant_id = current_setting('coifesp.tenant_id', true))
            """)
    op.execute("""
        CREATE POLICY agent_runs_tenant_update
        ON agent_runs FOR UPDATE
        USING (tenant_id = current_setting('coifesp.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('coifesp.tenant_id', true))
        """)
    op.execute("""
        CREATE FUNCTION coifesp_reject_agent_run_event_mutation_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'agent run events are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_agent_run_events_reject_update_delete
        BEFORE UPDATE OR DELETE ON agent_run_events
        FOR EACH ROW
        EXECUTE FUNCTION coifesp_reject_agent_run_event_mutation_v1()
        """)
    op.execute("""
        CREATE TRIGGER trg_agent_run_events_reject_truncate
        BEFORE TRUNCATE ON agent_run_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION coifesp_reject_agent_run_event_mutation_v1()
        """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_run_events_reject_truncate ON agent_run_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_run_events_reject_update_delete ON agent_run_events"
    )
    op.execute("DROP FUNCTION IF EXISTS coifesp_reject_agent_run_event_mutation_v1()")
    op.drop_index("ix_agent_run_events_stream", table_name="agent_run_events")
    op.drop_table("agent_run_events")
    op.drop_index("ix_agent_runs_claim", table_name="agent_runs")
    op.drop_table("agent_runs")
