"""Persist bounded Specialist delegation and Agent-as-Tool waiting state.

Revision ID: 20260901_63
Revises: 20260901_62
Create Date: 2026-09-01
"""

from collections.abc import Sequence
from contextlib import contextmanager

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_63"
down_revision: str | None = "20260901_62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_TABLE = "product_project_agent_runs"
_DELEGATION_TABLE = "product_specialist_delegations"
_TOOL_JOB_TABLE = "tool_jobs"
_RUN_ATTEMPT_UNIQUE = "uq_product_project_agent_run_task_attempt"
_RUN_LEGACY_CHECK = "ck_product_project_agent_runs_legacy_identity"
_RUN_SPECIALIST_CHECK = "ck_product_project_agent_runs_specialist"
_TOOL_STATUS_CHECK_CANDIDATES = {"status", "ck_tool_jobs_status"}
_TOOL_STATUS_CHECK = (
    "status IN ('queued','leased','running','retry_wait','awaiting_specialist',"
    "'succeeded','failed','cancelled')"
)
_TOOL_STATUS_CHECK_NAME = "ck_tool_jobs_status"
_OLD_RUN_LEGACY_CHECK = (
    "run_kind = 'task_execution' OR (created_by IS NOT NULL AND mode IS NOT NULL)"
)
_NEW_RUN_LEGACY_CHECK = (
    "run_kind IN ('task_execution','specialist') OR "
    "(created_by IS NOT NULL AND mode IS NOT NULL)"
)
_SPECIALIST_RUN_CHECK = (
    "run_kind <> 'specialist' OR ("
    "project_id IS NOT NULL AND process_id IS NOT NULL AND "
    "team_agent_id IS NOT NULL AND work_node_id IS NOT NULL AND "
    "team_task_id IS NOT NULL AND parent_run_id IS NOT NULL AND "
    "initiated_by_principal_id IS NOT NULL AND "
    "executed_as_principal_id IS NOT NULL AND "
    "delegation_scope_digest IS NOT NULL AND "
    "project_budget_reservation_id IS NOT NULL AND "
    "created_by IS NULL AND mode IS NULL AND "
    "length(delegation_scope_digest) = 64 AND "
    "initiated_by_principal_id = 'team-agent:' || team_id AND "
    "length(executed_as_principal_id) > "
    "length('specialist-agent:' || team_id || ':') AND "
    "executed_as_principal_id LIKE 'specialist-agent:' || team_id || ':%')"
)
_DELEGATION_LIFECYCLE_CHECK = (
    "(status IN ('PENDING','RUNNING') AND completed_at IS NULL AND "
    "result_json IS NULL AND error_code IS NULL) OR "
    "(status = 'COMPLETED' AND completed_at IS NOT NULL AND "
    "result_json IS NOT NULL AND error_code IS NULL) OR "
    "(status = 'FAILED' AND completed_at IS NOT NULL AND "
    "result_json IS NULL AND error_code IS NOT NULL) OR "
    "(status = 'CANCELLED' AND completed_at IS NOT NULL AND "
    "result_json IS NULL)"
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


def _force_recreate(table_name: str, copy_from: sa.Table) -> None:
    """Recreate one SQLite child while retaining its original definition."""

    first_column = next(iter(copy_from.columns))
    with op.batch_alter_table(table_name, recreate="always", copy_from=copy_from) as batch:
        # A no-op alteration forces Alembic's batch copier to use copy_from.
        batch.alter_column(first_column.name, existing_type=first_column.type)


@contextmanager
def _without_inbound_foreign_keys(bind, table_name: str):
    """Temporarily detach only child FKs while a parent is batch-rebuilt.

    SQLite cannot drop/recreate a referenced parent with foreign_keys=ON.
    Recreating the children without the inbound constraint keeps the pragma
    enabled and restores the exact child definition after the parent DDL.
    """

    if bind.dialect.name != "sqlite":
        yield
        return

    plans = []
    for child_name in _inbound_foreign_key_tables(bind, table_name):
        reflected = sa.Table(child_name, sa.MetaData(), autoload_with=bind)
        detached = reflected.to_metadata(sa.MetaData())
        for constraint in list(detached.constraints):
            if _references_table(constraint, table_name):
                detached.constraints.remove(constraint)
        plans.append((child_name, reflected, detached))

    for child_name, _reflected, detached in plans:
        _force_recreate(child_name, detached)
    try:
        yield
    finally:
        for child_name, reflected, _detached in plans:
            _force_recreate(child_name, reflected)


def _find_check_name(bind, table_name: str, candidates: set[str]) -> str:
    names = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(table_name)
    }
    for candidate in candidates:
        if candidate in names:
            return candidate
    raise RuntimeError(
        f"Cannot update {table_name}: expected status CHECK was not found"
    )


def _create_specialist_delegation_table() -> None:
    op.create_table(
        _DELEGATION_TABLE,
        sa.Column("delegation_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey(
                "product_projects.project_id",
                name="fk_specialist_delegation_project",
            ),
            nullable=False,
        ),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("work_node_id", sa.String(128), nullable=False),
        sa.Column(
            "team_id",
            sa.String(128),
            sa.ForeignKey("product_teams.team_id", name="fk_specialist_delegation_team"),
            nullable=False,
        ),
        sa.Column(
            "team_agent_id",
            sa.String(128),
            sa.ForeignKey(
                "product_team_project_agents.agent_id",
                name="fk_specialist_delegation_team_agent",
            ),
            nullable=False,
        ),
        sa.Column(
            "team_task_id",
            sa.String(128),
            sa.ForeignKey(
                "product_team_tasks.task_id",
                name="fk_specialist_delegation_team_task",
            ),
            nullable=False,
        ),
        sa.Column("parent_run_id", sa.String(128), nullable=False),
        sa.Column("child_run_id", sa.String(128), nullable=False),
        sa.Column("tool_job_tenant_id", sa.String(128), nullable=False),
        sa.Column("tool_job_id", sa.String(128), nullable=False),
        sa.Column("specialist_kind", sa.String(128), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("request_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("context_scope_digest", sa.String(64), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("output_schema_digest", sa.String(64), nullable=False),
        sa.Column("project_budget_reservation_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("result_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("delegation_id", name="pk_product_specialist_delegations"),
        sa.ForeignKeyConstraint(
            ["tool_job_tenant_id", "tool_job_id"],
            ["tool_jobs.tenant_id", "tool_jobs.job_id"],
            name="fk_specialist_delegation_tool_job",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name="positive_idempotency_key",
        ),
        sa.CheckConstraint(
            "length(specialist_kind) > 0",
            name="positive_specialist_kind",
        ),
        sa.CheckConstraint("depth >= 1", name="positive_depth"),
        sa.CheckConstraint(
            "length(context_scope_digest) = 64",
            name="context_scope_digest",
        ),
        sa.CheckConstraint("length(profile_digest) = 64", name="profile_digest"),
        sa.CheckConstraint(
            "length(output_schema_digest) = 64",
            name="output_schema_digest",
        ),
        sa.CheckConstraint(
            "tool_job_tenant_id = team_id",
            name="tool_job_tenant",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')",
            name="specialist_delegation_status",
        ),
        sa.CheckConstraint(
            _DELEGATION_LIFECYCLE_CHECK,
            name="specialist_delegation_lifecycle",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_product_specialist_delegation_idempotency",
        ),
        sa.UniqueConstraint(
            "child_run_id",
            name="uq_product_specialist_delegation_child_run",
        ),
        sa.UniqueConstraint(
            "tool_job_id",
            name="uq_product_specialist_delegation_tool_job",
        ),
    )
    op.create_index(
        "ix_product_specialist_delegations_project_status",
        _DELEGATION_TABLE,
        ["project_id", "status"],
    )
    op.create_index(
        "ix_product_specialist_delegations_parent_run",
        _DELEGATION_TABLE,
        ["parent_run_id"],
    )


def _upgrade_project_agent_runs(bind) -> None:
    if bind.dialect.name == "sqlite":
        with _without_inbound_foreign_keys(bind, _RUN_TABLE), op.batch_alter_table(
            _RUN_TABLE
        ) as batch:
            batch.drop_constraint(_RUN_ATTEMPT_UNIQUE, type_="unique")
            batch.drop_constraint(_RUN_LEGACY_CHECK, type_="check")
            batch.create_check_constraint(_RUN_LEGACY_CHECK, _NEW_RUN_LEGACY_CHECK)
            batch.create_check_constraint(_RUN_SPECIALIST_CHECK, _SPECIALIST_RUN_CHECK)
    else:
        op.drop_constraint(_RUN_ATTEMPT_UNIQUE, _RUN_TABLE, type_="unique")
        op.drop_constraint(_RUN_LEGACY_CHECK, _RUN_TABLE, type_="check")
        op.create_check_constraint(_RUN_LEGACY_CHECK, _RUN_TABLE, _NEW_RUN_LEGACY_CHECK)
        op.create_check_constraint(_RUN_SPECIALIST_CHECK, _RUN_TABLE, _SPECIALIST_RUN_CHECK)
    op.create_index(
        _RUN_ATTEMPT_UNIQUE,
        _RUN_TABLE,
        ["process_id", "team_task_id", "execution_attempt"],
        unique=True,
        postgresql_where=sa.text("run_kind = 'task_execution'"),
        sqlite_where=sa.text("run_kind = 'task_execution'"),
    )


def _upgrade_tool_jobs(bind) -> None:
    check_name = (
        _find_check_name(bind, _TOOL_JOB_TABLE, _TOOL_STATUS_CHECK_CANDIDATES)
        if bind.dialect.name == "sqlite"
        else _TOOL_STATUS_CHECK_NAME
    )
    if bind.dialect.name == "sqlite":
        with _without_inbound_foreign_keys(bind, _TOOL_JOB_TABLE), op.batch_alter_table(
            _TOOL_JOB_TABLE
        ) as batch:
            batch.drop_constraint(check_name, type_="check")
            batch.create_check_constraint(_TOOL_STATUS_CHECK_NAME, _TOOL_STATUS_CHECK)
    else:
        op.drop_constraint(check_name, _TOOL_JOB_TABLE, type_="check")
        op.create_check_constraint(_TOOL_STATUS_CHECK_NAME, _TOOL_JOB_TABLE, _TOOL_STATUS_CHECK)


def upgrade() -> None:
    bind = op.get_bind()
    _create_specialist_delegation_table()
    _upgrade_project_agent_runs(bind)
    _upgrade_tool_jobs(bind)


def _reject_specialist_rows(bind) -> None:
    run_row = bind.execute(
        sa.text(
            f"SELECT run_id FROM {_RUN_TABLE} "
            "WHERE run_kind = 'specialist' LIMIT 1"
        )
    ).first()
    if run_row is not None:
        raise RuntimeError(
            "Cannot downgrade 20260901_63: specialist Agent runs cannot be "
            "represented by the legacy project Agent run schema; preserve the "
            "specialist run data."
        )
    delegation_row = bind.execute(
        sa.text(
            f"SELECT delegation_id FROM {_DELEGATION_TABLE} LIMIT 1"
        )
    ).first()
    if delegation_row is not None:
        raise RuntimeError(
            "Cannot downgrade 20260901_63: specialist delegation data cannot be "
            "represented by the legacy schema; preserve the delegation rows."
        )


def _downgrade_project_agent_runs(bind) -> None:
    op.drop_index(_RUN_ATTEMPT_UNIQUE, table_name=_RUN_TABLE)
    if bind.dialect.name == "sqlite":
        with _without_inbound_foreign_keys(bind, _RUN_TABLE), op.batch_alter_table(
            _RUN_TABLE
        ) as batch:
            batch.drop_constraint(_RUN_SPECIALIST_CHECK, type_="check")
            batch.drop_constraint(_RUN_LEGACY_CHECK, type_="check")
            batch.create_check_constraint(_RUN_LEGACY_CHECK, _OLD_RUN_LEGACY_CHECK)
            batch.create_unique_constraint(
                _RUN_ATTEMPT_UNIQUE,
                ["process_id", "team_task_id", "execution_attempt"],
            )
    else:
        op.drop_constraint(_RUN_SPECIALIST_CHECK, _RUN_TABLE, type_="check")
        op.drop_constraint(_RUN_LEGACY_CHECK, _RUN_TABLE, type_="check")
        op.create_check_constraint(_RUN_LEGACY_CHECK, _RUN_TABLE, _OLD_RUN_LEGACY_CHECK)
        op.create_unique_constraint(
            _RUN_ATTEMPT_UNIQUE,
            _RUN_TABLE,
            ["process_id", "team_task_id", "execution_attempt"],
        )


def _downgrade_tool_jobs(bind) -> None:
    check_name = (
        _find_check_name(bind, _TOOL_JOB_TABLE, {_TOOL_STATUS_CHECK_NAME, "status"})
        if bind.dialect.name == "sqlite"
        else _TOOL_STATUS_CHECK_NAME
    )
    old_status_check = (
        "status IN ('queued','leased','running','retry_wait','succeeded','failed','cancelled')"
    )
    if bind.dialect.name == "sqlite":
        with _without_inbound_foreign_keys(bind, _TOOL_JOB_TABLE), op.batch_alter_table(
            _TOOL_JOB_TABLE
        ) as batch:
            batch.drop_constraint(check_name, type_="check")
            batch.create_check_constraint(_TOOL_STATUS_CHECK_NAME, old_status_check)
    else:
        op.drop_constraint(check_name, _TOOL_JOB_TABLE, type_="check")
        op.create_check_constraint(_TOOL_STATUS_CHECK_NAME, _TOOL_JOB_TABLE, old_status_check)


def downgrade() -> None:
    bind = op.get_bind()
    # Check all durable specialist state before issuing any DDL.
    _reject_specialist_rows(bind)
    _downgrade_tool_jobs(bind)
    _downgrade_project_agent_runs(bind)
    op.drop_index(
        "ix_product_specialist_delegations_parent_run",
        table_name=_DELEGATION_TABLE,
    )
    op.drop_index(
        "ix_product_specialist_delegations_project_status",
        table_name=_DELEGATION_TABLE,
    )
    op.drop_table(_DELEGATION_TABLE)
