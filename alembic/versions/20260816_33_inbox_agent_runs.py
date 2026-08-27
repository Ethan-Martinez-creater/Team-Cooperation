"""Add account-scoped collaboration inbox Agent run bindings.

Revision ID: 20260816_33
Revises: 20260813_32
Create Date: 2026-08-16
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260816_33"
down_revision: str | None = "20260813_32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_inbox_agent_runs",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column(
            "team_id",
            sa.String(128),
            sa.ForeignKey("product_teams.team_id"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("product_accounts.account_id"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('prioritization','status_briefing')"),
    )
    op.create_index(
        "ix_product_inbox_agent_runs_creator_created",
        "product_inbox_agent_runs",
        ["created_by", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("product_inbox_agent_runs")
