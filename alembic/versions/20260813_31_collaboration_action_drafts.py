"""Add human-reviewed Agent collaboration action drafts.

Revision ID: 20260813_31
Revises: 20260813_30
Create Date: 2026-08-13
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260813_31"
down_revision: str | None = "20260813_30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("product_collaboration_action_drafts",
        sa.Column("draft_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("source_agent_run_id", sa.String(128), nullable=False),
        sa.Column("source_message_sequence", sa.Integer(), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("executed_subject_id", sa.String(128), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_message_sequence>=1"),
        sa.CheckConstraint("action_index>=0"),
        sa.CheckConstraint("kind IN ('message','task','topic')"),
        sa.CheckConstraint("status IN ('pending','executed','rejected')"),
        sa.CheckConstraint("version>=1"),
        sa.CheckConstraint("length(source_content_sha256)=64"),
        sa.UniqueConstraint("project_id", "source_agent_run_id",
            "source_message_sequence", "action_index"))
    op.create_index("ix_product_collaboration_drafts_project_status",
        "product_collaboration_action_drafts", ["project_id", "status"])


def downgrade() -> None:
    op.drop_table("product_collaboration_action_drafts")
