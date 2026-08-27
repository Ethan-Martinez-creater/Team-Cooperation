"""Add tenant-isolated team capability directory.

Revision ID: 20260813_20
Revises: 20260812_19
Create Date: 2026-08-13
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_20"
down_revision: str | None = "20260812_19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def array():
    return postgresql.ARRAY(sa.String(128))


def upgrade() -> None:
    op.create_table(
        "team_capabilities",
        sa.Column("provider_tenant_id", sa.String(128), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", array(), nullable=False),
        sa.Column("protocols", array(), nullable=False),
        sa.Column("input_contract", sa.String(1024), nullable=False),
        sa.Column("output_contract", sa.String(1024), nullable=False),
        sa.Column("max_input_classification", sa.Integer(), nullable=False),
        sa.Column("required_compartments", array(), nullable=False),
        sa.Column("residency_regions", array(), nullable=False),
        sa.Column("visible_to_tenants", array(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("published_by", sa.String(128), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_input_classification BETWEEN 0 AND 3", name="ck_team_capabilities_classification"),
        sa.CheckConstraint("length(content_digest)=64", name="ck_team_capabilities_digest"),
        sa.PrimaryKeyConstraint("provider_tenant_id", "capability_id", "version"),
        sa.UniqueConstraint("provider_tenant_id", "capability_id", "content_digest", name="uq_team_capability_content"),
    )
    op.create_table(
        "team_capability_commands",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(request_digest)=64", name="ck_team_capability_commands_request_digest"),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"),
    )
    op.create_table(
        "team_capability_events",
        sa.Column("provider_tenant_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("capability_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("visible_to_tenants", array(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_team_capability_events_sequence"),
        sa.PrimaryKeyConstraint("provider_tenant_id", "sequence"),
        sa.UniqueConstraint("provider_tenant_id", "event_id", name="uq_team_capability_event"),
    )
    for table in ("team_capabilities", "team_capability_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_tenant_all ON {table} FOR ALL USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants)) WITH CHECK (provider_tenant_id=current_setting('coifesp.tenant_id',true) AND current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("ALTER TABLE team_capability_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE team_capability_commands FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY team_capability_commands_tenant_all ON team_capability_commands FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")
    op.execute("CREATE FUNCTION coifesp_reject_capability_event_mutation_v1() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'team_capability_events is append-only' USING ERRCODE='55000'; END; $$")
    op.execute("CREATE TRIGGER trg_capability_events_immutable BEFORE UPDATE OR DELETE ON team_capability_events FOR EACH ROW EXECUTE FUNCTION coifesp_reject_capability_event_mutation_v1()")
    op.execute("CREATE TRIGGER trg_capability_events_reject_truncate BEFORE TRUNCATE ON team_capability_events FOR EACH STATEMENT EXECUTE FUNCTION coifesp_reject_capability_event_mutation_v1()")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON team_capability_events FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_capability_events_reject_truncate ON team_capability_events")
    op.execute("DROP TRIGGER IF EXISTS trg_capability_events_immutable ON team_capability_events")
    op.drop_table("team_capability_events")
    op.drop_table("team_capability_commands")
    op.drop_table("team_capabilities")
    op.execute("DROP FUNCTION IF EXISTS coifesp_reject_capability_event_mutation_v1()")
