"""Project Work Graph and Plan v2 payload.

Revision ID: 20260829_46
Revises: 20260826_45
Create Date: 2026-08-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260829_46"
down_revision: str | None = "20260826_45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product_project_plan_drafts",
        sa.Column(
            "schema_version",
            sa.String(64),
            nullable=False,
            server_default="coifesp.project-plan.v1",
        ),
    )
    op.add_column(
        "product_project_plan_drafts",
        sa.Column(
            "plan_payload",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "ck_product_plan_drafts_schema_version",
        "product_project_plan_drafts",
        "schema_version IN ('coifesp.project-plan.v1','coifesp.project-plan.v2')",
    )

    op.create_table(
        "project_goals",
        sa.Column("goal_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("success_criteria_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("product_accounts.account_id"),
            nullable=False,
        ),
        sa.Column(
            "approved_by",
            sa.String(128),
            sa.ForeignKey("product_accounts.account_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_project_goals_version"),
    )
    op.create_index("ix_project_goals_project_status", "project_goals", ["project_id", "status"])

    op.create_table(
        "project_requirements",
        sa.Column("requirement_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "goal_id",
            sa.String(128),
            sa.ForeignKey("project_goals.goal_id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requirement_type", sa.String(64), nullable=False),
        sa.Column("priority", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_requirements_project_status",
        "project_requirements",
        ["project_id", "status"],
    )

    op.create_table(
        "project_milestones",
        sa.Column("milestone_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("completion_policy", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_milestones_project_status",
        "project_milestones",
        ["project_id", "status"],
    )

    op.create_table(
        "project_phases",
        sa.Column("phase_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "milestone_id",
            sa.String(128),
            sa.ForeignKey("project_milestones.milestone_id"),
            nullable=True,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "owner_team_id",
            sa.String(128),
            sa.ForeignKey("product_teams.team_id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_phases_project_status",
        "project_phases",
        ["project_id", "status"],
    )

    op.create_table(
        "project_risks",
        sa.Column("risk_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("likelihood", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "owner_team_id",
            sa.String(128),
            sa.ForeignKey("product_teams.team_id"),
            nullable=True,
        ),
        sa.Column("mitigation", sa.Text(), nullable=False),
        sa.Column("source_run_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_risks_project_status", "project_risks", ["project_id", "status"])

    op.create_table(
        "project_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("proposed_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("source_run_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_decisions_project_status",
        "project_decisions",
        ["project_id", "status"],
    )

    op.create_table(
        "project_work_nodes",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("product_projects.project_id"),
            nullable=False,
        ),
        sa.Column("node_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "node_type IN ('goal','requirement','milestone','phase','task','risk',"
            "'decision','artifact','verification')",
            name="ck_work_node_type",
        ),
        sa.UniqueConstraint("node_id", "project_id", name="uq_work_node_project"),
        sa.UniqueConstraint("project_id", "node_type", "subject_id", name="uq_work_node_subject"),
    )
    op.create_index(
        "ix_project_work_nodes_project_type",
        "project_work_nodes",
        ["project_id", "node_type"],
    )

    op.create_table(
        "project_work_relations",
        sa.Column("relation_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("source_node_id", sa.String(128), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("target_node_id", sa.String(128), nullable=False),
        sa.Column("created_by_type", sa.String(32), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        sa.Column("source_run_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_node_id", "project_id"],
            ["project_work_nodes.node_id", "project_work_nodes.project_id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id", "project_id"],
            ["project_work_nodes.node_id", "project_work_nodes.project_id"],
        ),
        sa.CheckConstraint(
            "relation_type IN ('depends_on','blocks','implements','delivers',"
            "'verifies','derived_from','supersedes','relates_to','part_of')",
            name="ck_work_relation_type",
        ),
        sa.CheckConstraint("source_node_id <> target_node_id", name="ck_work_relation_self"),
        sa.UniqueConstraint(
            "project_id",
            "source_node_id",
            "relation_type",
            "target_node_id",
            name="uq_work_relation_semantic",
        ),
    )
    op.create_index(
        "ix_project_work_relations_project_source",
        "project_work_relations",
        ["project_id", "source_node_id"],
    )
    op.create_index(
        "ix_project_work_relations_project_target",
        "project_work_relations",
        ["project_id", "target_node_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_work_relations_project_target",
        table_name="project_work_relations",
    )
    op.drop_index(
        "ix_project_work_relations_project_source",
        table_name="project_work_relations",
    )
    op.drop_table("project_work_relations")
    op.drop_index("ix_project_work_nodes_project_type", table_name="project_work_nodes")
    op.drop_table("project_work_nodes")
    op.drop_index("ix_project_decisions_project_status", table_name="project_decisions")
    op.drop_table("project_decisions")
    op.drop_index("ix_project_risks_project_status", table_name="project_risks")
    op.drop_table("project_risks")
    op.drop_index("ix_project_phases_project_status", table_name="project_phases")
    op.drop_table("project_phases")
    op.drop_index("ix_project_milestones_project_status", table_name="project_milestones")
    op.drop_table("project_milestones")
    op.drop_index("ix_project_requirements_project_status", table_name="project_requirements")
    op.drop_table("project_requirements")
    op.drop_index("ix_project_goals_project_status", table_name="project_goals")
    op.drop_table("project_goals")
    op.drop_constraint(
        "ck_product_plan_drafts_schema_version",
        "product_project_plan_drafts",
        type_="check",
    )
    op.drop_column("product_project_plan_drafts", "plan_payload")
    op.drop_column("product_project_plan_drafts", "schema_version")
