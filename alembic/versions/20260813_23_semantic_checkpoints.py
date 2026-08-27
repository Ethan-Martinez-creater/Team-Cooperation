"""Add reviewed encrypted semantic checkpoints.

Revision ID: 20260813_23
Revises: 20260813_22
Create Date: 2026-08-13
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_23"
down_revision: str | None = "20260813_22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("semantic_checkpoints",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("checkpoint_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("owner_principal_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("classification", sa.Integer(), nullable=False),
        sa.Column("compartments", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.Column("source_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(128), nullable=False),
        sa.Column("cipher_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason_digest", sa.String(64), nullable=True),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name="ck_semantic_checkpoints_status"),
        sa.CheckConstraint("classification BETWEEN 0 AND 3", name="ck_semantic_checkpoints_classification"),
        sa.CheckConstraint("length(source_digest)=64 AND length(fingerprint)=64", name="ck_semantic_checkpoints_digests"),
        sa.CheckConstraint("length(nonce)=12", name="ck_semantic_checkpoints_nonce"),
        sa.CheckConstraint("cipher_version > 0 AND version > 0", name="ck_semantic_checkpoints_versions"),
        sa.CheckConstraint("(status='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR (status<>'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="ck_semantic_checkpoints_review"),
        sa.PrimaryKeyConstraint("tenant_id", "checkpoint_id"))
    op.create_index("ix_semantic_checkpoints_conversation",
        "semantic_checkpoints", ["tenant_id", "conversation_id", "created_at"])
    op.execute("ALTER TABLE semantic_checkpoints ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE semantic_checkpoints FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY semantic_checkpoints_tenant_isolation ON semantic_checkpoints FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")


def downgrade() -> None:
    op.drop_index("ix_semantic_checkpoints_conversation", table_name="semantic_checkpoints")
    op.drop_table("semantic_checkpoints")
