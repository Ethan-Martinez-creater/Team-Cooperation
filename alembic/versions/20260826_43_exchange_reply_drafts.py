"""Recipient-side Agent reply drafts for exchanges.

Revision ID: 20260826_43
Revises: 20260826_42
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_43"
down_revision: str | None = "20260826_42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product_agent_exchange_recipients",
        sa.Column("draft_content", sa.Text(), nullable=True),
    )
    op.add_column(
        "product_agent_exchange_recipients",
        sa.Column("draft_turn_id", sa.String(128), nullable=True),
    )
    op.drop_constraint("status", "product_agent_exchange_recipients", type_="check")
    op.create_check_constraint(
        "status",
        "product_agent_exchange_recipients",
        "status IN ('pending','drafting','responded','declined')",
    )
    op.create_index(
        "ix_product_exchange_recipients_draft_turn",
        "product_agent_exchange_recipients",
        ["draft_turn_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_exchange_recipients_draft_turn",
        table_name="product_agent_exchange_recipients",
    )
    op.drop_constraint("status", "product_agent_exchange_recipients", type_="check")
    op.create_check_constraint(
        "status",
        "product_agent_exchange_recipients",
        "status IN ('pending','responded','declined')",
    )
    op.drop_column("product_agent_exchange_recipients", "draft_turn_id")
    op.drop_column("product_agent_exchange_recipients", "draft_content")