"""Make Memory idempotency claims atomic with record writes.

Revision ID: 20260729_02
Revises: 20260729_01
Create Date: 2026-07-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260729_02"
down_revision: str | None = "20260729_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_idempotency_claims",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(namespace) > 0",
            name=op.f("ck_memory_idempotency_claims_namespace"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name=op.f("ck_memory_idempotency_claims_idempotency_key"),
        ),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name=op.f("ck_memory_idempotency_claims_request_digest"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memory_records.tenant_id", "memory_records.memory_id"],
            name="fk_memory_claim_record",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "namespace",
            "idempotency_key",
            name=op.f("pk_memory_idempotency_claims"),
        ),
    )
    op.create_index(
        "ix_memory_claim_tenant_record",
        "memory_idempotency_claims",
        ["tenant_id", "memory_id"],
        unique=False,
    )
    op.execute("ALTER TABLE memory_idempotency_claims ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_idempotency_claims FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY memory_claims_tenant_isolation
        ON memory_idempotency_claims
        FOR ALL
        USING (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS memory_claims_tenant_isolation " "ON memory_idempotency_claims"
    )
    op.drop_index(
        "ix_memory_claim_tenant_record",
        table_name="memory_idempotency_claims",
    )
    op.drop_table("memory_idempotency_claims")
