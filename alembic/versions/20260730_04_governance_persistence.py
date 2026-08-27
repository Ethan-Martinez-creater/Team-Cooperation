"""Create durable, tenant-filtered collaboration governance state.

Revision ID: 20260730_04
Revises: 20260729_03
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_04"
down_revision: str | None = "20260729_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VISIBLE_TABLES = (
    "governance_members",
    "governance_plans",
    "governance_plan_deliverables",
    "governance_plan_approvers",
    "governance_discussion_items",
    "governance_assignments",
    "governance_assignment_dependencies",
    "governance_assignment_artifacts",
)


def _tenant_array() -> postgresql.ARRAY:
    return postgresql.ARRAY(sa.String(length=128))


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "governance_programs",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("owner_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("classification", sa.Integer(), nullable=False),
        sa.Column("compartments", _jsonb(), nullable=False),
        sa.Column("participant_tenant_ids", _tenant_array(), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "classification BETWEEN 0 AND 3",
            name=op.f("ck_governance_programs_classification"),
        ),
        sa.CheckConstraint(
            "aggregate_version >= 0",
            name=op.f("ck_governance_programs_aggregate_version"),
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0",
            name=op.f("ck_governance_programs_event_sequence"),
        ),
        sa.CheckConstraint(
            "owner_tenant_id = ANY(participant_tenant_ids)",
            name="ck_governance_programs_owner_participates",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            name=op.f("pk_governance_programs"),
        ),
    )
    op.create_table(
        "governance_members",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("added_by", sa.String(length=128), nullable=False),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('lead', 'contributor', 'reviewer', 'observer')",
            name=op.f("ck_governance_members_role"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["governance_programs.program_id"],
            name=op.f("fk_governance_members_program_id_governance_programs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "principal_id",
            name=op.f("pk_governance_members"),
        ),
    )
    op.create_index(
        "ix_governance_members_tenant_role",
        "governance_members",
        ["tenant_id", "role"],
        unique=False,
    )
    op.create_table(
        "governance_plans",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("lead_id", sa.String(length=128), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_governance_plans_positive_version"),
        ),
        sa.CheckConstraint(
            "length(content_digest) = 64",
            name=op.f("ck_governance_plans_digest"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'discussion', 'approved', 'rejected', 'superseded')",
            name=op.f("ck_governance_plans_state"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["governance_programs.program_id"],
            name=op.f("fk_governance_plans_program_id_governance_programs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "plan_id",
            name=op.f("pk_governance_plans"),
        ),
    )
    op.create_index(
        "ix_governance_plans_program_state",
        "governance_plans",
        ["program_id", "state"],
        unique=False,
    )
    op.create_table(
        "governance_plan_deliverables",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_governance_plan_deliverables_position"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "plan_id"],
            ["governance_plans.program_id", "governance_plans.plan_id"],
            name=op.f("fk_governance_plan_deliverables_program_id_governance_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "plan_id",
            "position",
            name=op.f("pk_governance_plan_deliverables"),
        ),
    )
    op.create_table(
        "governance_plan_approvers",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("approved_digest", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.CheckConstraint(
            "approved_digest IS NULL OR length(approved_digest) = 64",
            name=op.f("ck_governance_plan_approvers_approved_digest"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "plan_id"],
            ["governance_plans.program_id", "governance_plans.plan_id"],
            name=op.f("fk_governance_plan_approvers_program_id_governance_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "plan_id",
            "principal_id",
            name=op.f("pk_governance_plan_approvers"),
        ),
    )
    op.create_table(
        "governance_discussion_items",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("author_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('comment', 'proposal', 'risk', 'objection')",
            name=op.f("ck_governance_discussion_items_kind"),
        ),
        sa.CheckConstraint(
            "(resolved AND resolved_by IS NOT NULL AND resolution IS NOT NULL) "
            "OR (NOT resolved AND resolved_by IS NULL AND resolution IS NULL)",
            name=op.f("ck_governance_discussion_items_resolution"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "plan_id"],
            ["governance_plans.program_id", "governance_plans.plan_id"],
            name=op.f("fk_governance_discussion_items_program_id_governance_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "item_id",
            name=op.f("pk_governance_discussion_items"),
        ),
        sa.UniqueConstraint(
            "program_id",
            "plan_id",
            "item_id",
            name="uq_governance_discussion_plan_item",
        ),
    )
    op.create_index(
        "ix_governance_discussion_plan",
        "governance_discussion_items",
        ["program_id", "plan_id"],
        unique=False,
    )
    op.create_table(
        "governance_assignments",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("deliverable_contract", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(length=128), nullable=False),
        sa.Column("assignee_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("response_reason", sa.Text(), nullable=True),
        sa.Column("verification_note", sa.Text(), nullable=True),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(plan_digest) = 64",
            name=op.f("ck_governance_assignments_plan_digest"),
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'in_progress', 'submitted', "
            "'verified', 'declined')",
            name=op.f("ck_governance_assignments_state"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "plan_id"],
            ["governance_plans.program_id", "governance_plans.plan_id"],
            name=op.f("fk_governance_assignments_program_id_governance_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "assignment_id",
            name=op.f("pk_governance_assignments"),
        ),
    )
    op.create_index(
        "ix_governance_assignments_assignee_state",
        "governance_assignments",
        ["assignee_id", "state"],
        unique=False,
    )
    op.create_table(
        "governance_assignment_dependencies",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("dependency_id", sa.String(length=128), nullable=False),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.CheckConstraint(
            "assignment_id <> dependency_id",
            name=op.f("ck_governance_assignment_dependencies_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "assignment_id"],
            [
                "governance_assignments.program_id",
                "governance_assignments.assignment_id",
            ],
            name="fk_governance_assignment_dependency_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "dependency_id"],
            [
                "governance_assignments.program_id",
                "governance_assignments.assignment_id",
            ],
            name="fk_governance_assignment_dependency_target",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "assignment_id",
            "dependency_id",
            name=op.f("pk_governance_assignment_dependencies"),
        ),
    )
    op.create_table(
        "governance_assignment_artifacts",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("assignment_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("artifact_ref", sa.String(length=1024), nullable=False),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_governance_assignment_artifacts_position"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "assignment_id"],
            [
                "governance_assignments.program_id",
                "governance_assignments.assignment_id",
            ],
            name=op.f("fk_governance_assignment_artifacts_program_id_governance_assignments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "assignment_id",
            "position",
            name=op.f("pk_governance_assignment_artifacts"),
        ),
    )
    op.create_table(
        "governance_events",
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("visible_to_tenants", _tenant_array(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_event_id", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "sequence > 0",
            name=op.f("ck_governance_events_positive_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["governance_programs.program_id"],
            name=op.f("fk_governance_events_program_id_governance_programs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "program_id",
            "sequence",
            name=op.f("pk_governance_events"),
        ),
        sa.UniqueConstraint(
            "program_id",
            "event_id",
            name="uq_governance_events_program_event",
        ),
    )
    op.create_index(
        "ix_governance_events_program_occurred",
        "governance_events",
        ["program_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "governance_outbox",
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("program_id", sa.String(length=128), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("producer_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("recipient_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'published', 'dead_letter')",
            name=op.f("ck_governance_outbox_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_governance_outbox_attempt_count"),
        ),
        sa.CheckConstraint(
            "(status <> 'claimed') OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_governance_outbox_claimed_lease"),
        ),
        sa.ForeignKeyConstraint(
            ["program_id", "event_sequence"],
            ["governance_events.program_id", "governance_events.sequence"],
            name=op.f("fk_governance_outbox_program_id_governance_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "message_id",
            name=op.f("pk_governance_outbox"),
        ),
        sa.UniqueConstraint(
            "program_id",
            "event_sequence",
            "recipient_tenant_id",
            name="uq_governance_outbox_event_recipient",
        ),
    )
    op.create_index(
        "ix_governance_outbox_recipient_status_available",
        "governance_outbox",
        ["recipient_tenant_id", "status", "available_at"],
        unique=False,
    )

    op.execute("ALTER TABLE governance_programs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE governance_programs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY governance_programs_tenant_select
        ON governance_programs FOR SELECT
        USING (
            current_setting('coifesp.tenant_id', true)
            = ANY(participant_tenant_ids)
        )
        """)
    op.execute("""
        CREATE POLICY governance_programs_tenant_insert
        ON governance_programs FOR INSERT
        WITH CHECK (
            current_setting('coifesp.tenant_id', true)
            = ANY(participant_tenant_ids)
        )
        """)
    op.execute("""
        CREATE POLICY governance_programs_tenant_update
        ON governance_programs FOR UPDATE
        USING (
            current_setting('coifesp.tenant_id', true)
            = ANY(participant_tenant_ids)
        )
        WITH CHECK (
            current_setting('coifesp.tenant_id', true)
            = ANY(participant_tenant_ids)
        )
        """)

    for table_name in VISIBLE_TABLES:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_select
            ON {table_name} FOR SELECT
            USING (
                current_setting('coifesp.tenant_id', true)
                = ANY(visible_to_tenants)
            )
            """)
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_insert
            ON {table_name} FOR INSERT
            WITH CHECK (
                current_setting('coifesp.tenant_id', true)
                = ANY(visible_to_tenants)
            )
            """)
        op.execute(f"""
            CREATE POLICY {table_name}_tenant_update
            ON {table_name} FOR UPDATE
            USING (
                current_setting('coifesp.tenant_id', true)
                = ANY(visible_to_tenants)
            )
            WITH CHECK (
                current_setting('coifesp.tenant_id', true)
                = ANY(visible_to_tenants)
            )
            """)

    op.execute("ALTER TABLE governance_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE governance_events FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY governance_events_tenant_select
        ON governance_events FOR SELECT
        USING (
            current_setting('coifesp.tenant_id', true)
            = ANY(visible_to_tenants)
        )
        """)
    op.execute("""
        CREATE POLICY governance_events_tenant_insert
        ON governance_events FOR INSERT
        WITH CHECK (
            current_setting('coifesp.tenant_id', true)
            = ANY(visible_to_tenants)
        )
        """)
    op.execute("""
        CREATE FUNCTION coifesp_reject_governance_event_mutation_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'governance_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_governance_events_reject_update_delete
        BEFORE UPDATE OR DELETE ON governance_events
        FOR EACH ROW
        EXECUTE FUNCTION coifesp_reject_governance_event_mutation_v1()
        """)
    op.execute("""
        CREATE TRIGGER trg_governance_events_reject_truncate
        BEFORE TRUNCATE ON governance_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION coifesp_reject_governance_event_mutation_v1()
        """)
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON governance_events FROM PUBLIC")

    op.execute("ALTER TABLE governance_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE governance_outbox FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY governance_outbox_recipient_select
        ON governance_outbox FOR SELECT
        USING (
            recipient_tenant_id
            = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY governance_outbox_producer_insert
        ON governance_outbox FOR INSERT
        WITH CHECK (
            producer_tenant_id
            = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY governance_outbox_recipient_update
        ON governance_outbox FOR UPDATE
        USING (
            recipient_tenant_id
            = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            recipient_tenant_id
            = current_setting('coifesp.tenant_id', true)
        )
        """)


def downgrade() -> None:
    op.drop_table("governance_outbox")
    op.drop_table("governance_events")
    op.drop_table("governance_assignment_artifacts")
    op.drop_table("governance_assignment_dependencies")
    op.drop_table("governance_assignments")
    op.drop_table("governance_discussion_items")
    op.drop_table("governance_plan_approvers")
    op.drop_table("governance_plan_deliverables")
    op.drop_table("governance_plans")
    op.drop_table("governance_members")
    op.drop_table("governance_programs")
    op.execute("DROP FUNCTION IF EXISTS " "coifesp_reject_governance_event_mutation_v1()")
