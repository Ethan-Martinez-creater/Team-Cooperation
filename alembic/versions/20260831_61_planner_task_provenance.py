"""Persist Planner provenance on structured TeamTasks.

Revision ID: 20260831_61
Revises: 20260831_60
Create Date: 2026-08-31
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_61"
down_revision: str | None = "20260831_60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "product_team_tasks"
_CREATED_BY = "created_by"
_PROVENANCE_COLUMNS = (
    "produced_by_principal_id",
    "source_planner_run_id",
    "source_planner_command_id",
)
_CREATOR_CHECK_NAME = "ck_product_team_tasks_creator"
_CREATOR_CHECK = (
    "(created_by IS NOT NULL AND produced_by_principal_id IS NULL AND "
    "source_planner_run_id IS NULL AND source_planner_command_id IS NULL) OR "
    "(created_by IS NULL AND "
    "produced_by_principal_id = 'service:project-orchestrator' AND "
    "source_planner_run_id IS NOT NULL AND length(source_planner_run_id) > 0 AND "
    "source_planner_command_id IS NOT NULL AND "
    "length(source_planner_command_id) > 0 AND "
    "process_id IS NOT NULL AND length(process_id) > 0 AND "
    "source_decision_id IS NOT NULL AND length(source_decision_id) > 0)"
)
_SOURCE_COMMAND_INDEX = "uq_product_team_tasks_source_planner_command_id"
_CREATED_BY_NOT_NULL = re.compile(
    r"(?P<prefix>\"?created_by\"?\s+VARCHAR\(128\))\s+NOT\s+NULL",
    re.IGNORECASE,
)
_CREATED_BY_DECLARATION = re.compile(
    r"(?P<prefix>\"?created_by\"?\s+VARCHAR\(128\))(?!\s+NOT\s+NULL)",
    re.IGNORECASE,
)


def _rewrite_sqlite_created_by(bind, pattern, replacement: str, action: str) -> None:
    # SQLite has no ALTER COLUMN operation.  A narrowly validated schema-text
    # rewrite changes only this existing column declaration, preserving the
    # parent table object, its account FK, all child FKs, indexes and rows.
    table_sql = bind.execute(
        sa.text(
            "SELECT sql FROM sqlite_schema "
            "WHERE type = 'table' AND name = :table_name"
        ),
        {"table_name": _TABLE},
    ).scalar_one()
    rewritten, count = pattern.subn(replacement, table_sql)
    if count != 1:
        raise RuntimeError(
            f"Cannot make product_team_tasks.created_by {action}: "
            "the existing column declaration is not unique."
        )
    bind.execute(sa.text("PRAGMA writable_schema = ON"))
    try:
        bind.execute(
            sa.text(
                "UPDATE sqlite_schema SET sql = :table_sql "
                "WHERE type = 'table' AND name = :table_name"
            ),
            {"table_sql": rewritten, "table_name": _TABLE},
        )
        schema_version = bind.exec_driver_sql("PRAGMA schema_version").scalar_one()
        bind.exec_driver_sql(f"PRAGMA schema_version = {schema_version + 1}")
    finally:
        bind.execute(sa.text("PRAGMA writable_schema = OFF"))


def _set_sqlite_created_by_nullable(bind) -> None:
    _rewrite_sqlite_created_by(
        bind,
        _CREATED_BY_NOT_NULL,
        r"\g<prefix>",
        "nullable",
    )


def _set_sqlite_created_by_required(bind) -> None:
    _rewrite_sqlite_created_by(
        bind,
        _CREATED_BY_DECLARATION,
        r"\g<prefix> NOT NULL",
        "NOT NULL",
    )


def _add_provenance_columns() -> None:
    columns = [
        sa.Column("produced_by_principal_id", sa.String(256), nullable=True),
        sa.Column("source_planner_run_id", sa.String(128), nullable=True),
        sa.Column("source_planner_command_id", sa.String(128), nullable=True),
    ]
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot add a table-level CHECK with ALTER TABLE.  Attaching
        # the CHECK to the final nullable column keeps the parent table and
        # every child FK untouched, as in revision 59.
        columns[-1] = sa.Column(
            "source_planner_command_id",
            sa.String(128),
            sa.CheckConstraint(_CREATOR_CHECK, name=_CREATOR_CHECK_NAME),
            nullable=True,
        )
    for column in columns:
        op.add_column(_TABLE, column)
    op.create_index(
        _SOURCE_COMMAND_INDEX,
        _TABLE,
        ["source_planner_command_id"],
        unique=True,
    )
    if op.get_bind().dialect.name != "sqlite":
        op.create_check_constraint(_CREATOR_CHECK_NAME, _TABLE, _CREATOR_CHECK)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _set_sqlite_created_by_nullable(bind)
    else:
        op.alter_column(
            _TABLE,
            _CREATED_BY,
            existing_type=sa.String(128),
            nullable=True,
        )
    _add_provenance_columns()


def _reject_planner_provenance(bind) -> None:
    present = " OR ".join(
        [f"{_CREATED_BY} IS NULL"]
        + [f"{column} IS NOT NULL" for column in _PROVENANCE_COLUMNS]
    )
    incompatible = bind.execute(
        sa.text(f"SELECT task_id FROM {_TABLE} WHERE {present} LIMIT 1")
    ).first()
    if incompatible is not None:
        raise RuntimeError(
            "Cannot downgrade 20260831_61: Planner provenance cannot be "
            "represented by the legacy TeamTask schema; preserve the provenance fields."
        )


def downgrade() -> None:
    bind = op.get_bind()
    _reject_planner_provenance(bind)
    if bind.dialect.name == "sqlite":
        _require_sqlite_drop_column(bind)
    op.drop_index(_SOURCE_COMMAND_INDEX, table_name=_TABLE)
    if bind.dialect.name == "sqlite":
        # The final column owns the SQLite CHECK, so drop it first.  Native
        # DROP COLUMN preserves inbound FKs and avoids rebuilding the parent.
        for column in reversed(_PROVENANCE_COLUMNS):
            op.drop_column(_TABLE, column)
        _set_sqlite_created_by_required(bind)
        return

    op.drop_constraint(_CREATOR_CHECK_NAME, _TABLE, type_="check")
    for column in reversed(_PROVENANCE_COLUMNS):
        op.drop_column(_TABLE, column)
    op.alter_column(
        _TABLE,
        _CREATED_BY,
        existing_type=sa.String(128),
        nullable=False,
    )


def _require_sqlite_drop_column(bind) -> None:
    version = bind.execute(sa.text("SELECT sqlite_version()")).scalar_one()
    if tuple(int(part) for part in version.split(".")) < (3, 35, 0):
        raise RuntimeError("SQLite 3.35+ is required for lossless provenance column downgrade")
