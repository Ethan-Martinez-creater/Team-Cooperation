"""Persist independent agent-review evidence.

Revision ID: 20260830_56
Revises: 20260830_55
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_56"
down_revision: str | None = "20260830_55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "task_agent_reviews"
_COMPLETION_CHECK = (
    "(status = 'QUEUED' AND completed_at IS NULL) OR "
    "(status IN ('PASS','FAIL','UNAVAILABLE','STALE') AND completed_at IS NOT NULL)"
)
_RESULT_CHECK = "status NOT IN ('PASS','FAIL') OR result_json IS NOT NULL"


def upgrade() -> None:
    # Keep this migration literal and standalone.  Agent-review evidence has
    # service-level ownership checks and must not acquire cross-metadata FKs.
    op.create_table(
        _TABLE,
        sa.Column("review_id", sa.String(128), primary_key=True),
        sa.Column("verification_id", sa.String(128), nullable=False),
        sa.Column("source_run_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("owner_team_id", sa.String(128), nullable=False),
        sa.Column("criterion_id", sa.Text(), nullable=False),
        sa.Column("criterion_key", sa.String(64), nullable=False),
        sa.Column("subject_digest", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("budget_reservation_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("initiated_by", sa.String(256), nullable=False),
        sa.Column("executed_as", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(criterion_id) > 0",
            name="ck_task_agent_reviews_criterion_id",
        ),
        sa.CheckConstraint(
            "length(criterion_key) = 64",
            name="ck_task_agent_reviews_criterion_key",
        ),
        sa.CheckConstraint(
            "length(subject_digest) = 64",
            name="ck_task_agent_reviews_subject_digest",
        ),
        sa.CheckConstraint(
            "contract_version >= 1",
            name="ck_task_agent_reviews_contract_version",
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name="ck_task_agent_reviews_attempt",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','PASS','FAIL','UNAVAILABLE','STALE')",
            name="ck_task_agent_reviews_status",
        ),
        sa.CheckConstraint(
            _COMPLETION_CHECK,
            name="ck_task_agent_reviews_completion",
        ),
        sa.CheckConstraint(
            _RESULT_CHECK,
            name="ck_task_agent_reviews_result",
        ),
        sa.UniqueConstraint(
            "run_id",
            name="uq_task_agent_reviews_run_id",
        ),
        sa.UniqueConstraint(
            "budget_reservation_id",
            name="uq_task_agent_reviews_budget_reservation_id",
        ),
        sa.UniqueConstraint(
            "verification_id",
            "criterion_key",
            "attempt",
            name="uq_task_agent_reviews_verification_criterion_attempt",
        ),
    )
    op.create_index(
        "ix_task_agent_reviews_verification_criterion_attempt",
        _TABLE,
        ["verification_id", "criterion_key", "attempt"],
    )
    op.create_index(
        "ix_task_agent_reviews_status_created",
        _TABLE,
        ["status", "created_at"],
    )


def downgrade() -> None:
    # Review rows are evidence and cannot be represented by revision 55.
    # Refuse before any DDL so a rollback cannot silently destroy them.
    existing = (
        op.get_bind()
        .execute(sa.text(f"SELECT review_id FROM {_TABLE} LIMIT 1"))
        .first()
    )
    if existing is not None:
        raise RuntimeError(
            "Cannot downgrade 20260830_56: agent review evidence exists; "
            "preserve the review rows."
        )

    op.drop_index(
        "ix_task_agent_reviews_status_created",
        table_name=_TABLE,
    )
    op.drop_index(
        "ix_task_agent_reviews_verification_criterion_attempt",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
