"""Add tenant-isolated durable high-risk operation approvals.

Revision ID: 20260730_08
Revises: 20260730_07
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260730_08"
down_revision: str | None = "20260730_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("approval_id", sa.String(length=128), nullable=False),
        sa.Column("requester_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("reason_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("required_approver_role", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_execution_id", sa.String(length=128), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name=op.f("ck_approval_requests_approval_request_digest"),
        ),
        sa.CheckConstraint(
            "length(reason_digest) = 64",
            name=op.f("ck_approval_requests_approval_reason_digest"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','revoked','consumed')",
            name=op.f("ck_approval_requests_approval_status"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_approval_requests_approval_version"),
        ),
        sa.CheckConstraint(
            "(status IN ('approved','rejected','revoked','consumed')) = "
            "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_approval_requests_approval_decision"),
        ),
        sa.CheckConstraint(
            "(status = 'consumed') = "
            "(consumed_by_execution_id IS NOT NULL AND consumed_at IS NOT NULL)",
            name=op.f("ck_approval_requests_approval_consumption"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "approval_id",
            name=op.f("pk_approval_requests"),
        ),
    )
    op.create_index(
        "ix_approval_requests_tenant_status_expiry",
        "approval_requests",
        ["tenant_id", "status", "expires_at"],
        unique=False,
    )
    op.execute("ALTER TABLE approval_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approval_requests FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY approval_requests_tenant_select
        ON approval_requests FOR SELECT
        USING (tenant_id = current_setting('coifesp.tenant_id', true))
        """)
    op.execute("""
        CREATE POLICY approval_requests_tenant_insert
        ON approval_requests FOR INSERT
        WITH CHECK (tenant_id = current_setting('coifesp.tenant_id', true))
        """)
    op.execute("""
        CREATE POLICY approval_requests_tenant_update
        ON approval_requests FOR UPDATE
        USING (tenant_id = current_setting('coifesp.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('coifesp.tenant_id', true))
        """)


def downgrade() -> None:
    op.drop_index(
        "ix_approval_requests_tenant_status_expiry",
        table_name="approval_requests",
    )
    op.drop_table("approval_requests")
