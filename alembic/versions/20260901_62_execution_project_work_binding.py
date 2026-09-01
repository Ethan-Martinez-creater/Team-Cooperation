"""Bind durable execution tasks to versioned Project Work contracts.

Revision ID: 20260901_62
Revises: 20260831_61
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_62"
down_revision: str | None = "20260831_61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "execution_tasks"
_COLUMNS = (
    "project_id",
    "process_id",
    "team_task_id",
    "work_node_id",
    "contract_version",
)
_CHECK_NAME = "ck_execution_tasks_project_work_binding"
_CHECK = (
    "(project_id IS NULL AND process_id IS NULL AND team_task_id IS NULL "
    "AND work_node_id IS NULL AND contract_version IS NULL) OR "
    "(project_id IS NOT NULL AND process_id IS NOT NULL "
    "AND team_task_id IS NOT NULL AND work_node_id IS NOT NULL "
    "AND contract_version >= 1 AND program_id IS NULL AND assignment_id IS NULL)"
)
_UNIQUE = "uq_execution_tasks_project_contract"
_LOOKUP = "ix_execution_tasks_project_work"


def upgrade() -> None:
    columns = [
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("process_id", sa.String(128), nullable=True),
        sa.Column("team_task_id", sa.String(128), nullable=True),
        sa.Column("work_node_id", sa.String(128), nullable=True),
        sa.Column("contract_version", sa.Integer(), nullable=True),
    ]
    if op.get_bind().dialect.name == "sqlite":
        columns[-1] = sa.Column(
            "contract_version",
            sa.Integer(),
            sa.CheckConstraint(_CHECK, name=_CHECK_NAME),
            nullable=True,
        )
    for column in columns:
        op.add_column(_TABLE, column)
    if op.get_bind().dialect.name != "sqlite":
        op.create_check_constraint(_CHECK_NAME, _TABLE, _CHECK)
    op.create_index(
        _UNIQUE,
        _TABLE,
        ["process_id", "team_task_id", "contract_version"],
        unique=True,
        postgresql_where=sa.text("process_id IS NOT NULL"),
        sqlite_where=sa.text("process_id IS NOT NULL"),
    )
    op.create_index(
        _LOOKUP,
        _TABLE,
        ["project_id", "process_id", "team_task_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    present = " OR ".join(f"{column} IS NOT NULL" for column in _COLUMNS)
    incompatible = bind.execute(
        sa.text(f"SELECT task_id FROM {_TABLE} WHERE {present} LIMIT 1")
    ).first()
    if incompatible is not None:
        raise RuntimeError(
            "Cannot downgrade 20260901_62: Project Work execution bindings "
            "cannot be represented by the legacy execution schema."
        )
    op.drop_index(_LOOKUP, table_name=_TABLE)
    op.drop_index(_UNIQUE, table_name=_TABLE)
    if bind.dialect.name == "sqlite":
        version = bind.execute(sa.text("SELECT sqlite_version()")) .scalar_one()
        if tuple(int(part) for part in version.split(".")) < (3, 35, 0):
            raise RuntimeError(
                "SQLite 3.35+ is required for lossless Project Work binding downgrade"
            )
        for column in reversed(_COLUMNS):
            op.drop_column(_TABLE, column)
        return
    op.drop_constraint(_CHECK_NAME, _TABLE, type_="check")
    for column in reversed(_COLUMNS):
        op.drop_column(_TABLE, column)
