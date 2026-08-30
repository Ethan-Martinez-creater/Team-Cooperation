"""Persist task-scoped human review decisions.

Revision ID: 20260831_57
Revises: 20260830_56
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_57"
down_revision: str | None = "20260830_56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "task_human_reviews"
_COMPLETION_CHECK = (
    "(status = 'OPEN' AND completed_at IS NULL) OR "
    "(status IN ('ACCEPTED','REJECTED','STALE') AND completed_at IS NOT NULL)"
)
_DECISION_FIELDS_CHECK = (
    "(decision IS NULL AND decision_key IS NULL AND decision_digest IS NULL "
    "AND decided_by IS NULL AND reason IS NULL AND decided_at IS NULL) OR "
    "(decision IS NOT NULL AND decision_key IS NOT NULL AND decision_digest IS NOT NULL "
    "AND decided_by IS NOT NULL AND reason IS NOT NULL AND decided_at IS NOT NULL)"
)


def upgrade() -> None:
    # Keep this migration literal and standalone.  Human-review ownership is
    # enforced by the verification service, without cross-metadata FKs.
    op.create_table(
        _TABLE,
        sa.Column("review_id", sa.String(128), primary_key=True),
        sa.Column("verification_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("source_run_id", sa.String(128), nullable=False),
        sa.Column("reviewer_team_id", sa.String(128), nullable=False),
        sa.Column("criterion_id", sa.Text(), nullable=False),
        sa.Column("criterion_key", sa.String(64), nullable=False),
        sa.Column("subject_digest", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_key", sa.String(128), nullable=True),
        sa.Column("decision_digest", sa.String(64), nullable=True),
        sa.Column("decided_by", sa.String(256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(criterion_id) > 0",
            name="ck_task_human_reviews_criterion_id",
        ),
        sa.CheckConstraint(
            "length(criterion_key) = 64",
            name="ck_task_human_reviews_criterion_key",
        ),
        sa.CheckConstraint(
            "length(subject_digest) = 64",
            name="ck_task_human_reviews_subject_digest",
        ),
        sa.CheckConstraint(
            "contract_version >= 1",
            name="ck_task_human_reviews_contract_version",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','ACCEPTED','REJECTED','STALE')",
            name="ck_task_human_reviews_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_task_human_reviews_version",
        ),
        sa.CheckConstraint(
            _COMPLETION_CHECK,
            name="ck_task_human_reviews_completion",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('ACCEPT','REJECT')",
            name="ck_task_human_reviews_decision",
        ),
        sa.CheckConstraint(
            _DECISION_FIELDS_CHECK,
            name="ck_task_human_reviews_decision_fields",
        ),
        sa.CheckConstraint(
            "decision_digest IS NULL OR length(decision_digest) = 64",
            name="ck_task_human_reviews_decision_digest",
        ),
        sa.CheckConstraint(
            "(status <> 'OPEN') OR "
            "(decision IS NULL AND decision_key IS NULL AND decision_digest IS NULL "
            "AND decided_by IS NULL AND reason IS NULL AND decided_at IS NULL)",
            name="ck_task_human_reviews_open_decision",
        ),
        sa.CheckConstraint(
            "(status <> 'ACCEPTED') OR "
            "(decision = 'ACCEPT' AND decision_key IS NOT NULL AND decision_digest IS NOT NULL "
            "AND decided_by IS NOT NULL AND reason IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_task_human_reviews_accepted_decision",
        ),
        sa.CheckConstraint(
            "(status <> 'REJECTED') OR "
            "(decision = 'REJECT' AND decision_key IS NOT NULL AND decision_digest IS NOT NULL "
            "AND decided_by IS NOT NULL AND reason IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_task_human_reviews_rejected_decision",
        ),
        sa.UniqueConstraint(
            "verification_id",
            "criterion_key",
            name="uq_task_human_reviews_verification_criterion",
        ),
    )
    op.create_index(
        "ix_task_human_reviews_reviewer_status_created",
        _TABLE,
        ["reviewer_team_id", "status", "created_at"],
    )
    op.create_index(
        "ix_task_human_reviews_verification_id",
        _TABLE,
        ["verification_id"],
    )


def downgrade() -> None:
    # Human-review decisions are evidence and cannot be represented by
    # revision 56.  Refuse before issuing DDL when any row exists.
    existing = (
        op.get_bind()
        .execute(sa.text(f"SELECT review_id FROM {_TABLE} LIMIT 1"))
        .first()
    )
    if existing is not None:
        raise RuntimeError(
            "Cannot downgrade 20260831_57: human review evidence exists; "
            "preserve the review rows."
        )

    op.drop_index(
        "ix_task_human_reviews_verification_id",
        table_name=_TABLE,
    )
    op.drop_index(
        "ix_task_human_reviews_reviewer_status_created",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
