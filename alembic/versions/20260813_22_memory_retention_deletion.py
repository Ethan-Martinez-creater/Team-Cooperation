"""Add auditable memory legal holds and deletion workflow.

Revision ID: 20260813_22
Revises: 20260813_21
Create Date: 2026-08-13
"""

from typing import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "20260813_22"
down_revision: str | None = "20260813_21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("memory_legal_holds",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("hold_id", sa.String(128), nullable=False),
        sa.Column("memory_id", sa.String(64), nullable=False),
        sa.Column("reason_digest", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.String(128), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(reason_digest)=64", name="ck_memory_legal_holds_reason_digest"),
        sa.CheckConstraint("(active AND released_by IS NULL AND released_at IS NULL) OR (NOT active AND released_by IS NOT NULL AND released_at IS NOT NULL)", name="ck_memory_legal_holds_release"),
        sa.ForeignKeyConstraint(["tenant_id", "memory_id"],
            ["memory_records.tenant_id", "memory_records.memory_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "hold_id"))
    op.create_table("memory_deletion_requests",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("memory_id", sa.String(64), nullable=False),
        sa.Column("requester_id", sa.String(128), nullable=False),
        sa.Column("reason_digest", sa.String(64), nullable=False),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason_digest", sa.String(64), nullable=True),
        sa.Column("content_fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("status IN ('pending','rejected','purged')", name="ck_memory_deletion_requests_status"),
        sa.CheckConstraint("previous_status IN ('active','quarantined')", name="ck_memory_deletion_requests_previous_status"),
        sa.CheckConstraint("length(reason_digest)=64 AND length(content_fingerprint)=64", name="ck_memory_deletion_requests_digests"),
        sa.CheckConstraint("decision_reason_digest IS NULL OR length(decision_reason_digest)=64", name="ck_memory_deletion_requests_decision_digest"),
        sa.CheckConstraint("(status='pending' AND decided_by IS NULL AND decided_at IS NULL) OR (status<>'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)", name="ck_memory_deletion_requests_decision"),
        sa.PrimaryKeyConstraint("tenant_id", "request_id"))
    for table in ("memory_legal_holds", "memory_deletion_requests"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")


def downgrade() -> None:
    op.drop_table("memory_deletion_requests")
    op.drop_table("memory_legal_holds")
