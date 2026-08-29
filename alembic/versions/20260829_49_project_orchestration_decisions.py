"""Persist snapshot-guarded project orchestration decisions and requests.

Revision ID: 20260829_49
Revises: 20260829_48
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260829_49"
down_revision: str | None = "20260829_48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing command rows predate replayable payloads.  Add the column
    # nullable, backfill a canonical empty object for those historical rows,
    # and only then enforce the new invariant.  Batch mode keeps this valid on
    # SQLite while emitting ordinary ALTER statements on PostgreSQL.
    with op.batch_alter_table("project_process_commands") as batch:
        batch.add_column(sa.Column("request_json", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE project_process_commands "
        "SET request_json = '{}' WHERE request_json IS NULL"
    )
    with op.batch_alter_table("project_process_commands") as batch:
        batch.alter_column(
            "request_json",
            existing_type=sa.JSON(),
            nullable=False,
        )

    op.create_table(
        "project_orchestration_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("based_on_process_version", sa.Integer(), nullable=False),
        sa.Column("based_on_event_sequence", sa.Integer(), nullable=False),
        sa.Column("graph_snapshot_digest", sa.String(71), nullable=False),
        sa.Column("command_batch_digest", sa.String(64), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["process_id", "project_id"],
            ["project_processes.process_id", "project_processes.project_id"],
        ),
        sa.CheckConstraint(
            "based_on_process_version >= 1",
            name="project_decision_process_version",
        ),
        sa.CheckConstraint(
            "based_on_event_sequence >= 0",
            name="project_decision_event_sequence",
        ),
        sa.CheckConstraint(
            "length(command_batch_digest) = 64",
            name="project_decision_batch_digest",
        ),
        sa.CheckConstraint(
            "length(decision_digest) = 64",
            name="project_decision_digest",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPLIED','STALE','REJECTED')",
            name="project_decision_status",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND applied_at IS NULL) OR "
            "(status <> 'PENDING' AND applied_at IS NOT NULL)",
            name="project_decision_terminal_time",
        ),
    )
    op.create_index(
        "ix_project_decision_process_status",
        "project_orchestration_decisions",
        ["process_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_decision_process_status",
        table_name="project_orchestration_decisions",
    )
    op.drop_table("project_orchestration_decisions")
    with op.batch_alter_table("project_process_commands") as batch:
        batch.drop_column("request_json")
