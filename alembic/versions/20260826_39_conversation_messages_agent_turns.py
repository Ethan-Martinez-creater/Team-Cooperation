"""Conversation messages and durable agent turns.

Revision ID: 20260826_39
Revises: 20260826_38
Create Date: 2026-08-26
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260826_39"
down_revision: str | None = "20260826_38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_project_conversation_messages",
        sa.Column("conversation_id", sa.String(128),
            sa.ForeignKey("product_project_conversations.conversation_id"),
            nullable=False, primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.String(128), nullable=True),
        sa.Column("run_id", sa.String(128), nullable=True),
        sa.Column("message_kind", sa.String(32), nullable=False, server_default="text"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="positive_sequence"),
        sa.CheckConstraint("role IN ('user','assistant','system')", name="role"),
        sa.CheckConstraint("message_kind IN ('text','system')", name="message_kind"))
    op.create_index("ix_product_messages_conversation_sequence",
        "product_project_conversation_messages", ["conversation_id", "sequence"])
    op.create_table(
        "product_project_agent_turns",
        sa.Column("turn_id", sa.String(128), primary_key=True),
        sa.Column("conversation_id", sa.String(128),
            sa.ForeignKey("product_project_conversations.conversation_id"), nullable=False),
        sa.Column("user_message_sequence", sa.Integer(), nullable=False),
        sa.Column("assistant_message_sequence", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(128), nullable=True),
        sa.Column("trigger_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("trigger_kind IN ('user_message','exchange')", name="trigger_kind"),
        sa.CheckConstraint("status IN ('active','completed','failed','cancelled')",
            name="status"),
        sa.UniqueConstraint("conversation_id", "idempotency_key",
            name="uq_turn_idempotency"))
    op.create_index("ix_product_turns_conversation_status",
        "product_project_agent_turns", ["conversation_id", "status"])
    op.add_column("product_project_agent_runs",
        sa.Column("conversation_id", sa.String(128), nullable=True))
    op.add_column("product_project_agent_runs",
        sa.Column("turn_id", sa.String(128), nullable=True))
    op.create_index("ix_product_project_agent_runs_conversation",
        "product_project_agent_runs", ["conversation_id", "turn_id"])


def downgrade() -> None:
    op.drop_index("ix_product_project_agent_runs_conversation",
        table_name="product_project_agent_runs")
    op.drop_column("product_project_agent_runs", "turn_id")
    op.drop_column("product_project_agent_runs", "conversation_id")
    op.drop_index("ix_product_turns_conversation_status",
        table_name="product_project_agent_turns")
    op.drop_table("product_project_agent_turns")
    op.drop_index("ix_product_messages_conversation_sequence",
        table_name="product_project_conversation_messages")
    op.drop_table("product_project_conversation_messages")