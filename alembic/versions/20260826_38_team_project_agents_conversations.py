"""Persistent team project agents and per-user project conversations.

Revision ID: 20260826_38
Revises: 20260822_37
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_38"
down_revision: str | None = "20260822_37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_team_project_agents",
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("memory_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','archived')", name="status"),
        sa.CheckConstraint("memory_version >= 1", name="positive_memory_version"),
        sa.UniqueConstraint("project_id", "team_id",
            name="uq_team_project_agent"))
    op.create_index("ix_product_team_agents_project_status",
        "product_team_project_agents", ["project_id", "status"])
    op.create_table(
        "product_project_conversations",
        sa.Column("conversation_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("team_agent_id", sa.String(128),
            sa.ForeignKey("product_team_project_agents.agent_id"), nullable=False),
        sa.Column("account_id", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_message_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','archived')", name="status"),
        sa.CheckConstraint("last_message_sequence >= 0", name="nonnegative_sequence"),
        sa.UniqueConstraint("project_id", "account_id",
            name="uq_project_account_conversation"))
    op.create_index("ix_product_conversations_account_updated",
        "product_project_conversations", ["account_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_product_conversations_account_updated",
        table_name="product_project_conversations")
    op.drop_table("product_project_conversations")
    op.drop_index("ix_product_team_agents_project_status",
        table_name="product_team_project_agents")
    op.drop_table("product_team_project_agents")