"""Bind autonomous task runs to Team Agent service identities.

Revision ID: 20260830_52
Revises: 20260830_51
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_52"
down_revision: str | None = "20260830_51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "product_project_agent_runs"
_BINDING_IDS = (
    "process_id",
    "team_agent_id",
    "work_node_id",
    "team_task_id",
    "parent_run_id",
    "orchestration_decision_id",
    "capacity_reservation_id",
    "project_budget_reservation_id",
)


def upgrade() -> None:
    # Batch mode preserves the original account FK, mode CHECK, conversation
    # index and legacy rows while supporting SQLite as well as PostgreSQL.
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column("created_by", existing_type=sa.String(128), nullable=True)
        batch.alter_column("mode", existing_type=sa.String(32), nullable=True)
        for name in _BINDING_IDS:
            batch.add_column(sa.Column(name, sa.String(128), nullable=True))
        batch.add_column(
            sa.Column("run_kind", sa.String(32), nullable=False, server_default="conversation")
        )
        batch.add_column(sa.Column("initiated_by_principal_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("executed_as_principal_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("delegation_scope_digest", sa.String(64), nullable=True))
        batch.add_column(sa.Column("execution_attempt", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_project_run_team_agent",
            "product_team_project_agents",
            ["team_agent_id"],
            ["agent_id"],
        )
        batch.create_foreign_key(
            "fk_project_run_team_task",
            "product_team_tasks",
            ["team_task_id"],
            ["task_id"],
        )
        # Cross-metadata process/graph/decision/reservation references and all
        # project/team ownership relationships remain dispatcher validations.
        batch.create_check_constraint(
            "ck_product_project_agent_runs_kind",
            "run_kind IN ('conversation','planning','task_execution','verification',"
            "'replanning','exchange_draft','specialist')",
        )
        batch.create_check_constraint(
            "ck_product_project_agent_runs_legacy_identity",
            "run_kind = 'task_execution' OR (created_by IS NOT NULL AND mode IS NOT NULL)",
        )
        # Explicit IS NOT NULL terms prevent SQL's three-valued CHECK semantics
        # from accepting an incomplete service identity. length/|| work on both
        # supported dialects; automatic runs never impersonate an account.
        batch.create_check_constraint(
            "ck_product_project_agent_runs_task_execution",
            "run_kind <> 'task_execution' OR ("
            "process_id IS NOT NULL AND team_agent_id IS NOT NULL AND "
            "work_node_id IS NOT NULL AND team_task_id IS NOT NULL AND "
            "orchestration_decision_id IS NOT NULL AND "
            "initiated_by_principal_id IS NOT NULL AND "
            "executed_as_principal_id IS NOT NULL AND "
            "delegation_scope_digest IS NOT NULL AND "
            "execution_attempt IS NOT NULL AND "
            "capacity_reservation_id IS NOT NULL AND "
            "project_budget_reservation_id IS NOT NULL AND "
            "created_by IS NULL AND mode IS NULL AND "
            "execution_attempt >= 1 AND length(delegation_scope_digest) = 64 AND "
            "initiated_by_principal_id = 'service:project-orchestrator' AND "
            "executed_as_principal_id = 'team-agent:' || team_id)",
        )
        batch.create_unique_constraint(
            "uq_product_project_agent_run_task_attempt",
            ["process_id", "team_task_id", "execution_attempt"],
        )
    op.execute(
        "UPDATE product_project_agent_runs SET run_kind = 'conversation', "
        "initiated_by_principal_id = created_by, executed_as_principal_id = created_by"
    )


def downgrade() -> None:
    # Do this before the first DDL statement: there is no lossless way to turn a
    # service-owned run into the old non-null account/mode representation.
    incompatible = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT run_id FROM product_project_agent_runs "
                "WHERE created_by IS NULL OR mode IS NULL LIMIT 1"
            )
        )
        .first()
    )
    if incompatible is not None:
        raise RuntimeError(
            "Cannot downgrade 20260830_52: project Agent runs with NULL created_by or mode "
            "cannot be represented by the legacy schema; preserve the autonomous run data."
        )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint("uq_product_project_agent_run_task_attempt", type_="unique")
        batch.drop_constraint("ck_product_project_agent_runs_task_execution", type_="check")
        batch.drop_constraint("ck_product_project_agent_runs_legacy_identity", type_="check")
        batch.drop_constraint("ck_product_project_agent_runs_kind", type_="check")
        batch.drop_constraint("fk_project_run_team_task", type_="foreignkey")
        batch.drop_constraint("fk_project_run_team_agent", type_="foreignkey")
        for name in (
            "execution_attempt",
            "delegation_scope_digest",
            "executed_as_principal_id",
            "initiated_by_principal_id",
            "run_kind",
            *_BINDING_IDS,
        ):
            batch.drop_column(name)
        batch.alter_column("mode", existing_type=sa.String(32), nullable=False)
        batch.alter_column("created_by", existing_type=sa.String(128), nullable=False)
