"""Team Agent exchanges with immutable shared context packages.

Revision ID: 20260826_40
Revises: 20260826_39
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_40"
down_revision: str | None = "20260826_39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_agent_exchange_drafts",
        sa.Column("draft_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("source_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("source_conversation_id", sa.String(128), nullable=True),
        sa.Column("source_turn_id", sa.String(128), nullable=True),
        sa.Column("purpose", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("constraints", sa.Text(), nullable=False, server_default=""),
        sa.Column("shared_resource_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recipient_team_ids", sa.Text(), nullable=False, server_default="[]"),
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
            "status IN ('drafting','approved','rejected','withdrawn')", name="status"))
    op.create_index("ix_product_exchange_drafts_project_status",
        "product_agent_exchange_drafts", ["project_id", "status"])
    op.create_table(
        "product_agent_exchanges",
        sa.Column("exchange_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("source_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("source_conversation_id", sa.String(128), nullable=True),
        sa.Column("source_turn_id", sa.String(128), nullable=True),
        sa.Column("purpose", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("constraints", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="sent"),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(content_sha256)=64", name="content_sha256"),
        sa.CheckConstraint("status IN ('sent','responded','closed')", name="status"))
    op.create_index("ix_product_exchanges_project_created",
        "product_agent_exchanges", ["project_id", "created_at"])
    op.create_table(
        "product_agent_exchange_recipients",
        sa.Column("exchange_id", sa.String(128),
            sa.ForeignKey("product_agent_exchanges.exchange_id"),
            nullable=False, primary_key=True),
        sa.Column("recipient_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False, primary_key=True),
        sa.Column("context_snapshot", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("response_id", sa.String(128), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','responded','declined')", name="status"))
    op.create_index("ix_product_exchange_recipients_team_status",
        "product_agent_exchange_recipients", ["recipient_team_id", "status"])
    op.create_table(
        "product_agent_exchange_responses",
        sa.Column("response_id", sa.String(128), primary_key=True),
        sa.Column("exchange_id", sa.String(128),
            sa.ForeignKey("product_agent_exchanges.exchange_id"), nullable=False),
        sa.Column("recipient_team_id", sa.String(128),
            sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("turn_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(content_sha256)=64", name="content_sha256"),
        sa.UniqueConstraint("exchange_id", "recipient_team_id",
            name="uq_exchange_team_response"))
    op.create_index("ix_product_exchange_responses_exchange",
        "product_agent_exchange_responses", ["exchange_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_product_exchange_responses_exchange",
        table_name="product_agent_exchange_responses")
    op.drop_table("product_agent_exchange_responses")
    op.drop_index("ix_product_exchange_recipients_team_status",
        table_name="product_agent_exchange_recipients")
    op.drop_table("product_agent_exchange_recipients")
    op.drop_index("ix_product_exchanges_project_created",
        table_name="product_agent_exchanges")
    op.drop_table("product_agent_exchanges")
    op.drop_index("ix_product_exchange_drafts_project_status",
        table_name="product_agent_exchange_drafts")
    op.drop_table("product_agent_exchange_drafts")