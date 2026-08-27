"""Add cross-team capacity conflict negotiations.

Revision ID: 20260813_26
Revises: 20260813_25
Create Date: 2026-08-13
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_26"
down_revision: str | None = "20260813_25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("team_capacity_negotiations",
        sa.Column("provider_tenant_id", sa.String(128), nullable=False),
        sa.Column("negotiation_id", sa.String(128), nullable=False),
        sa.Column("consumer_tenant_id", sa.String(128), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("requested_slots", sa.Integer(), nullable=False),
        sa.Column("earliest_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("request_reason_digest", sa.String(64), nullable=False),
        sa.Column("decision_reason_digest", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visible_to_tenants", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.CheckConstraint("requested_slots > 0 AND state_version > 0", name="ck_team_capacity_negotiations_values"),
        sa.CheckConstraint("earliest_start < latest_end", name="ck_team_capacity_negotiations_window"),
        sa.CheckConstraint("status IN ('proposed','accepted','rejected','withdrawn')", name="ck_team_capacity_negotiations_status"),
        sa.CheckConstraint("length(request_reason_digest)=64 AND (decision_reason_digest IS NULL OR length(decision_reason_digest)=64)", name="ck_team_capacity_negotiations_digests"),
        sa.CheckConstraint("(status='proposed' AND decided_by IS NULL AND decided_at IS NULL) OR (status<>'proposed' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)", name="ck_team_capacity_negotiations_decision"),
        sa.ForeignKeyConstraint(["provider_tenant_id", "capability_id", "version"],
            ["team_capabilities.provider_tenant_id", "team_capabilities.capability_id", "team_capabilities.version"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider_tenant_id", "negotiation_id"))
    op.create_index("ix_team_capacity_negotiations_pending", "team_capacity_negotiations",
        ["provider_tenant_id", "status", "created_at"])
    op.execute("ALTER TABLE team_capacity_negotiations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE team_capacity_negotiations FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY team_capacity_negotiations_select ON team_capacity_negotiations FOR SELECT USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY team_capacity_negotiations_insert ON team_capacity_negotiations FOR INSERT WITH CHECK (consumer_tenant_id=current_setting('coifesp.tenant_id',true) AND current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY team_capacity_negotiations_update ON team_capacity_negotiations FOR UPDATE USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants)) WITH CHECK (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")


def downgrade() -> None:
    op.drop_index("ix_team_capacity_negotiations_pending", table_name="team_capacity_negotiations")
    op.drop_table("team_capacity_negotiations")
