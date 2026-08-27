"""Project plan drafts and team requirement drafts.

Revision ID: 20260826_41
Revises: 20260826_40
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_41"
down_revision: str | None = "20260826_40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_project_plan_drafts",
        sa.Column("draft_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("source_conversation_id", sa.String(128), nullable=True),
        sa.Column("source_turn_id", sa.String(128), nullable=True),
        sa.Column("source_run_id", sa.String(128), nullable=True),
        sa.Column("goals", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("phases", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("milestones", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("risks", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("dependencies", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="drafting"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint("length(content_sha256)=64", name="content_sha256"),
        sa.CheckConstraint("version >= 1", name="positive_version"),
        sa.CheckConstraint(
            "status IN ('drafting','approved','rejected')", name="status"))
    op.create_index("ix_product_plan_drafts_project_status",
        "product_project_plan_drafts", ["project_id", "status"])
    op.create_table(
        "product_project_team_requirement_drafts",
        sa.Column("requirement_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("plan_draft_id", sa.String(128),
            sa.ForeignKey("product_project_plan_drafts.draft_id"), nullable=True),
        sa.Column("team_category", sa.String(32), nullable=False),
        sa.Column("team_count", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="drafting"),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("team_count >= 1", name="positive_team_count"),
        sa.CheckConstraint(
            "team_category IN ('product','engineering','quality','design','operations','custom')",
            name="team_category"),
        sa.CheckConstraint(
            "status IN ('drafting','approved','rejected')", name="status"))
    op.create_index("ix_product_team_requirement_project",
        "product_project_team_requirement_drafts", ["project_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_product_team_requirement_project",
        table_name="product_project_team_requirement_drafts")
    op.drop_table("product_project_team_requirement_drafts")
    op.drop_index("ix_product_plan_drafts_project_status",
        table_name="product_project_plan_drafts")
    op.drop_table("product_project_plan_drafts")