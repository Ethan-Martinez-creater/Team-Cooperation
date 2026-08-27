"""Add durable Agent waiting-for-tool state.

Revision ID: 20260812_18
Revises: 20260812_17
Create Date: 2026-08-12
"""

from typing import Sequence

from alembic import op

revision: str = "20260812_18"
down_revision: str | None = "20260812_17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD = (
    "status IN ('queued','leased','running','awaiting_approval',"
    "'completed','failed','cancelled')"
)
NEW = (
    "status IN ('queued','leased','running','awaiting_approval','awaiting_tool',"
    "'completed','failed','cancelled')"
)


def upgrade() -> None:
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_status", "agent_runs", NEW)


def downgrade() -> None:
    # Downgrade is intentionally fail-closed if awaiting jobs remain.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM agent_runs WHERE status='awaiting_tool') THEN
            RAISE EXCEPTION 'cannot downgrade while Agent runs await tools';
          END IF;
        END $$
    """)
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_status", "agent_runs", OLD)
