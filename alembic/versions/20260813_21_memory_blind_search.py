"""Add tenant-keyed blind search index for encrypted memory.

Revision ID: 20260813_21
Revises: 20260813_20
Create Date: 2026-08-13
"""

from typing import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "20260813_21"
down_revision: str | None = "20260813_20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_search_terms",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("memory_id", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(128), nullable=False),
        sa.Column("term_digest", sa.String(64), nullable=False),
        sa.CheckConstraint("length(term_digest)=64", name="ck_memory_search_terms_term_digest"),
        sa.ForeignKeyConstraint(["tenant_id", "memory_id"],
            ["memory_records.tenant_id", "memory_records.memory_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "memory_id", "key_id", "term_digest"),
    )
    op.create_index("ix_memory_search_tenant_key_term", "memory_search_terms",
                    ["tenant_id", "key_id", "term_digest"])
    op.execute("ALTER TABLE memory_search_terms ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_search_terms FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY memory_search_terms_tenant_isolation ON memory_search_terms FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")


def downgrade() -> None:
    op.drop_index("ix_memory_search_tenant_key_term", table_name="memory_search_terms")
    op.drop_table("memory_search_terms")
