"""Add server-governed project Agent modes.

Revision ID: 20260813_32
Revises: 20260813_31
Create Date: 2026-08-16
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260813_32"
down_revision: str | None = "20260813_31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product_project_agent_runs",
        sa.Column("mode", sa.String(32), nullable=True))
    op.execute("UPDATE product_project_agent_runs SET mode = 'analysis' WHERE mode IS NULL")
    op.alter_column("product_project_agent_runs", "mode", nullable=False)
    op.create_check_constraint("ck_product_project_agent_runs_mode",
        "product_project_agent_runs",
        "mode IN ('analysis','collaboration_actions','delivery_review')")


def downgrade() -> None:
    op.drop_constraint("ck_product_project_agent_runs_mode",
        "product_project_agent_runs", type_="check")
    op.drop_column("product_project_agent_runs", "mode")
