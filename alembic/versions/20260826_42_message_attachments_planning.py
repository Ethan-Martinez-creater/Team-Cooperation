"""Message attachments and planning trigger kind.

Revision ID: 20260826_42
Revises: 20260826_41
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_42"
down_revision: str | None = "20260826_41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product_project_conversation_messages",
        sa.Column("attachment_resource_ids", sa.Text(), nullable=False, server_default="[]"),
    )
    op.drop_constraint("trigger_kind", "product_project_agent_turns", type_="check")
    op.create_check_constraint(
        "trigger_kind",
        "product_project_agent_turns",
        "trigger_kind IN ('user_message','exchange','planning')",
    )


def downgrade() -> None:
    op.drop_constraint("trigger_kind", "product_project_agent_turns", type_="check")
    op.create_check_constraint(
        "trigger_kind",
        "product_project_agent_turns",
        "trigger_kind IN ('user_message','exchange')",
    )
    op.drop_column("product_project_conversation_messages", "attachment_resource_ids")