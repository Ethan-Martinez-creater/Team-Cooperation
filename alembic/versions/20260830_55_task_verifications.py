"""Persist immutable task verification evidence.

Revision ID: 20260830_55
Revises: 20260830_54
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_55"
down_revision: str | None = "20260830_54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "task_verifications"
_COMPLETION_CHECK = (
    "(status = 'PENDING' AND completed_at IS NULL) OR "
    "(status IN ('PASS','FAIL','STALE') AND completed_at IS NOT NULL)"
)


def upgrade() -> None:
    # Keep this migration literal and standalone.  The runtime repository has
    # independent metadata, so importing it here would couple Alembic's
    # migration graph to application imports and cross-metadata FKs.
    op.create_table(
        _TABLE,
        sa.Column("verification_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("source_run_id", sa.String(128), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("subject_digest", sa.String(64), nullable=False),
        sa.Column("policy_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("artifacts_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("checks_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("initiated_by", sa.String(256), nullable=False),
        sa.Column("executed_as", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "contract_version >= 1",
            name="ck_task_verifications_contract_version",
        ),
        sa.CheckConstraint(
            "length(subject_digest) = 64",
            name="ck_task_verifications_subject_digest",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','PASS','FAIL','STALE')",
            name="ck_task_verifications_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_task_verifications_version",
        ),
        sa.CheckConstraint(_COMPLETION_CHECK, name="ck_task_verifications_completion"),
        sa.UniqueConstraint(
            "source_run_id",
            "subject_digest",
            name="uq_task_verifications_source_subject",
        ),
    )
    op.create_index(
        "ix_task_verifications_project_task_created",
        _TABLE,
        ["project_id", "task_id", "created_at"],
    )
    op.create_index(
        "ix_task_verifications_status_created",
        _TABLE,
        ["status", "created_at"],
    )


def downgrade() -> None:
    # Verification evidence is not representable by revision 54.  Refuse any
    # non-empty downgrade before issuing DDL so evidence and schema remain
    # untouched when a rollback is attempted accidentally.
    existing = (
        op.get_bind()
        .execute(sa.text(f"SELECT verification_id FROM {_TABLE} LIMIT 1"))
        .first()
    )
    if existing is not None:
        raise RuntimeError(
            "Cannot downgrade 20260830_55: task verification evidence exists; "
            "preserve the verification rows."
        )

    op.drop_index("ix_task_verifications_status_created", table_name=_TABLE)
    op.drop_index("ix_task_verifications_project_task_created", table_name=_TABLE)
    op.drop_table(_TABLE)
