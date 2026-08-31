"""Persist immutable human delivery approval evidence.

Revision ID: 20260831_60
Revises: 20260831_59
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_60"
down_revision: str | None = "20260831_59"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "project_delivery_approvals"


def upgrade() -> None:
    # Delivery metadata intentionally has no cross-metadata foreign keys.
    # Services validate ownership of the referenced project, process,
    # delivery, and completion contract records.
    op.create_table(
        _TABLE,
        sa.Column("approval_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decision_key", sa.String(128), nullable=False),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_delivery_version", sa.Integer(), nullable=False),
        sa.Column("expected_process_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(approval_id) > 0",
            name="ck_project_delivery_approvals_approval_id",
        ),
        sa.CheckConstraint(
            "length(project_id) > 0",
            name="ck_project_delivery_approvals_project_id",
        ),
        sa.CheckConstraint(
            "length(process_id) > 0",
            name="ck_project_delivery_approvals_process_id",
        ),
        sa.CheckConstraint(
            "length(delivery_id) > 0",
            name="ck_project_delivery_approvals_delivery_id",
        ),
        sa.CheckConstraint(
            "length(contract_id) > 0",
            name="ck_project_delivery_approvals_contract_id",
        ),
        sa.CheckConstraint(
            "contract_version >= 1",
            name="ck_project_delivery_approvals_contract_version",
        ),
        sa.CheckConstraint(
            "length(actor_id) > 0",
            name="ck_project_delivery_approvals_actor_id",
        ),
        sa.CheckConstraint(
            "decision IN ('ACCEPT','REJECT')",
            name="ck_project_delivery_approvals_decision",
        ),
        sa.CheckConstraint(
            "length(decision_key) > 0",
            name="ck_project_delivery_approvals_decision_key",
        ),
        sa.CheckConstraint(
            "length(decision_digest) = 64",
            name="ck_project_delivery_approvals_decision_digest",
        ),
        sa.CheckConstraint(
            "length(reason) > 0",
            name="ck_project_delivery_approvals_reason",
        ),
        sa.CheckConstraint(
            "expected_delivery_version >= 1",
            name="ck_project_delivery_approvals_expected_delivery_version",
        ),
        sa.CheckConstraint(
            "expected_process_version >= 1",
            name="ck_project_delivery_approvals_expected_process_version",
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "actor_id",
            name="uq_project_delivery_approvals_delivery_actor",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    existing = connection.execute(sa.text(f"SELECT 1 FROM {_TABLE} LIMIT 1")).first()
    if existing is not None:
        raise RuntimeError(
            "Cannot downgrade 20260831_60: delivery approval evidence exists; "
            "preserve the evidence rows."
        )
    op.drop_table(_TABLE)
