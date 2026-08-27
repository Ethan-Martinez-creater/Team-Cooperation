"""Add project repository bindings and code change drafts.

Revision ID: 20260822_36
Revises: 20260822_35
Create Date: 2026-08-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260822_36"
down_revision: str | None = "20260822_35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("product_project_repositories",
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), primary_key=True),
        sa.Column("repository_id", sa.String(128), primary_key=True),
        sa.Column("connector_id", sa.String(128), nullable=False),
        sa.Column("connector_version", sa.Integer(), nullable=False),
        sa.Column("remote_repository_id", sa.String(256), nullable=False),
        sa.Column("default_branch", sa.String(256), nullable=False),
        sa.Column("available_operations", sa.String(512), nullable=False),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("connector_version >= 1"))
    op.create_index("ix_product_project_repositories_connector",
        "product_project_repositories", ["connector_id", "connector_version"])
    op.create_table("product_code_change_drafts",
        sa.Column("draft_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128),
            sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("repository_id", sa.String(128), nullable=False),
        sa.Column("base_commit", sa.String(64), nullable=False),
        sa.Column("files_json", sa.Text(), nullable=False),
        sa.Column("patch_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(128),
            sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("patch_artifact_id", sa.String(128), nullable=True),
        sa.Column("patch_artifact_sha256", sa.String(64), nullable=True),
        sa.CheckConstraint("status IN ('pending','rejected','approved','applied')"),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("length(base_commit) IN (40,64)"),
        sa.CheckConstraint("patch_artifact_sha256 IS NULL OR length(patch_artifact_sha256)=64"))
    op.create_index("ix_product_code_drafts_project_status",
        "product_code_change_drafts", ["project_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_product_code_drafts_project_status",
        table_name="product_code_change_drafts")
    op.drop_table("product_code_change_drafts")
    op.drop_index("ix_product_project_repositories_connector",
        table_name="product_project_repositories")
    op.drop_table("product_project_repositories")
