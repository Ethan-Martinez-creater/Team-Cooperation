"""Add encrypted, durable Agent steering and follow-up commands.

Revision ID: 20260806_14
Revises: 20260730_13
Create Date: 2026-08-06
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260806_14"
down_revision: str | None = "20260730_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_commands",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("content_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("content_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("content_key_id", sa.String(length=128), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_run_version", sa.Integer(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "sequence > 0",
            name=op.f("ck_agent_run_commands_sequence"),
        ),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name=op.f("ck_agent_run_commands_request_digest"),
        ),
        sa.CheckConstraint(
            "length(content_fingerprint) = 64",
            name=op.f("ck_agent_run_commands_content_fingerprint"),
        ),
        sa.CheckConstraint(
            "command_type IN ('steer','follow_up')",
            name=op.f("ck_agent_run_commands_type"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','applied','rejected')",
            name=op.f("ck_agent_run_commands_status"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND applied_at IS NULL "
            "AND applied_run_version IS NULL AND rejected_at IS NULL "
            "AND rejection_code IS NULL) OR "
            "(status = 'applied' AND applied_at IS NOT NULL "
            "AND applied_run_version IS NOT NULL AND rejected_at IS NULL "
            "AND rejection_code IS NULL) OR "
            "(status = 'rejected' AND applied_at IS NULL "
            "AND applied_run_version IS NULL AND rejected_at IS NOT NULL "
            "AND rejection_code IS NOT NULL)",
            name=op.f("ck_agent_run_commands_lifecycle_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.run_id"],
            name="fk_agent_run_command_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "run_id",
            "sequence",
            name=op.f("pk_agent_run_commands"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "command_id",
            name="uq_agent_run_command_id",
        ),
    )
    op.create_index(
        "ix_agent_run_commands_pending",
        "agent_run_commands",
        ["tenant_id", "run_id", "status", "sequence"],
        unique=False,
    )
    op.execute("ALTER TABLE agent_run_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_run_commands FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_run_commands_tenant_select
        ON agent_run_commands FOR SELECT
        USING (tenant_id = current_setting('coifesp.tenant_id', true))
        """)
    op.execute("""
        CREATE POLICY agent_run_commands_tenant_insert
        ON agent_run_commands FOR INSERT
        WITH CHECK (tenant_id = current_setting('coifesp.tenant_id', true))
        """)
    op.execute("""
        CREATE POLICY agent_run_commands_tenant_update
        ON agent_run_commands FOR UPDATE
        USING (tenant_id = current_setting('coifesp.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('coifesp.tenant_id', true))
        """)
    op.execute("""
        CREATE FUNCTION coifesp_validate_agent_run_command_update_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               OR OLD.run_id IS DISTINCT FROM NEW.run_id
               OR OLD.sequence IS DISTINCT FROM NEW.sequence
               OR OLD.command_id IS DISTINCT FROM NEW.command_id
               OR OLD.command_type IS DISTINCT FROM NEW.command_type
               OR OLD.request_digest IS DISTINCT FROM NEW.request_digest
               OR OLD.content_ciphertext IS DISTINCT FROM NEW.content_ciphertext
               OR OLD.content_nonce IS DISTINCT FROM NEW.content_nonce
               OR OLD.content_fingerprint IS DISTINCT FROM NEW.content_fingerprint
               OR OLD.content_key_id IS DISTINCT FROM NEW.content_key_id
               OR OLD.submitted_by IS DISTINCT FROM NEW.submitted_by
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'agent run command identity and content are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status IS DISTINCT FROM NEW.status
               AND NOT (
                   OLD.status = 'pending'
                   AND NEW.status IN ('applied', 'rejected')
               ) THEN
                RAISE EXCEPTION 'agent run command lifecycle cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """)
    op.execute("""
        CREATE FUNCTION coifesp_reject_agent_run_command_removal_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'agent run commands cannot be removed'
                USING ERRCODE = '55000';
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER trg_agent_run_commands_validate_update
        BEFORE UPDATE ON agent_run_commands
        FOR EACH ROW
        EXECUTE FUNCTION coifesp_validate_agent_run_command_update_v1()
        """)
    op.execute("""
        CREATE TRIGGER trg_agent_run_commands_reject_delete
        BEFORE DELETE ON agent_run_commands
        FOR EACH ROW
        EXECUTE FUNCTION coifesp_reject_agent_run_command_removal_v1()
        """)
    op.execute("""
        CREATE TRIGGER trg_agent_run_commands_reject_truncate
        BEFORE TRUNCATE ON agent_run_commands
        FOR EACH STATEMENT
        EXECUTE FUNCTION coifesp_reject_agent_run_command_removal_v1()
        """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_run_commands_reject_truncate "
        "ON agent_run_commands"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_run_commands_reject_delete "
        "ON agent_run_commands"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_run_commands_validate_update "
        "ON agent_run_commands"
    )
    op.execute("DROP FUNCTION IF EXISTS coifesp_reject_agent_run_command_removal_v1()")
    op.execute("DROP FUNCTION IF EXISTS coifesp_validate_agent_run_command_update_v1()")
    op.drop_index("ix_agent_run_commands_pending", table_name="agent_run_commands")
    op.drop_table("agent_run_commands")
