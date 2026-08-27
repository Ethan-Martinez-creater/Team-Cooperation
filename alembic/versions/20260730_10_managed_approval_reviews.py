"""Add tool-managed, human-readable approval review projections.

Revision ID: 20260730_10
Revises: 20260730_09
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_10"
down_revision: str | None = "20260730_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column(
            "origin",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
    )
    op.add_column(
        "approval_requests",
        sa.Column("review_projection", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("projection_digest", sa.String(length=64), nullable=True),
    )
    op.alter_column("approval_requests", "origin", server_default=None)
    op.create_check_constraint(
        op.f("ck_approval_requests_approval_origin"),
        "approval_requests",
        "origin IN ('manual','tool_managed')",
    )
    op.create_check_constraint(
        op.f("ck_approval_requests_approval_projection"),
        "approval_requests",
        "(origin = 'tool_managed') = "
        "(review_projection IS NOT NULL AND projection_digest IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_approval_requests_approval_projection"),
        "approval_requests",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_approval_requests_approval_origin"),
        "approval_requests",
        type_="check",
    )
    op.drop_column("approval_requests", "projection_digest")
    op.drop_column("approval_requests", "review_projection")
    op.drop_column("approval_requests", "origin")
