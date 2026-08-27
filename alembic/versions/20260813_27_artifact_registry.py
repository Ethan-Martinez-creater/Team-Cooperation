"""Add durable cross-team artifact manifest registry.

Revision ID: 20260813_27
Revises: 20260813_26
Create Date: 2026-08-13
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260813_27"
down_revision: str | None = "20260813_26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("artifact_manifests",
        sa.Column("owner_tenant_id", sa.String(128), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(256), nullable=False),
        sa.Column("content_uri", sa.String(2048), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("classification", sa.Integer(), nullable=False),
        sa.Column("compartments", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.Column("producer_principal_id", sa.String(128), nullable=False),
        sa.Column("source_tool", sa.String(128), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visible_to_tenants", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("metadata_schema", sa.String(128), nullable=False),
        sa.CheckConstraint("classification BETWEEN 0 AND 3", name="ck_artifact_manifests_classification"),
        sa.CheckConstraint("size_bytes >= 0 AND length(sha256)=64 AND length(content_digest)=64", name="ck_artifact_manifests_integrity"),
        sa.PrimaryKeyConstraint("owner_tenant_id", "artifact_id"))
    op.create_table("artifact_manifest_commands",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(request_digest)=64", name="ck_artifact_manifest_commands_digest"),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"))
    op.execute("ALTER TABLE artifact_manifests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE artifact_manifests FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY artifact_manifests_select ON artifact_manifests FOR SELECT USING (current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("CREATE POLICY artifact_manifests_insert ON artifact_manifests FOR INSERT WITH CHECK (owner_tenant_id=current_setting('coifesp.tenant_id',true) AND current_setting('coifesp.tenant_id',true)=ANY(visible_to_tenants))")
    op.execute("ALTER TABLE artifact_manifest_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE artifact_manifest_commands FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY artifact_manifest_commands_all ON artifact_manifest_commands FOR ALL USING (tenant_id=current_setting('coifesp.tenant_id',true)) WITH CHECK (tenant_id=current_setting('coifesp.tenant_id',true))")


def downgrade() -> None:
    op.drop_table("artifact_manifest_commands")
    op.drop_table("artifact_manifests")
