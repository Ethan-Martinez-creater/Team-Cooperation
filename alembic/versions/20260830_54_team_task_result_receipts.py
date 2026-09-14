"""Persist task execution result receipts on project Agent runs.

Revision ID: 20260830_54
Revises: 20260830_53
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from contextlib import contextmanager

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


def _references_table(constraint: sa.Constraint, table_name: str) -> bool:
    if not isinstance(constraint, sa.ForeignKeyConstraint):
        return False
    return any(
        foreign_key.target_fullname.rsplit(".", 1)[0]
        in {table_name, f"public.{table_name}"}
        for foreign_key in constraint.elements
    )


def _inbound_foreign_key_tables(bind, table_name: str) -> tuple[str, ...]:
    if bind.dialect.name != "sqlite":
        return ()
    inspector = sa.inspect(bind)
    return tuple(
        child
        for child in inspector.get_table_names()
        if child != table_name
        and any(
            foreign_key["referred_table"] == table_name
            for foreign_key in inspector.get_foreign_keys(child)
        )
    )


def _trigger_sql(bind, table_name: str) -> tuple[str, ...]:
    rows = bind.execute(
        sa.text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = :table_name AND sql IS NOT NULL"
        ),
        {"table_name": table_name},
    )
    return tuple(row[0] for row in rows)


def _force_recreate(table_name: str, copy_from: sa.Table) -> None:
    first_column = next(iter(copy_from.columns))
    with op.batch_alter_table(table_name, recreate="always", copy_from=copy_from) as batch:
        batch.alter_column(first_column.name, existing_type=first_column.type)


@contextmanager
def _without_inbound_foreign_keys(bind, table_name: str):
    if bind.dialect.name != "sqlite":
        yield
        return

    inspector = sa.inspect(bind)
    depths = {table_name: 0}
    changed = True
    while changed:
        changed = False
        for child_name in inspector.get_table_names():
            if child_name in depths:
                continue
            parents = {
                foreign_key["referred_table"]
                for foreign_key in inspector.get_foreign_keys(child_name)
            }
            parent_depths = [depths[parent] for parent in parents if parent in depths]
            if parent_depths:
                depths[child_name] = min(parent_depths) + 1
                changed = True

    plans = []
    affected = set(depths)
    for child_name, depth in depths.items():
        if child_name == table_name:
            continue
        reflected = sa.Table(child_name, sa.MetaData(), autoload_with=bind)
        detached = reflected.to_metadata(sa.MetaData())
        for constraint in list(detached.constraints):
            if isinstance(constraint, sa.ForeignKeyConstraint) and any(
                foreign_key.target_fullname.rsplit(".", 1)[0] in affected
                for foreign_key in constraint.elements
            ):
                detached.constraints.remove(constraint)
        plans.append((depth, child_name, reflected, detached, _trigger_sql(bind, child_name)))

    parent_triggers = _trigger_sql(bind, table_name)
    bind.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    for _depth, child_name, _reflected, detached, _triggers in sorted(
        plans, reverse=True
    ):
        _force_recreate(child_name, detached)
    try:
        yield
    finally:
        for _depth, child_name, reflected, _detached, triggers in sorted(plans):
            _force_recreate(child_name, reflected)
            for trigger in triggers:
                bind.exec_driver_sql(trigger)
        for trigger in parent_triggers:
            bind.exec_driver_sql(trigger)


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
        # SQLite cannot drop a column referenced by a table CHECK. Rebuild the
        # table so the CHECK is removed before any receipt column disappears.
        with _without_inbound_foreign_keys(op.get_bind(), _TABLE), op.batch_alter_table(
            _TABLE, recreate="always"
        ) as batch:
            batch.drop_constraint(
                "ck_product_project_agent_runs_task_result", type_="check"
            )
            for column in reversed(_RESULT_COLUMNS):
                batch.drop_column(column)
        return

    op.drop_constraint("ck_product_project_agent_runs_task_result", _TABLE, type_="check")
    for column in reversed(_RESULT_COLUMNS):
        op.drop_column(_TABLE, column)
