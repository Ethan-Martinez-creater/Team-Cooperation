"""Add reviewed versioned connector registry.

Revision ID: 20260813_28
Revises: 20260813_27
Create Date: 2026-08-13
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_28"
down_revision: str | None = "20260813_27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("connector_registrations",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("connector_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("proposed_action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("token_endpoint", sa.String(2048), nullable=False),
        sa.Column("client_id", sa.String(256), nullable=False),
        sa.Column("client_secret_env", sa.String(128), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(256)), nullable=False),
        sa.Column("allowed_paths", postgresql.ARRAY(sa.String(256)), nullable=False),
        sa.Column("max_classification", sa.Integer(), nullable=False),
        sa.Column("timeout_millis", sa.Integer(), nullable=False),
        sa.Column("max_response_bytes", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("circuit_failure_threshold", sa.Integer(), nullable=False),
        sa.Column("circuit_cooldown_millis", sa.Integer(), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason_digest", sa.String(64), nullable=True),
        sa.CheckConstraint("proposed_action IN ('activate','disable')", name="ck_connector_registrations_action"),
        sa.CheckConstraint("status IN ('pending','active','rejected','disabled')", name="ck_connector_registrations_status"),
        sa.CheckConstraint("version > 0 AND max_classification BETWEEN 0 AND 3", name="ck_connector_registrations_values"),
        sa.CheckConstraint("length(config_digest)=64", name="ck_connector_registrations_digest"),
        sa.CheckConstraint("(status='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR (status<>'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="ck_connector_registrations_review"),
        sa.PrimaryKeyConstraint("tenant_id", "connector_id", "version"))
    op.create_index("uq_connector_one_pending", "connector_registrations",
        ["tenant_id", "connector_id"], unique=True,
        postgresql_where=sa.text("status='pending'"))
    op.execute("ALTER TABLE connector_registrations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector_registrations FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY connector_registrations_all ON connector_registrations FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")


def downgrade() -> None:
    op.drop_table("connector_registrations")
