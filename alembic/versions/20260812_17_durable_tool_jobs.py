"""Create encrypted, tenant-isolated durable tool jobs.

Revision ID: 20260812_17
Revises: 20260806_16
Create Date: 2026-08-12
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260812_17"
down_revision: str | None = "20260806_16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_jobs",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("job_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("call_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("arguments_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("arguments_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("arguments_fingerprint", sa.String(64), nullable=False),
        sa.Column("arguments_key_id", sa.String(128), nullable=False),
        sa.Column("result_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("result_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("result_fingerprint", sa.String(64), nullable=True),
        sa.Column("result_key_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(request_digest)=64", name=op.f("ck_tool_jobs_request_digest")),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND max_attempts AND max_attempts BETWEEN 1 AND 10",
            name=op.f("ck_tool_jobs_attempts"),
        ),
        sa.CheckConstraint(
            "status IN ('queued','leased','running','retry_wait','succeeded','failed','cancelled')",
            name=op.f("ck_tool_jobs_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('leased','running') AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status NOT IN ('leased','running') AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_tool_jobs_lease_state"),
        ),
        sa.CheckConstraint(
            "(result_ciphertext IS NULL AND result_nonce IS NULL AND result_fingerprint IS NULL AND result_key_id IS NULL) OR (result_ciphertext IS NOT NULL AND result_nonce IS NOT NULL AND result_fingerprint IS NOT NULL AND result_key_id IS NOT NULL)",
            name=op.f("ck_tool_jobs_result_crypto"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','cancelled'))=(completed_at IS NOT NULL)",
            name=op.f("ck_tool_jobs_terminal"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "job_id", name=op.f("pk_tool_jobs")),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_tool_jobs_idempotency"),
        sa.UniqueConstraint("tenant_id", "run_id", "call_id", name="uq_tool_jobs_run_call"),
    )
    op.create_index(
        "ix_tool_jobs_claim", "tool_jobs", ["tenant_id", "status", "available_at", "created_at"]
    )
    op.create_table(
        "tool_job_events",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("job_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_tool_job_events_sequence")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["tool_jobs.tenant_id", "tool_jobs.job_id"],
            name="fk_tool_job_event_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "job_id", "sequence", name=op.f("pk_tool_job_events")),
        sa.UniqueConstraint("tenant_id", "event_id", name="uq_tool_job_event_id"),
    )
    for table in ("tool_jobs", "tool_job_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_select ON {table} FOR SELECT USING (tenant_id=current_setting('coifesp.tenant_id', true))"
        )
        op.execute(
            f"CREATE POLICY {table}_tenant_insert ON {table} FOR INSERT WITH CHECK (tenant_id=current_setting('coifesp.tenant_id', true))"
        )
    op.execute(
        "CREATE POLICY tool_jobs_tenant_update ON tool_jobs FOR UPDATE USING (tenant_id=current_setting('coifesp.tenant_id', true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id', true))"
    )
    op.execute("""
        CREATE FUNCTION coifesp_reject_tool_job_event_mutation_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'tool job events are append-only' USING ERRCODE='55000'; END; $$
    """)
    op.execute(
        "CREATE TRIGGER trg_tool_job_events_reject_update_delete BEFORE UPDATE OR DELETE ON tool_job_events FOR EACH ROW EXECUTE FUNCTION coifesp_reject_tool_job_event_mutation_v1()"
    )
    op.execute(
        "CREATE TRIGGER trg_tool_job_events_reject_truncate BEFORE TRUNCATE ON tool_job_events FOR EACH STATEMENT EXECUTE FUNCTION coifesp_reject_tool_job_event_mutation_v1()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tool_job_events_reject_truncate ON tool_job_events")
    op.execute("DROP TRIGGER IF EXISTS trg_tool_job_events_reject_update_delete ON tool_job_events")
    op.execute("DROP FUNCTION IF EXISTS coifesp_reject_tool_job_event_mutation_v1()")
    op.drop_table("tool_job_events")
    op.drop_index("ix_tool_jobs_claim", table_name="tool_jobs")
    op.drop_table("tool_jobs")
