"""Add resource versions, derivatives and document change drafts.

Revision ID: 20260822_37
Revises: 20260822_36
Create Date: 2026-08-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260822_37"
down_revision: str | None = "20260822_36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("product_resource_versions",
        sa.Column("version_id", sa.String(128), primary_key=True),
        sa.Column("resource_id", sa.String(128),
            sa.ForeignKey("product_project_resources.resource_id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("parent_version_id", sa.String(128), nullable=True),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version_number >= 1"),
        sa.CheckConstraint("length(artifact_sha256)=64"),
        sa.UniqueConstraint("resource_id", "version_number",
            name="uq_product_resource_version_number"))
    op.create_index("ix_product_resource_versions_resource",
        "product_resource_versions", ["resource_id", "version_number"])
    op.create_table("product_resource_derivatives",
        sa.Column("derivative_id", sa.String(128), primary_key=True),
        sa.Column("resource_id", sa.String(128),
            sa.ForeignKey("product_project_resources.resource_id"), nullable=False),
        sa.Column("version_id", sa.String(128),
            sa.ForeignKey("product_resource_versions.version_id"), nullable=False),
        sa.Column("derivative_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("content_artifact_id", sa.String(128), nullable=True),
        sa.Column("content_artifact_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "derivative_type IN ('plain_text','page_preview','thumbnail','structured_content')"),
        sa.CheckConstraint("status IN ('ready','failed')"),
        sa.CheckConstraint("size_bytes >= 0"),
        sa.CheckConstraint(
            "content_artifact_sha256 IS NULL OR length(content_artifact_sha256)=64"))
    op.create_index("ix_product_resource_derivatives_resource",
        "product_resource_derivatives", ["resource_id", "version_id"])
    op.create_table("product_document_change_drafts",
        sa.Column("draft_id", sa.String(128), primary_key=True),
        sa.Column("resource_id", sa.String(128),
            sa.ForeignKey("product_project_resources.resource_id"), nullable=False),
        sa.Column("source_version_id", sa.String(128),
            sa.ForeignKey("product_resource_versions.version_id"), nullable=False),
        sa.Column("modification_json", sa.Text(), nullable=False),
        sa.Column("generated_version_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','rejected','approved')"),
        sa.CheckConstraint("version >= 1"))
    op.create_index("ix_product_document_drafts_resource_status",
        "product_document_change_drafts", ["resource_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_product_document_drafts_resource_status",
        table_name="product_document_change_drafts")
    op.drop_table("product_document_change_drafts")
    op.drop_index("ix_product_resource_derivatives_resource",
        table_name="product_resource_derivatives")
    op.drop_table("product_resource_derivatives")
    op.drop_index("ix_product_resource_versions_resource",
        table_name="product_resource_versions")
    op.drop_table("product_resource_versions")
