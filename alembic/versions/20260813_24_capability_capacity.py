"""Add time-bounded capability capacity declarations.

Revision ID: 20260813_24
Revises: 20260813_23
Create Date: 2026-08-13
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_24"
down_revision: str | None = "20260813_23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("team_capability_capacity",
        sa.Column("provider_tenant_id", sa.String(128), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("available_slots", sa.Integer(), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visible_to_tenants", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.CheckConstraint("status IN ('available','limited','unavailable')", name="ck_team_capability_capacity_status"),
        sa.CheckConstraint("available_slots >= 0 AND state_version > 0", name="ck_team_capability_capacity_values"),
        sa.ForeignKeyConstraint(["provider_tenant_id", "capability_id", "version"],
            ["team_capabilities.provider_tenant_id", "team_capabilities.capability_id", "team_capabilities.version"],
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider_tenant_id", "capability_id", "version"))
    op.create_index("ix_team_capability_capacity_matching", "team_capability_capacity",
        ["valid_until", "status"])
    op.execute("ALTER TABLE team_capability_capacity ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE team_capability_capacity FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY team_capability_capacity_select ON team_capability_capacity FOR SELECT USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY team_capability_capacity_insert ON team_capability_capacity FOR INSERT WITH CHECK (provider_tenant_id=current_setting('coifesp.tenant_id',true) AND current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY team_capability_capacity_update ON team_capability_capacity FOR UPDATE USING (provider_tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (provider_tenant_id=current_setting('coifesp.tenant_id',true) AND current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")


def downgrade() -> None:
    op.drop_index("ix_team_capability_capacity_matching", table_name="team_capability_capacity")
    op.drop_table("team_capability_capacity")
