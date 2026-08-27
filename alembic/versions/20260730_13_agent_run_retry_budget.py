"""Add bounded retry scheduling to durable Agent runs.

Revision ID: 20260730_13
Revises: 20260730_12
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260730_13"
down_revision: str | None = "20260730_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "max_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_failure_budget"),
        "agent_runs",
        "failure_count >= 0 AND max_failures BETWEEN 1 AND 20 "
        "AND failure_count <= max_failures",
    )
    op.drop_index("ix_agent_runs_claim", table_name="agent_runs")
    op.create_index(
        "ix_agent_runs_claim",
        "agent_runs",
        ["tenant_id", "status", "next_attempt_at", "updated_at"],
        unique=False,
    )
    op.alter_column("agent_runs", "failure_count", server_default=None)
    op.alter_column("agent_runs", "max_failures", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_claim", table_name="agent_runs")
    op.create_index(
        "ix_agent_runs_claim",
        "agent_runs",
        ["tenant_id", "status", "updated_at"],
        unique=False,
    )
    op.drop_constraint(
        op.f("ck_agent_runs_failure_budget"),
        "agent_runs",
        type_="check",
    )
    op.drop_column("agent_runs", "last_error_code")
    op.drop_column("agent_runs", "next_attempt_at")
    op.drop_column("agent_runs", "max_failures")
    op.drop_column("agent_runs", "failure_count")
