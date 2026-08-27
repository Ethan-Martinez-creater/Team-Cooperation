"""Create the immutable, tenant-isolated audit chain.

Revision ID: 20260729_03
Revises: 20260729_02
Create Date: 2026-07-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260729_03"
down_revision: str | None = "20260729_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_heads",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name=op.f("ck_audit_heads_sequence"),
        ),
        sa.CheckConstraint(
            "length(last_hash) = 64",
            name=op.f("ck_audit_heads_hash_length"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            name=op.f("pk_audit_heads"),
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=128), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name=op.f("ck_audit_events_positive_sequence"),
        ),
        sa.CheckConstraint(
            "length(previous_hash) = 64",
            name=op.f("ck_audit_events_previous_hash"),
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64",
            name=op.f("ck_audit_events_event_hash"),
        ),
        sa.CheckConstraint(
            "length(signature) = 64",
            name=op.f("ck_audit_events_signature"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["audit_heads.tenant_id"],
            name="fk_audit_event_head",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "sequence",
            name=op.f("pk_audit_events"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name=op.f("uq_audit_events_tenant_event"),
        ),
    )
    op.create_index(
        "ix_audit_tenant_occurred",
        "audit_events",
        ["tenant_id", "occurred_at"],
        unique=False,
    )

    op.execute("ALTER TABLE audit_heads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_heads FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_heads_tenant_isolation
        ON audit_heads
        FOR ALL
        USING (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_events_tenant_select
        ON audit_events
        FOR SELECT
        USING (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY audit_events_tenant_insert
        ON audit_events
        FOR INSERT
        WITH CHECK (
            tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE FUNCTION coifesp_reject_audit_event_mutation_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_audit_events_reject_update_delete
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION coifesp_reject_audit_event_mutation_v1()
        """)
    op.execute("""
        CREATE TRIGGER trg_audit_events_reject_truncate
        BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT
        EXECUTE FUNCTION coifesp_reject_audit_event_mutation_v1()
        """)
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_reject_truncate " "ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_reject_update_delete " "ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_tenant_insert ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_tenant_select ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_heads_tenant_isolation ON audit_heads")
    op.drop_index(
        "ix_audit_tenant_occurred",
        table_name="audit_events",
    )
    op.drop_table("audit_events")
    op.drop_table("audit_heads")
    op.execute("DROP FUNCTION IF EXISTS " "coifesp_reject_audit_event_mutation_v1()")
