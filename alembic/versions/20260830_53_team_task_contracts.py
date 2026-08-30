"""Persist structured execution contracts on TeamTasks.

Revision ID: 20260830_53
Revises: 20260830_52
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_53"
down_revision: str | None = "20260830_52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "product_team_tasks"
_CONTRACT_COLUMNS = (
    "process_id",
    "work_node_id",
    "requested_capability",
    "input_manifest_json",
    "output_contract_json",
    "verification_policy_json",
    "source_decision_id",
    "source_contract_version",
    "autonomy_requirement",
    "accepted_contract_version",
)
_CONTRACT_CHECK = (
    "(source_contract_version IS NULL AND "
    "process_id IS NULL AND work_node_id IS NULL AND "
    "requested_capability IS NULL AND input_manifest_json IS NULL AND "
    "output_contract_json IS NULL AND verification_policy_json IS NULL AND "
    "source_decision_id IS NULL AND autonomy_requirement IS NULL AND "
    "accepted_contract_version IS NULL) OR "
    "(source_contract_version IS NOT NULL AND source_contract_version >= 1 AND "
    "process_id IS NOT NULL AND work_node_id IS NOT NULL AND "
    "requested_capability IS NOT NULL AND input_manifest_json IS NOT NULL AND "
    "output_contract_json IS NOT NULL AND verification_policy_json IS NOT NULL AND "
    "autonomy_requirement IS NOT NULL AND "
    "(accepted_contract_version IS NULL OR "
    "(accepted_contract_version = source_contract_version AND "
    "accepted_contract_version >= 1)))"
)


def upgrade() -> None:
    # Every field is nullable to leave pre-contract TeamTasks untouched.  The
    # service validates process/node ownership because those tables belong to
    # separate metadata domains and must not become cross-metadata FKs here.
    columns = [
        sa.Column("process_id", sa.String(128), nullable=True),
        sa.Column("work_node_id", sa.String(128), nullable=True),
        sa.Column("requested_capability", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("input_manifest_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("output_contract_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("verification_policy_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("source_decision_id", sa.String(128), nullable=True),
        sa.Column("source_contract_version", sa.Integer(), nullable=True),
        sa.Column("autonomy_requirement", sa.String(32), nullable=True),
        sa.Column("accepted_contract_version", sa.Integer(), nullable=True),
    ]
    if op.get_bind().dialect.name == "sqlite":
        # Native ADD preserves incoming FKs, triggers and dependent tables. The
        # last column owns the row CHECK; all referenced columns now exist.
        # Do not batch-copy the parent (or temporarily rewrite its children).
        columns[-1] = sa.Column(
            "accepted_contract_version", sa.Integer(),
            sa.CheckConstraint(_CONTRACT_CHECK, name="ck_product_team_tasks_contract"),
            nullable=True,
        )
        for column in columns:
            op.add_column(_TABLE, column)
        return
    with op.batch_alter_table(_TABLE) as batch:
        for column in columns:
            batch.add_column(column)
        batch.create_check_constraint("ck_product_team_tasks_contract", _CONTRACT_CHECK)


def downgrade() -> None:
    # Contract values cannot be losslessly represented by revision 52. Check
    # before any DDL so a failed downgrade leaves both schema and data intact.
    present = " OR ".join(f"{column} IS NOT NULL" for column in _CONTRACT_COLUMNS)
    incompatible = op.get_bind().execute(
        sa.text(f"SELECT task_id FROM {_TABLE} WHERE {present} LIMIT 1")
    ).first()
    if incompatible is not None:
        raise RuntimeError(
            "Cannot downgrade 20260830_53: structured TeamTask contract data "
            "cannot be represented by the legacy schema; preserve the contract fields."
        )

    if op.get_bind().dialect.name == "sqlite":
        version = op.get_bind().execute(sa.text("SELECT sqlite_version()")).scalar_one()
        if tuple(int(part) for part in version.split(".")) < (3, 35, 0):
            raise RuntimeError("SQLite 3.35+ is required for lossless contract column downgrade")
        # Drop the column owning the CHECK first. No table rebuild or FK toggle.
        for column in reversed(_CONTRACT_COLUMNS):
            op.drop_column(_TABLE, column)
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint("ck_product_team_tasks_contract", type_="check")
        for column in reversed(_CONTRACT_COLUMNS):
            batch.drop_column(column)
