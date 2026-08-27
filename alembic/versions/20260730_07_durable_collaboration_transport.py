"""Add leased collaboration outbox delivery and replay-safe inbox.

Revision ID: 20260730_07
Revises: 20260730_06
Create Date: 2026-07-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_07"
down_revision: str | None = "20260730_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.drop_index(
        "ix_governance_outbox_recipient_status_available",
        table_name="governance_outbox",
    )
    op.add_column(
        "governance_outbox",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )
    op.add_column(
        "governance_outbox",
        sa.Column("lease_token", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "governance_outbox",
        sa.Column("envelope", _jsonb(), nullable=True),
    )
    op.add_column(
        "governance_outbox",
        sa.Column("envelope_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "governance_outbox",
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
    )
    op.alter_column("governance_outbox", "max_attempts", server_default=None)
    op.drop_constraint(
        "ck_governance_outbox_attempt_count",
        "governance_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_governance_outbox_claimed_lease",
        "governance_outbox",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_governance_outbox_attempt_count"),
        "governance_outbox",
        "max_attempts BETWEEN 1 AND 100 AND " "attempt_count BETWEEN 0 AND max_attempts",
    )
    op.create_check_constraint(
        op.f("ck_governance_outbox_claimed_lease"),
        "governance_outbox",
        "(status = 'claimed' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status <> 'claimed' AND lease_owner IS NULL "
        "AND lease_token IS NULL AND lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_governance_outbox_published_envelope"),
        "governance_outbox",
        "(status = 'published' AND published_at IS NOT NULL "
        "AND envelope IS NOT NULL AND envelope_digest IS NOT NULL) "
        "OR (status <> 'published' AND published_at IS NULL)",
    )
    op.create_index(
        "ix_governance_outbox_producer_status_available",
        "governance_outbox",
        ["producer_tenant_id", "status", "available_at"],
        unique=False,
    )

    op.execute("DROP POLICY governance_outbox_recipient_select ON governance_outbox")
    op.execute("DROP POLICY governance_outbox_recipient_update ON governance_outbox")
    op.execute("""
        CREATE POLICY governance_outbox_producer_select
        ON governance_outbox FOR SELECT
        USING (
            producer_tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY governance_outbox_producer_update
        ON governance_outbox FOR UPDATE
        USING (
            producer_tenant_id = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            producer_tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)

    op.create_table(
        "collaboration_inbox",
        sa.Column("recipient_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("sender_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("envelope", _jsonb(), nullable=False),
        sa.Column("envelope_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handler_key", sa.String(length=128), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(envelope_digest) = 64",
            name=op.f("ck_collaboration_inbox_envelope_digest"),
        ),
        sa.CheckConstraint(
            "result_digest IS NULL OR length(result_digest) = 64",
            name=op.f("ck_collaboration_inbox_result_digest"),
        ),
        sa.CheckConstraint(
            "status IN ('received', 'claimed', 'processed', 'rejected')",
            name=op.f("ck_collaboration_inbox_status"),
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 100 AND " "attempt_count BETWEEN 0 AND max_attempts",
            name=op.f("ck_collaboration_inbox_attempt_count"),
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'claimed' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_collaboration_inbox_claimed_lease"),
        ),
        sa.CheckConstraint(
            "(status IN ('processed','rejected')) = (completed_at IS NOT NULL)",
            name=op.f("ck_collaboration_inbox_completion"),
        ),
        sa.PrimaryKeyConstraint(
            "recipient_tenant_id",
            "message_id",
            name=op.f("pk_collaboration_inbox"),
        ),
    )
    op.create_index(
        "ix_collaboration_inbox_recipient_status_available",
        "collaboration_inbox",
        ["recipient_tenant_id", "status", "available_at"],
        unique=False,
    )
    op.execute("ALTER TABLE collaboration_inbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE collaboration_inbox FORCE ROW LEVEL SECURITY")
    for command in ("SELECT", "INSERT", "UPDATE"):
        lower = command.lower()
        if command == "SELECT":
            clause = """
                USING (
                    recipient_tenant_id =
                    current_setting('coifesp.tenant_id', true)
                )
                """
        elif command == "INSERT":
            clause = """
                WITH CHECK (
                    recipient_tenant_id =
                    current_setting('coifesp.tenant_id', true)
                )
                """
        else:
            clause = """
                USING (
                    recipient_tenant_id =
                    current_setting('coifesp.tenant_id', true)
                )
                WITH CHECK (
                    recipient_tenant_id =
                    current_setting('coifesp.tenant_id', true)
                )
                """
        op.execute(f"""
            CREATE POLICY collaboration_inbox_tenant_{lower}
            ON collaboration_inbox FOR {command}
            {clause}
            """)


def downgrade() -> None:
    op.drop_index(
        "ix_collaboration_inbox_recipient_status_available",
        table_name="collaboration_inbox",
    )
    op.drop_table("collaboration_inbox")
    op.execute("DROP POLICY governance_outbox_producer_update ON governance_outbox")
    op.execute("DROP POLICY governance_outbox_producer_select ON governance_outbox")
    op.execute("""
        CREATE POLICY governance_outbox_recipient_select
        ON governance_outbox FOR SELECT
        USING (
            recipient_tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.execute("""
        CREATE POLICY governance_outbox_recipient_update
        ON governance_outbox FOR UPDATE
        USING (
            recipient_tenant_id = current_setting('coifesp.tenant_id', true)
        )
        WITH CHECK (
            recipient_tenant_id = current_setting('coifesp.tenant_id', true)
        )
        """)
    op.drop_index(
        "ix_governance_outbox_producer_status_available",
        table_name="governance_outbox",
    )
    op.drop_constraint(
        "ck_governance_outbox_published_envelope",
        "governance_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_governance_outbox_claimed_lease",
        "governance_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_governance_outbox_attempt_count",
        "governance_outbox",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_governance_outbox_attempt_count"),
        "governance_outbox",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_governance_outbox_claimed_lease"),
        "governance_outbox",
        "(status <> 'claimed') OR " "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
    )
    op.drop_column("governance_outbox", "last_error_code")
    op.drop_column("governance_outbox", "envelope_digest")
    op.drop_column("governance_outbox", "envelope")
    op.drop_column("governance_outbox", "lease_token")
    op.drop_column("governance_outbox", "max_attempts")
    op.create_index(
        "ix_governance_outbox_recipient_status_available",
        "governance_outbox",
        ["recipient_tenant_id", "status", "available_at"],
        unique=False,
    )
