"""Unique exchange-draft source turns.

Revision ID: 20260826_45
Revises: 20260826_44
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_45"
down_revision: str | None = "20260826_44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One exchange draft per generating turn so concurrent terminal callbacks
    # cannot produce two drafts; multiple NULLs stay allowed for manually
    # created drafts.
    op.create_unique_constraint(
        "uq_exchange_drafts_source_turn",
        "product_agent_exchange_drafts",
        ["source_turn_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_exchange_drafts_source_turn", "product_agent_exchange_drafts", type_="unique"
    )