"""Add account notification preferences, notifications and deliveries.

Revision ID: 20260822_35
Revises: 20260822_34
Create Date: 2026-08-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260822_35"
down_revision: str | None = "20260822_34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("product_notification_preferences",
        sa.Column("account_id", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), primary_key=True),
        sa.Column("notify_tasks", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_messages", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_resources", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_topics", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_agent_events", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_due_soon", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_overdue", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("due_soon_hours", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("time_zone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("quiet_start_minute", sa.Integer(), nullable=True),
        sa.Column("quiet_end_minute", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("due_soon_hours BETWEEN 1 AND 336"),
        sa.CheckConstraint(
            "(quiet_start_minute IS NULL AND quiet_end_minute IS NULL) OR "
            "(quiet_start_minute IS NOT NULL AND quiet_end_minute IS NOT NULL)"),
        sa.CheckConstraint(
            "quiet_start_minute IS NULL OR "
            "(quiet_start_minute >= 0 AND quiet_start_minute <= 1439)"),
        sa.CheckConstraint(
            "quiet_end_minute IS NULL OR "
            "(quiet_end_minute >= 0 AND quiet_end_minute <= 1439)"))
    op.create_table("product_notifications",
        sa.Column("notification_id", sa.String(128), primary_key=True),
        sa.Column("account_id", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("activity_sequence", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("activity_sequence >= 0"),
        sa.CheckConstraint(
            "category IN ('task','message','resource','topic','agent','due_soon','overdue')"),
        sa.UniqueConstraint("account_id", "project_id", "activity_sequence", "category",
            "subject_id", name="uq_product_notification_projection"))
    op.create_index("ix_product_notifications_account_visibility", "product_notifications",
        ["account_id", "archived_at", "read_at", "created_at"])
    op.create_table("product_notification_deliveries",
        sa.Column("delivery_id", sa.String(128), primary_key=True),
        sa.Column("notification_id", sa.String(128),
            sa.ForeignKey("product_notifications.notification_id"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel IN ('in_app','email','im')"),
        sa.CheckConstraint("status IN ('pending','delivered','failed')"),
        sa.UniqueConstraint("idempotency_key", name="uq_product_notification_delivery_key"))


def downgrade() -> None:
    op.drop_table("product_notification_deliveries")
    op.drop_index("ix_product_notifications_account_visibility",
        table_name="product_notifications")
    op.drop_table("product_notifications")
    op.drop_table("product_notification_preferences")
