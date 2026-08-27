"""Bind approval decisions to classification and compartment labels.

Revision ID: 20260730_09
Revises: 20260730_08
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_09"
down_revision: str | None = "20260730_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column(
            "classification",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "approval_requests",
        sa.Column(
            "compartments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("approval_requests", "classification", server_default=None)
    op.alter_column("approval_requests", "compartments", server_default=None)
    op.create_check_constraint(
        op.f("ck_approval_requests_classification"),
        "approval_requests",
        "classification BETWEEN 0 AND 3",
    )
    op.create_check_constraint(
        op.f("ck_approval_requests_compartments_array"),
        "approval_requests",
        "jsonb_typeof(compartments) = 'array'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_approval_requests_compartments_array"),
        "approval_requests",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_approval_requests_classification"),
        "approval_requests",
        type_="check",
    )
    op.drop_column("approval_requests", "compartments")
    op.drop_column("approval_requests", "classification")
