"""Persist cumulative model cost across Agent Run recovery.

Revision ID: 20260806_16
Revises: 20260806_15
Create Date: 2026-08-06
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260806_16"
down_revision: str | None = "20260806_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "model_cost_microusd",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.drop_constraint(
        op.f("ck_agent_runs_usage_nonnegative"),
        "agent_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_usage_nonnegative"),
        "agent_runs",
        "turns >= 0 AND tool_calls >= 0 AND total_tokens >= 0 "
        "AND model_cost_microusd >= 0",
    )
    op.alter_column("agent_runs", "model_cost_microusd", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_agent_runs_usage_nonnegative"),
        "agent_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_usage_nonnegative"),
        "agent_runs",
        "turns >= 0 AND tool_calls >= 0 AND total_tokens >= 0",
    )
    op.drop_column("agent_runs", "model_cost_microusd")
