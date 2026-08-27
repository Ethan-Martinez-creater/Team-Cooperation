"""Enforce managed approval projection shape and digest constraints.

Revision ID: 20260730_11
Revises: 20260730_10
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op

revision: str = "20260730_11"
down_revision: str | None = "20260730_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_approval_requests_approval_projection_digest"),
        "approval_requests",
        "projection_digest IS NULL OR length(projection_digest) = 64",
    )
    op.create_check_constraint(
        op.f("ck_approval_requests_approval_projection_shape"),
        "approval_requests",
        "review_projection IS NULL OR "
        "(jsonb_typeof(review_projection) = 'object' "
        "AND review_projection->>'schema' = 'coifesp.approval-review.v1' "
        "AND review_projection->>'tool_name' = tool_name "
        "AND jsonb_typeof(review_projection->'fields') = 'array')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_approval_requests_approval_projection_shape"),
        "approval_requests",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_approval_requests_approval_projection_digest"),
        "approval_requests",
        type_="check",
    )
