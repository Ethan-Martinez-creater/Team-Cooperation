"""Add atomic cross-team capacity reservations.

Revision ID: 20260813_25
Revises: 20260813_24
Create Date: 2026-08-13
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_25"
down_revision: str | None = "20260813_24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("team_capacity_reservations",
        sa.Column("provider_tenant_id", sa.String(128), nullable=False),
        sa.Column("reservation_id", sa.String(128), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("consumer_tenant_id", sa.String(128), nullable=False),
        sa.Column("slots", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.String(128), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visible_to_tenants", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.CheckConstraint("slots > 0", name="ck_team_capacity_reservations_slots"),
        sa.CheckConstraint("status IN ('active','released','expired')", name="ck_team_capacity_reservations_status"),
        sa.CheckConstraint("(status='active' AND released_by IS NULL AND released_at IS NULL) OR (status<>'active' AND released_by IS NOT NULL AND released_at IS NOT NULL)", name="ck_team_capacity_reservations_release"),
        sa.ForeignKeyConstraint(["provider_tenant_id", "capability_id", "version"],
            ["team_capabilities.provider_tenant_id", "team_capabilities.capability_id", "team_capabilities.version"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider_tenant_id", "reservation_id"))
    op.create_index("ix_team_capacity_reservations_active", "team_capacity_reservations",
        ["provider_tenant_id", "capability_id", "version", "status", "expires_at"])
    op.execute("ALTER TABLE team_capacity_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE team_capacity_reservations FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY team_capacity_reservations_select ON team_capacity_reservations FOR SELECT USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY team_capacity_reservations_insert ON team_capacity_reservations FOR INSERT WITH CHECK (consumer_tenant_id=current_setting('coifesp.tenant_id',true) AND current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY team_capacity_reservations_update ON team_capacity_reservations FOR UPDATE USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants)) WITH CHECK (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")


def downgrade() -> None:
    op.drop_index("ix_team_capacity_reservations_active", table_name="team_capacity_reservations")
    op.drop_table("team_capacity_reservations")
