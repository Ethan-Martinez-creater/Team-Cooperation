"""Persist task execution result receipts on project Agent runs.

Revision ID: 20260830_54
Revises: 20260830_53
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_54"
down_revision: str | None = "20260830_53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "product_project_agent_runs"
_RESULT_COLUMNS = (
    "task_contract_version",
    "task_result_status",
    "task_result_json",
    "task_result_at",
)
_RESULT_CHECK = (
    "(task_contract_version IS NULL OR "
    "(run_kind = 'task_execution' AND task_contract_version >= 1)) AND "
    "((task_result_status IS NULL AND task_result_json IS NULL AND task_result_at IS NULL) OR "
    "(task_result_status IS NOT NULL AND run_kind = 'task_execution' AND "
    "task_result_status IN ('submitted','invalid_output','failed','cancelled') AND "
    "task_result_json IS NOT NULL AND task_result_at IS NOT NULL))"
)


def upgrade() -> None:
    columns = [
        sa.Column("task_contract_version", sa.Integer(), nullable=True),
        sa.Column("task_result_status", sa.String(32), nullable=True),
        sa.Column("task_result_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("task_result_at", sa.DateTime(timezone=True), nullable=True),
    ]
    if op.get_bind().dialect.name == "sqlite":
        # Native ADD COLUMN leaves inbound foreign keys, dependent tables,
        # indexes and triggers untouched.  The final column owns the named
        # CHECK so SQLite can add it without rebuilding the parent table.
        columns[-1] = sa.Column(
            "task_result_at",
            sa.DateTime(timezone=True),
            sa.CheckConstraint(_RESULT_CHECK, name="ck_product_project_agent_runs_task_result"),
            nullable=True,
        )
        for column in columns:
            op.add_column(_TABLE, column)
        return

    # PostgreSQL supports ordinary ALTER TABLE ADD COLUMN and CHECK DDL.
    for column in columns:
        op.add_column(_TABLE, column)
    op.create_check_constraint(
        "ck_product_project_agent_runs_task_result",
        _TABLE,
        _RESULT_CHECK,
    )


def downgrade() -> None:
    present = " OR ".join(f"{column} IS NOT NULL" for column in _RESULT_COLUMNS)
    incompatible = op.get_bind().execute(
        sa.text(f"SELECT run_id FROM {_TABLE} WHERE {present} LIMIT 1")
    ).first()
    if incompatible is not None:
        raise RuntimeError(
            "Cannot downgrade 20260830_54: task result receipt or contract version data "
            "cannot be represented by the legacy project Agent run schema; preserve the "
            "receipt fields."
        )

    if op.get_bind().dialect.name == "sqlite":
        version = op.get_bind().execute(sa.text("SELECT sqlite_version()")).scalar_one()
        if tuple(int(part) for part in version.split(".")) < (3, 35, 0):
            raise RuntimeError("SQLite 3.35+ is required for lossless task result column downgrade")
        # Drop the column that owns the CHECK first, then the remaining fields
        # in reverse order.  No table rebuild and no global FK toggle.
        for column in reversed(_RESULT_COLUMNS):
            op.drop_column(_TABLE, column)
        return

    op.drop_constraint("ck_product_project_agent_runs_task_result", _TABLE, type_="check")
    for column in reversed(_RESULT_COLUMNS):
        op.drop_column(_TABLE, column)
