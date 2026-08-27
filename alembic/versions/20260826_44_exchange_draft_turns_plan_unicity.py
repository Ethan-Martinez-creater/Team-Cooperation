"""Exchange-draft turns and unique plan import source runs.

Revision ID: 20260826_44
Revises: 20260826_43
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_44"
down_revision: str | None = "20260826_43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("trigger_kind", "product_project_agent_turns", type_="check")
    op.create_check_constraint(
        "trigger_kind",
        "product_project_agent_turns",
        "trigger_kind IN ('user_message','exchange','planning','exchange_draft')",
    )
    # One plan draft per source run: projection retries after a crash must not
    # create duplicates even when two callbacks race. Multiple NULLs are
    # allowed so manually imported drafts stay unaffected.
    op.create_unique_constraint(
        "uq_plan_drafts_source_run",
        "product_project_plan_drafts",
        ["source_run_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_plan_drafts_source_run", "product_project_plan_drafts", type_="unique")
    op.drop_constraint("trigger_kind", "product_project_agent_turns", type_="check")
    op.create_check_constraint(
        "trigger_kind",
        "product_project_agent_turns",
        "trigger_kind IN ('user_message','exchange','planning')",
    )