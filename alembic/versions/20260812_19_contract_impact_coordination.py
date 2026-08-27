"""Add versioned collaboration contracts and bilateral change impacts.

Revision ID: 20260812_19
Revises: 20260812_18
Create Date: 2026-08-12
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260812_19"
down_revision: str | None = "20260812_18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def tenants():
    return postgresql.ARRAY(sa.String(128))


def upgrade() -> None:
    op.create_table(
        "collaboration_contracts",
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("producer_tenant_id", sa.String(128), nullable=False),
        sa.Column("producer_assignment_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("visible_to_tenants", tenants(), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('openapi','json_schema','protobuf','asyncapi','data','document','generic')", name="ck_collaboration_contracts_kind"),
        sa.CheckConstraint("aggregate_version >= 1", name="ck_collaboration_contracts_aggregate_version"),
        sa.ForeignKeyConstraint(["program_id"], ["governance_programs.program_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["program_id", "producer_assignment_id"], ["governance_assignments.program_id", "governance_assignments.assignment_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("program_id", "contract_id"),
    )
    op.create_table(
        "collaboration_contract_releases",
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("artifact_ref", sa.String(1024), nullable=False),
        sa.Column("compatibility", sa.String(32), nullable=False),
        sa.Column("predecessor_version", sa.String(64), nullable=True),
        sa.Column("visible_to_tenants", tenants(), nullable=False),
        sa.Column("released_by", sa.String(128), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(content_digest)=64", name="ck_collaboration_contract_releases_digest"),
        sa.CheckConstraint("compatibility IN ('compatible','breaking','unknown')", name="ck_collaboration_contract_releases_compatibility"),
        sa.ForeignKeyConstraint(["program_id", "contract_id"], ["collaboration_contracts.program_id", "collaboration_contracts.contract_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("program_id", "contract_id", "version"),
    )
    op.create_table(
        "collaboration_contract_dependencies",
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("dependency_id", sa.String(128), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("consumer_tenant_id", sa.String(128), nullable=False),
        sa.Column("consumer_assignment_id", sa.String(128), nullable=False),
        sa.Column("version_constraint", sa.String(256), nullable=False),
        sa.Column("baseline_version", sa.String(64), nullable=False),
        sa.Column("baseline_digest", sa.String(64), nullable=False),
        sa.Column("visible_to_tenants", tenants(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(baseline_digest)=64", name="ck_collaboration_contract_dependencies_baseline_digest"),
        sa.ForeignKeyConstraint(["program_id", "contract_id"], ["collaboration_contracts.program_id", "collaboration_contracts.contract_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["program_id", "consumer_assignment_id"], ["governance_assignments.program_id", "governance_assignments.assignment_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("program_id", "dependency_id"),
        sa.UniqueConstraint("program_id", "contract_id", "consumer_assignment_id", name="uq_contract_dependency_consumer"),
    )
    op.create_table(
        "collaboration_change_impacts",
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("impact_id", sa.String(128), nullable=False),
        sa.Column("dependency_id", sa.String(128), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("consumer_tenant_id", sa.String(128), nullable=False),
        sa.Column("consumer_assignment_id", sa.String(128), nullable=False),
        sa.Column("from_version", sa.String(64), nullable=False),
        sa.Column("to_version", sa.String(64), nullable=False),
        sa.Column("from_digest", sa.String(64), nullable=False),
        sa.Column("to_digest", sa.String(64), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("compatibility", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("consumer_note", sa.Text(), nullable=True),
        sa.Column("remediation", sa.Text(), nullable=True),
        sa.Column("acknowledged_by", sa.String(128), nullable=True),
        sa.Column("accepted_by", sa.String(128), nullable=True),
        sa.Column("visible_to_tenants", tenants(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(from_digest)=64 AND length(to_digest)=64", name="ck_collaboration_change_impacts_digests"),
        sa.CheckConstraint("severity BETWEEN 0 AND 2", name="ck_collaboration_change_impacts_severity"),
        sa.CheckConstraint("state_version >= 1", name="ck_collaboration_change_impacts_state_version"),
        sa.CheckConstraint("compatibility IN ('compatible','breaking','unknown')", name="ck_collaboration_change_impacts_compatibility"),
        sa.CheckConstraint("state IN ('pending','acknowledged','blocked','remediation_proposed','accepted')", name="ck_collaboration_change_impacts_state"),
        sa.ForeignKeyConstraint(["program_id", "dependency_id"], ["collaboration_contract_dependencies.program_id", "collaboration_contract_dependencies.dependency_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["program_id", "contract_id", "to_version"], ["collaboration_contract_releases.program_id", "collaboration_contract_releases.contract_id", "collaboration_contract_releases.version"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("program_id", "impact_id"),
        sa.UniqueConstraint("program_id", "dependency_id", "to_version", name="uq_change_impact_release"),
    )
    op.create_index("ix_change_impacts_assignment_state", "collaboration_change_impacts", ["consumer_assignment_id", "state"])
    op.create_table(
        "collaboration_contract_events",
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("visible_to_tenants", tenants(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("program_id", "sequence"),
        sa.UniqueConstraint("program_id", "event_id", name="uq_contract_event_id"),
    )
    op.create_table(
        "collaboration_contract_outbox",
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("producer_tenant_id", sa.String(128), nullable=False),
        sa.Column("recipient_tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','published','dead_letter')", name="ck_collaboration_contract_outbox_status"),
        sa.ForeignKeyConstraint(["program_id", "event_sequence"], ["collaboration_contract_events.program_id", "collaboration_contract_events.sequence"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint("program_id", "event_sequence", "recipient_tenant_id", name="uq_contract_outbox_recipient"),
    )
    op.create_table(
        "collaboration_contract_commands",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("program_id", sa.String(128), nullable=False),
        sa.Column("command_type", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(request_digest)=64", name="ck_collaboration_contract_commands_request_digest"),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"),
    )
    visible = ("collaboration_contracts", "collaboration_contract_releases", "collaboration_contract_dependencies", "collaboration_change_impacts", "collaboration_contract_events")
    for table in visible:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_tenant_all ON {table} FOR ALL USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants)) WITH CHECK (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("ALTER TABLE collaboration_contract_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE collaboration_contract_outbox FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY collaboration_contract_outbox_tenant_select ON collaboration_contract_outbox FOR SELECT USING (recipient_tenant_id=current_setting('coifesp.tenant_id',true))")
    op.execute("CREATE POLICY collaboration_contract_outbox_tenant_insert ON collaboration_contract_outbox FOR INSERT WITH CHECK (producer_tenant_id=current_setting('coifesp.tenant_id',true))")
    op.execute("ALTER TABLE collaboration_contract_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE collaboration_contract_commands FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY collaboration_contract_commands_tenant_all ON collaboration_contract_commands FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")
    op.execute("CREATE FUNCTION coifesp_reject_contract_event_mutation_v1() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'collaboration_contract_events is append-only' USING ERRCODE='55000'; END; $$")
    op.execute("CREATE TRIGGER trg_contract_events_immutable BEFORE UPDATE OR DELETE ON collaboration_contract_events FOR EACH ROW EXECUTE FUNCTION coifesp_reject_contract_event_mutation_v1()")
    op.execute("CREATE TRIGGER trg_contract_events_reject_truncate BEFORE TRUNCATE ON collaboration_contract_events FOR EACH STATEMENT EXECUTE FUNCTION coifesp_reject_contract_event_mutation_v1()")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON collaboration_contract_events FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_contract_events_reject_truncate ON collaboration_contract_events")
    op.execute("DROP TRIGGER IF EXISTS trg_contract_events_immutable ON collaboration_contract_events")
    op.drop_table("collaboration_contract_commands")
    op.drop_table("collaboration_contract_outbox")
    op.drop_table("collaboration_contract_events")
    op.drop_table("collaboration_change_impacts")
    op.drop_table("collaboration_contract_dependencies")
    op.drop_table("collaboration_contract_releases")
    op.drop_table("collaboration_contracts")
    op.execute("DROP FUNCTION IF EXISTS coifesp_reject_contract_event_mutation_v1()")
