"""Create the encrypted, tenant-isolated Memory store.

Revision ID: 20260729_01
Revises:
Create Date: 2026-07-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260729_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_records",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("classification", sa.Integer(), nullable=False),
        sa.Column(
            "compartments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("resource_id", sa.String(length=256), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=256), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("trust_level", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=128), nullable=True),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('session', 'user_private', 'team_project', 'organization')",
            name=op.f("ck_memory_records_scope"),
        ),
        sa.CheckConstraint(
            "kind IN ('fact', 'decision', 'procedure', 'task_summary')",
            name=op.f("ck_memory_records_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'quarantined', 'revoked')",
            name=op.f("ck_memory_records_status"),
        ),
        sa.CheckConstraint(
            "classification BETWEEN 0 AND 3",
            name=op.f("ck_memory_records_classification"),
        ),
        sa.CheckConstraint(
            "source_type IN ('user', 'agent', 'tool', 'document', 'a2a', 'system')",
            name=op.f("ck_memory_records_source_type"),
        ),
        sa.CheckConstraint(
            "trust_level BETWEEN 0 AND 3",
            name=op.f("ck_memory_records_trust_level"),
        ),
        sa.CheckConstraint(
            "length(nonce) = 12",
            name=op.f("ck_memory_records_nonce_length"),
        ),
        sa.CheckConstraint(
            "length(content_fingerprint) = 64",
            name=op.f("ck_memory_records_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_memory_records_positive_version"),
        ),
        sa.CheckConstraint(
            "(scope <> 'user_private' OR owner_principal_id IS NOT NULL) "
            "AND (scope <> 'session' OR session_id IS NOT NULL) "
            "AND (scope <> 'team_project' OR project_id IS NOT NULL)",
            name=op.f("ck_memory_records_scope_owner"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(compartments) = 'array'",
            name=op.f("ck_memory_records_compartments_array"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "memory_id",
            name="pk_memory_records",
        ),
    )
    op.create_index(
        "ix_memory_tenant_status_scope_created",
        "memory_records",
        ["tenant_id", "status", "scope", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_tenant_project",
        "memory_records",
        ["tenant_id", "project_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_tenant_owner",
        "memory_records",
        ["tenant_id", "owner_principal_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_tenant_session",
        "memory_records",
        ["tenant_id", "session_id"],
        unique=False,
    )

    op.execute("ALTER TABLE memory_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_records FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY memory_records_tenant_isolation
        ON memory_records
        FOR ALL
        USING (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS memory_records_tenant_isolation " "ON memory_records")
    op.drop_index("ix_memory_tenant_session", table_name="memory_records")
    op.drop_index("ix_memory_tenant_owner", table_name="memory_records")
    op.drop_index("ix_memory_tenant_project", table_name="memory_records")
    op.drop_index(
        "ix_memory_tenant_status_scope_created",
        table_name="memory_records",
    )
    op.drop_table("memory_records")
