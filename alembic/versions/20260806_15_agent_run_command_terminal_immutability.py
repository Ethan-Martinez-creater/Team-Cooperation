"""Make terminal Agent control command lifecycle fields immutable.

Revision ID: 20260806_15
Revises: 20260806_14
Create Date: 2026-08-06
"""

from typing import Sequence

from alembic import op

revision: str = "20260806_15"
down_revision: str | None = "20260806_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_IDENTITY_GUARD = """
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
"""


def upgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION coifesp_validate_agent_run_command_update_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            {_IDENTITY_GUARD}
            IF OLD.status <> 'pending'
               AND (
                   OLD.status IS DISTINCT FROM NEW.status
                   OR OLD.applied_at IS DISTINCT FROM NEW.applied_at
                   OR OLD.applied_run_version IS DISTINCT FROM NEW.applied_run_version
                   OR OLD.rejected_at IS DISTINCT FROM NEW.rejected_at
                   OR OLD.rejection_code IS DISTINCT FROM NEW.rejection_code
               ) THEN
                RAISE EXCEPTION 'terminal agent run command lifecycle is immutable'
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


def downgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION coifesp_validate_agent_run_command_update_v1()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            {_IDENTITY_GUARD}
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
