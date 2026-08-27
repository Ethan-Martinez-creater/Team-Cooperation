"""Add project notification cursors and team discussion topics.

Revision ID: 20260813_30
Revises: 20260813_29
Create Date: 2026-08-13
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260813_30"
down_revision: str | None = "20260813_29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("product_project_activity_cursors",
        sa.Column("account_id", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), primary_key=True),
        sa.Column("last_read_sequence", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("last_read_sequence>=0"))
    op.create_table("product_project_topics",
        sa.Column("topic_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("proposed_by_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source_agent_run_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decided_by_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('open','decided','cancelled')"),
        sa.CheckConstraint("origin IN ('human','agent_confirmed')"))
    op.create_table("product_project_agent_runs",
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), primary_key=True),
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id"))
    op.create_index("ix_product_topics_project_status", "product_project_topics",
        ["project_id", "status"])
    op.create_table("product_project_topic_contributions",
        sa.Column("contribution_id", sa.String(128), primary_key=True),
        sa.Column("topic_id", sa.String(128),
            sa.ForeignKey("product_project_topics.topic_id"), nullable=False),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_product_topic_contributions_topic_created",
        "product_project_topic_contributions", ["topic_id", "created_at"])


def downgrade() -> None:
    op.drop_table("product_project_topic_contributions")
    op.drop_table("product_project_agent_runs")
    op.drop_table("product_project_topics")
    op.drop_table("product_project_activity_cursors")
