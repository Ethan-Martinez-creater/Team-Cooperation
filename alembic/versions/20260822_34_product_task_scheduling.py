"""Add team task scheduling and schedule change proposals.

Revision ID: 20260822_34
Revises: 20260816_33
Create Date: 2026-08-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260822_34"
down_revision: str | None = "20260816_33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product_team_tasks",
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"))
    op.add_column("product_team_tasks",
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("product_team_tasks",
        sa.Column("schedule_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("product_team_tasks",
        sa.Column("due_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("product_team_tasks",
        sa.Column("due_changed_by", sa.String(128), nullable=True))
    op.create_foreign_key("fk_product_tasks_due_changed_by",
        "product_team_tasks", "product_accounts", ["due_changed_by"], ["account_id"])
    op.add_column("product_team_tasks",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_product_tasks_priority",
        "product_team_tasks", "priority IN ('low','normal','high','urgent')")
    op.create_check_constraint("ck_product_tasks_schedule_version",
        "product_team_tasks", "schedule_version >= 1")
    op.create_index("ix_product_tasks_team_status_due", "product_team_tasks",
        ["target_team_id", "status", "due_at"])
    op.create_index("ix_product_tasks_project_priority_due", "product_team_tasks",
        ["project_id", "priority", "due_at"])
    op.create_table("product_task_schedule_proposals",
        sa.Column("proposal_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("task_id", sa.String(128),
            sa.ForeignKey("product_team_tasks.task_id"), nullable=False),
        sa.Column("proposed_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("proposed_by_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("decided_by_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=True),
        sa.Column("old_priority", sa.String(16), nullable=False),
        sa.Column("new_priority", sa.String(16), nullable=False),
        sa.Column("old_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schedule_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded')"),
        sa.CheckConstraint("old_priority IN ('low','normal','high','urgent')"),
        sa.CheckConstraint("new_priority IN ('low','normal','high','urgent')"),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("schedule_version >= 1"))
    op.create_index("ix_product_schedule_proposals_task_status",
        "product_task_schedule_proposals", ["task_id", "status"])
    op.create_index("ix_product_schedule_proposals_project_created",
        "product_task_schedule_proposals", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_product_schedule_proposals_project_created",
        table_name="product_task_schedule_proposals")
    op.drop_index("ix_product_schedule_proposals_task_status",
        table_name="product_task_schedule_proposals")
    op.drop_table("product_task_schedule_proposals")
    op.drop_index("ix_product_tasks_project_priority_due", table_name="product_team_tasks")
    op.drop_index("ix_product_tasks_team_status_due", table_name="product_team_tasks")
    op.drop_constraint("ck_product_tasks_schedule_version", "product_team_tasks", type_="check")
    op.drop_constraint("ck_product_tasks_priority", "product_team_tasks", type_="check")
    op.drop_column("product_team_tasks", "completed_at")
    op.drop_constraint("fk_product_tasks_due_changed_by", "product_team_tasks", type_="foreignkey")
    op.drop_column("product_team_tasks", "due_changed_by")
    op.drop_column("product_team_tasks", "due_changed_at")
    op.drop_column("product_team_tasks", "schedule_version")
    op.drop_column("product_team_tasks", "due_at")
    op.drop_column("product_team_tasks", "priority")
