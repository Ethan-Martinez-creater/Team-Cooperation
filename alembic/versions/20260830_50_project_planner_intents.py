"""Persist project Planner intents bound to durable Agent Runs.

Revision ID: 20260830_50
Revises: 20260829_49
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_50"
down_revision: str | None = "20260829_49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_planner_intents",
        sa.Column("planner_intent_id", sa.String(128), primary_key=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("owner_team_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("based_on_process_version", sa.Integer(), nullable=False),
        sa.Column("based_on_event_sequence", sa.Integer(), nullable=False),
        sa.Column("graph_snapshot_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=True, unique=True),
        sa.Column("decision_id", sa.String(128), nullable=True, unique=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.CheckConstraint(
            "based_on_process_version >= 1", name="planner_intent_process_version"
        ),
        sa.CheckConstraint(
            "based_on_event_sequence >= 0", name="planner_intent_event_sequence"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','PROJECTED','STALE','REJECTED','FAILED','CANCELLED')",
            name="planner_intent_status",
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING','RUNNING') AND projected_at IS NULL) OR "
            "(status NOT IN ('PENDING','RUNNING') AND projected_at IS NOT NULL)",
            name="planner_intent_terminal_time",
        ),
    )
    op.create_index(
        "ix_project_planner_intent_status",
        "project_planner_intents",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_planner_intent_status", table_name="project_planner_intents"
    )
    op.drop_table("project_planner_intents")
