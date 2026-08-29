"""Add durable orchestrator wakeups with lease and fencing state.

Revision ID: 20260829_48
Revises: 20260829_47
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260829_48"
down_revision: str | None = "20260829_47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_process_wakeups",
        sa.Column("wakeup_id", sa.String(160), primary_key=True),
        sa.Column(
            "process_id",
            sa.String(128),
            sa.ForeignKey("project_processes.process_id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("source_event_type", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("retry_budget", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "process_id",
            "source_event_id",
            name="uq_project_process_wakeup_source",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','LEASED','RETRY_WAIT','COMPLETED','FAILED')",
            name="project_wakeup_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="project_wakeup_attempt"),
        sa.CheckConstraint("retry_budget >= 0", name="project_wakeup_retry_budget"),
        sa.CheckConstraint("fencing_token >= 0", name="project_wakeup_fencing_token"),
        sa.CheckConstraint(
            "(status = 'LEASED' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'LEASED' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="project_wakeup_lease_state",
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED','FAILED') AND terminal_at IS NOT NULL) OR "
            "(status NOT IN ('COMPLETED','FAILED') AND terminal_at IS NULL)",
            name="project_wakeup_terminal_time",
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="project_wakeup_payload_digest",
        ),
    )
    op.create_index(
        "ix_project_wakeup_claim",
        "project_process_wakeups",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_project_wakeup_process_status",
        "project_process_wakeups",
        ["process_id", "status"],
    )
    op.create_index(
        "ix_project_wakeup_lease_expiry",
        "project_process_wakeups",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_wakeup_lease_expiry", table_name="project_process_wakeups")
    op.drop_index("ix_project_wakeup_process_status", table_name="project_process_wakeups")
    op.drop_index("ix_project_wakeup_claim", table_name="project_process_wakeups")
    op.drop_table("project_process_wakeups")
