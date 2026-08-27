"""Add atomic governance command idempotency claims.

Revision ID: 20260730_05
Revises: 20260730_04
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260730_05"
down_revision: str | None = "20260730_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governance_commands",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_version", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name=op.f("ck_governance_commands_request_digest"),
        ),
        sa.CheckConstraint(
            "status IN ('started', 'completed')",
            name=op.f("ck_governance_commands_status"),
        ),
        sa.CheckConstraint(
            "(status = 'started' AND result_version IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND result_version IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name=op.f("ck_governance_commands_completion"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "idempotency_key",
            name=op.f("pk_governance_commands"),
        ),
    )
    op.create_index(
        "ix_governance_commands_program_created",
        "governance_commands",
        ["program_id", "created_at"],
        unique=False,
    )
    op.execute("ALTER TABLE governance_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE governance_commands FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY governance_commands_tenant_select
        ON governance_commands FOR SELECT
        USING (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY governance_commands_tenant_insert
        ON governance_commands FOR INSERT
        WITH CHECK (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY governance_commands_tenant_update
        ON governance_commands FOR UPDATE
        USING (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)


def downgrade() -> None:
    op.drop_table("governance_commands")
