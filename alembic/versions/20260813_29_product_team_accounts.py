"""Add team-first product accounts, projects, and propagation policy records.

Revision ID: 20260813_29
Revises: 20260813_28
Create Date: 2026-08-13
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260813_29"
down_revision: str | None = "20260813_28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("product_teams",
        sa.Column("team_id", sa.String(128), primary_key=True),
        sa.Column("handle", sa.String(64), nullable=False, unique=True),
        sa.Column("handle_key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("product_accounts",
        sa.Column("account_id", sa.String(128), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("username_key", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("email_key", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("team_role", sa.String(32), nullable=False),
        sa.Column("registration_status", sa.String(32), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("team_role IN ('owner','admin','member')"),
        sa.CheckConstraint("registration_status IN ('pending','active','rejected')"))
    op.create_index("ix_product_accounts_team_status", "product_accounts",
        ["team_id", "registration_status"])
    op.create_table("product_account_registrations",
        sa.Column("account_id", sa.String(128), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("username_key", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_key", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','active','rejected')"))
    op.create_index("ix_product_registration_team_status", "product_account_registrations",
        ["team_id", "status"])
    op.create_table("product_account_sessions",
        sa.Column("session_hash", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(session_hash)=64"))
    op.create_table("product_team_relation_requests",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("sender_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("recipient_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("requested_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sender_team_id<>recipient_team_id"),
        sa.CheckConstraint("status IN ('pending','accepted','declined','cancelled')"))
    op.create_table("product_team_relations",
        sa.Column("team_low", sa.String(128), sa.ForeignKey("product_teams.team_id"), primary_key=True),
        sa.Column("team_high", sa.String(128), sa.ForeignKey("product_teams.team_id"), primary_key=True),
        sa.Column("accepted_request_id", sa.String(128),
            sa.ForeignKey("product_team_relation_requests.request_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("team_low<team_high"))
    op.create_table("product_projects",
        sa.Column("project_id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("product_project_participations",
        sa.Column("project_id", sa.String(128), sa.ForeignKey("product_projects.project_id"), primary_key=True),
        sa.Column("team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("assigned_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "name"))
    op.create_table("product_project_memberships",
        sa.Column("project_id", sa.String(128), primary_key=True),
        sa.Column("team_id", sa.String(128), primary_key=True),
        sa.Column("account_id", sa.String(128), sa.ForeignKey("product_accounts.account_id"), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("added_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "team_id"],
            ["product_project_participations.project_id", "product_project_participations.team_id"]),
        sa.UniqueConstraint("project_id", "account_id"))
    op.create_table("product_project_resources",
        sa.Column("resource_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("owner_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("artifact_owner_team_id", sa.String(128), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(256), nullable=False),
        sa.Column("propagation", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("propagation IN ('team_private','project_readonly','portable')"),
        sa.CheckConstraint("length(artifact_sha256)=64"),
        sa.UniqueConstraint("artifact_owner_team_id", "artifact_id"))
    op.create_table("product_resource_shares",
        sa.Column("share_id", sa.String(128), primary_key=True),
        sa.Column("resource_id", sa.String(128), sa.ForeignKey("product_project_resources.resource_id"), nullable=False),
        sa.Column("shared_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("recipient_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("resource_id", "recipient_team_id"))
    op.create_table("product_personal_library",
        sa.Column("account_id", sa.String(128), sa.ForeignKey("product_accounts.account_id"), primary_key=True),
        sa.Column("resource_id", sa.String(128), sa.ForeignKey("product_project_resources.resource_id"), primary_key=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("product_project_messages",
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("source_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("target_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_team_id<>target_team_id"))
    op.create_table("product_team_tasks",
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("source_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("target_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("created_by", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assigned_account_id", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=True),
        sa.Column("artifact_resource_ids", sa.Text(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_team_id<>target_team_id"),
        sa.CheckConstraint("status IN ('proposed','accepted','in_progress','submitted','verified','changes_requested','rejected')"))
    op.create_table("product_project_activities",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(128), sa.ForeignKey("product_projects.project_id"), nullable=False),
        sa.Column("actor_account_id", sa.String(128), sa.ForeignKey("product_accounts.account_id"), nullable=False),
        sa.Column("actor_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("target_team_id", sa.String(128), sa.ForeignKey("product_teams.team_id"), nullable=True),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    for table in ("product_project_activities", "product_team_tasks",
            "product_project_messages", "product_personal_library", "product_resource_shares",
            "product_project_resources", "product_project_memberships",
            "product_project_participations", "product_projects", "product_team_relations",
            "product_team_relation_requests",
            "product_account_sessions", "product_account_registrations",
            "product_accounts", "product_teams"):
        op.drop_table(table)
