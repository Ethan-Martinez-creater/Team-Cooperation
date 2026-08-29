from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

PRODUCT_METADATA = MetaData()

TEAMS = Table(
    "product_teams",
    PRODUCT_METADATA,
    Column("team_id", String(128), primary_key=True),
    Column("handle", String(64), nullable=False, unique=True),
    Column("handle_key", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

ACCOUNTS = Table(
    "product_accounts",
    PRODUCT_METADATA,
    Column("account_id", String(128), primary_key=True),
    Column("username", String(64), nullable=False, unique=True),
    Column("username_key", String(64), nullable=False, unique=True),
    Column("display_name", String(128), nullable=False),
    Column("email", String(320), nullable=False, unique=True),
    Column("email_key", String(320), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("team_role", String(32), nullable=False),
    Column("registration_status", String(32), nullable=False),
    Column("must_change_password", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_product_accounts_team", ACCOUNTS.c.team_id)
Index("ix_product_accounts_team_status", ACCOUNTS.c.team_id, ACCOUNTS.c.registration_status)

ACCOUNT_REGISTRATIONS = Table(
    "product_account_registrations",
    PRODUCT_METADATA,
    Column("account_id", String(128), primary_key=True),
    Column("username", String(64), nullable=False),
    Column("username_key", String(64), nullable=False, unique=True),
    Column("display_name", String(128), nullable=False),
    Column("email", String(320), nullable=False),
    Column("email_key", String(320), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", String(128), ForeignKey("product_accounts.account_id"), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('pending','active','rejected')", name="status"),
)
Index(
    "ix_product_registration_team_status",
    ACCOUNT_REGISTRATIONS.c.team_id,
    ACCOUNT_REGISTRATIONS.c.status,
)

ACCOUNT_SESSIONS = Table(
    "product_account_sessions",
    PRODUCT_METADATA,
    Column("session_hash", String(64), primary_key=True),
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("length(session_hash) = 64", name="session_hash"),
)
Index(
    "ix_product_sessions_account_expiry",
    ACCOUNT_SESSIONS.c.account_id,
    ACCOUNT_SESSIONS.c.expires_at,
)

CONTACT_REQUESTS = Table(
    "product_team_relation_requests",
    PRODUCT_METADATA,
    Column("request_id", String(128), primary_key=True),
    Column("sender_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("recipient_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("requested_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("message", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("sender_team_id <> recipient_team_id", name="different_teams"),
    CheckConstraint("status IN ('pending','accepted','declined','cancelled')", name="status"),
)
Index(
    "ix_product_relation_recipient_status",
    CONTACT_REQUESTS.c.recipient_team_id,
    CONTACT_REQUESTS.c.status,
)

CONTACTS = Table(
    "product_team_relations",
    PRODUCT_METADATA,
    Column("team_low", String(128), ForeignKey("product_teams.team_id"), primary_key=True),
    Column("team_high", String(128), ForeignKey("product_teams.team_id"), primary_key=True),
    Column(
        "accepted_request_id",
        String(128),
        ForeignKey("product_team_relation_requests.request_id"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("team_low < team_high", name="ordered_teams"),
)

PROJECTS = Table(
    "product_projects",
    PRODUCT_METADATA,
    Column("project_id", String(128), primary_key=True),
    Column("name", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("owner_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_product_projects_owner_created", PROJECTS.c.owner_team_id, PROJECTS.c.created_at)

PROJECT_TEAMS = Table(
    "product_project_participations",
    PRODUCT_METADATA,
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), primary_key=True),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("assigned_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("project_id", "name", name="uq_product_project_participation_name"),
    CheckConstraint(
        "kind IN ('product','engineering','quality','design','operations','custom')", name="kind"
    ),
)
Index("ix_product_teams_project", PROJECT_TEAMS.c.project_id)

PROJECT_MEMBERSHIPS = Table(
    "product_project_memberships",
    PRODUCT_METADATA,
    Column("project_id", String(128), primary_key=True),
    Column("team_id", String(128), primary_key=True),
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), primary_key=True),
    Column("role", String(32), nullable=False),
    Column("added_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("joined_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["project_id", "team_id"],
        ["product_project_participations.project_id", "product_project_participations.team_id"],
    ),
    CheckConstraint("role IN ('lead','contributor','reviewer','observer')", name="role"),
    UniqueConstraint("project_id", "account_id", name="uq_product_project_account"),
)
Index(
    "ix_product_memberships_account_project",
    PROJECT_MEMBERSHIPS.c.account_id,
    PROJECT_MEMBERSHIPS.c.project_id,
)

PROJECT_RESOURCES = Table(
    "product_project_resources",
    PRODUCT_METADATA,
    Column("resource_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("owner_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("title", String(256), nullable=False),
    Column("artifact_owner_team_id", String(128), nullable=False),
    Column("artifact_id", String(128), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("media_type", String(256), nullable=False),
    Column("propagation", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "propagation IN ('team_private','project_readonly','portable')", name="propagation"
    ),
    CheckConstraint("length(artifact_sha256) = 64", name="artifact_sha256"),
    UniqueConstraint("artifact_owner_team_id", "artifact_id", name="uq_product_resource_artifact"),
)
Index(
    "ix_product_resources_project_created",
    PROJECT_RESOURCES.c.project_id,
    PROJECT_RESOURCES.c.created_at,
)

RESOURCE_SHARES = Table(
    "product_resource_shares",
    PRODUCT_METADATA,
    Column("share_id", String(128), primary_key=True),
    Column(
        "resource_id",
        String(128),
        ForeignKey("product_project_resources.resource_id"),
        nullable=False,
    ),
    Column("shared_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("recipient_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("resource_id", "recipient_team_id", name="uq_product_resource_recipient_team"),
)

PERSONAL_LIBRARY = Table(
    "product_personal_library",
    PRODUCT_METADATA,
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), primary_key=True),
    Column(
        "resource_id",
        String(128),
        ForeignKey("product_project_resources.resource_id"),
        primary_key=True,
    ),
    Column("saved_at", DateTime(timezone=True), nullable=False),
)

PROJECT_MESSAGES = Table(
    "product_project_messages",
    PRODUCT_METADATA,
    Column("message_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("source_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("target_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("source_team_id <> target_team_id", name="different_message_teams"),
)
Index(
    "ix_product_messages_project_created",
    PROJECT_MESSAGES.c.project_id,
    PROJECT_MESSAGES.c.created_at,
)

TEAM_TASKS = Table(
    "product_team_tasks",
    PRODUCT_METADATA,
    Column("task_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("source_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("target_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("acceptance_criteria", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column(
        "assigned_account_id", String(128), ForeignKey("product_accounts.account_id"), nullable=True
    ),
    Column("artifact_resource_ids", Text, nullable=False),
    Column("review_note", Text, nullable=False),
    Column("priority", String(16), nullable=False, server_default="normal"),
    Column("due_at", DateTime(timezone=True), nullable=True),
    Column("schedule_version", Integer, nullable=False, server_default="1"),
    Column("due_changed_at", DateTime(timezone=True), nullable=True),
    Column("due_changed_by", String(128), ForeignKey("product_accounts.account_id"), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("source_team_id <> target_team_id", name="different_task_teams"),
    CheckConstraint(
        "status IN ('proposed','accepted','in_progress','submitted','verified','changes_requested','rejected')",
        name="status",
    ),
    CheckConstraint("priority IN ('low','normal','high','urgent')", name="priority"),
    CheckConstraint("schedule_version >= 1", name="positive_schedule_version"),
)
Index("ix_product_tasks_project_status", TEAM_TASKS.c.project_id, TEAM_TASKS.c.status)
Index(
    "ix_product_tasks_team_status_due",
    TEAM_TASKS.c.target_team_id,
    TEAM_TASKS.c.status,
    TEAM_TASKS.c.due_at,
)
Index(
    "ix_product_tasks_project_priority_due",
    TEAM_TASKS.c.project_id,
    TEAM_TASKS.c.priority,
    TEAM_TASKS.c.due_at,
)

TASK_SCHEDULE_PROPOSALS = Table(
    "product_task_schedule_proposals",
    PRODUCT_METADATA,
    Column("proposal_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("task_id", String(128), ForeignKey("product_team_tasks.task_id"), nullable=False),
    Column("proposed_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("proposed_by_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("decided_by_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=True),
    Column("old_priority", String(16), nullable=False),
    Column("new_priority", String(16), nullable=False),
    Column("old_due_at", DateTime(timezone=True), nullable=True),
    Column("new_due_at", DateTime(timezone=True), nullable=True),
    Column("reason", Text, nullable=False),
    Column("decision_reason", Text, nullable=False, server_default=""),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("schedule_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('pending','accepted','rejected','superseded')", name="proposal_status"
    ),
    CheckConstraint("old_priority IN ('low','normal','high','urgent')", name="old_priority"),
    CheckConstraint("new_priority IN ('low','normal','high','urgent')", name="new_priority"),
    CheckConstraint("version >= 1", name="positive_version"),
    CheckConstraint("schedule_version >= 1", name="based_schedule_version"),
)
Index(
    "ix_product_schedule_proposals_task_status",
    TASK_SCHEDULE_PROPOSALS.c.task_id,
    TASK_SCHEDULE_PROPOSALS.c.status,
)
Index(
    "ix_product_schedule_proposals_project_created",
    TASK_SCHEDULE_PROPOSALS.c.project_id,
    TASK_SCHEDULE_PROPOSALS.c.created_at,
)

NOTIFICATION_PREFERENCES = Table(
    "product_notification_preferences",
    PRODUCT_METADATA,
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), primary_key=True),
    Column("notify_tasks", Boolean, nullable=False, server_default="1"),
    Column("notify_messages", Boolean, nullable=False, server_default="1"),
    Column("notify_resources", Boolean, nullable=False, server_default="1"),
    Column("notify_topics", Boolean, nullable=False, server_default="1"),
    Column("notify_agent_events", Boolean, nullable=False, server_default="1"),
    Column("notify_due_soon", Boolean, nullable=False, server_default="1"),
    Column("notify_overdue", Boolean, nullable=False, server_default="1"),
    Column("due_soon_hours", Integer, nullable=False, server_default="48"),
    Column("time_zone", String(64), nullable=False, server_default="UTC"),
    Column("quiet_start_minute", Integer, nullable=True),
    Column("quiet_end_minute", Integer, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("due_soon_hours BETWEEN 1 AND 336", name="due_soon_window"),
    CheckConstraint(
        "(quiet_start_minute IS NULL AND quiet_end_minute IS NULL) OR "
        "(quiet_start_minute IS NOT NULL AND quiet_end_minute IS NOT NULL)",
        name="quiet_window_pair",
    ),
    CheckConstraint(
        "quiet_start_minute IS NULL OR (quiet_start_minute >= 0 AND quiet_start_minute <= 1439)",
        name="quiet_start_range",
    ),
    CheckConstraint(
        "quiet_end_minute IS NULL OR (quiet_end_minute >= 0 AND quiet_end_minute <= 1439)",
        name="quiet_end_range",
    ),
)

NOTIFICATIONS = Table(
    "product_notifications",
    PRODUCT_METADATA,
    Column("notification_id", String(128), primary_key=True),
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("activity_sequence", Integer, nullable=False),
    Column("category", String(32), nullable=False),
    Column("title", String(256), nullable=False),
    Column("summary", String(512), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("read_at", DateTime(timezone=True), nullable=True),
    Column("archived_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("activity_sequence >= 0", name="nonnegative_activity_sequence"),
    CheckConstraint(
        "category IN ('task','message','resource','topic','agent','due_soon','overdue')",
        name="category",
    ),
    UniqueConstraint(
        "account_id",
        "project_id",
        "activity_sequence",
        "category",
        "subject_id",
        name="uq_product_notification_projection",
    ),
)
Index(
    "ix_product_notifications_account_visibility",
    NOTIFICATIONS.c.account_id,
    NOTIFICATIONS.c.archived_at,
    NOTIFICATIONS.c.read_at,
    NOTIFICATIONS.c.created_at,
)

NOTIFICATION_DELIVERIES = Table(
    "product_notification_deliveries",
    PRODUCT_METADATA,
    Column("delivery_id", String(128), primary_key=True),
    Column(
        "notification_id",
        String(128),
        ForeignKey("product_notifications.notification_id"),
        nullable=False,
    ),
    Column("channel", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("channel IN ('in_app','email','im')", name="channel"),
    CheckConstraint("status IN ('pending','delivered','failed')", name="delivery_status"),
    UniqueConstraint("idempotency_key", name="uq_product_notification_delivery_key"),
)

PROJECT_ACTIVITIES = Table(
    "product_project_activities",
    PRODUCT_METADATA,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column(
        "actor_account_id", String(128), ForeignKey("product_accounts.account_id"), nullable=False
    ),
    Column("actor_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("target_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=True),
    Column("summary", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_product_activity_project_created",
    PROJECT_ACTIVITIES.c.project_id,
    PROJECT_ACTIVITIES.c.created_at,
)

PROJECT_ACTIVITY_CURSORS = Table(
    "product_project_activity_cursors",
    PRODUCT_METADATA,
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), primary_key=True),
    Column("last_read_sequence", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("last_read_sequence >= 0", name="nonnegative_sequence"),
)

PROJECT_AGENT_RUNS = Table(
    "product_project_agent_runs",
    PRODUCT_METADATA,
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("mode", String(32), nullable=False),
    Column("conversation_id", String(128), nullable=True),
    Column("turn_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("mode IN ('analysis','collaboration_actions','delivery_review')", name="mode"),
    UniqueConstraint("run_id", name="uq_product_project_agent_run"),
)

INBOX_AGENT_RUNS = Table(
    "product_inbox_agent_runs",
    PRODUCT_METADATA,
    Column("run_id", String(128), primary_key=True),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("mode", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("mode IN ('prioritization','status_briefing')", name="mode"),
)
Index(
    "ix_product_inbox_agent_runs_creator_created",
    INBOX_AGENT_RUNS.c.created_by,
    INBOX_AGENT_RUNS.c.created_at,
)

PROJECT_TOPICS = Table(
    "product_project_topics",
    PRODUCT_METADATA,
    Column("topic_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("proposed_by_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("title", String(256), nullable=False),
    Column("context", Text, nullable=False),
    Column("origin", String(32), nullable=False),
    Column("source_agent_run_id", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("decision", Text, nullable=False),
    Column("decided_by_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('open','decided','cancelled')", name="status"),
    CheckConstraint("origin IN ('human','agent_confirmed')", name="origin"),
)
Index("ix_product_topics_project_status", PROJECT_TOPICS.c.project_id, PROJECT_TOPICS.c.status)

PROJECT_TOPIC_CONTRIBUTIONS = Table(
    "product_project_topic_contributions",
    PRODUCT_METADATA,
    Column("contribution_id", String(128), primary_key=True),
    Column("topic_id", String(128), ForeignKey("product_project_topics.topic_id"), nullable=False),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_product_topic_contributions_topic_created",
    PROJECT_TOPIC_CONTRIBUTIONS.c.topic_id,
    PROJECT_TOPIC_CONTRIBUTIONS.c.created_at,
)

COLLABORATION_ACTION_DRAFTS = Table(
    "product_collaboration_action_drafts",
    PRODUCT_METADATA,
    Column("draft_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("source_agent_run_id", String(128), nullable=False),
    Column("source_message_sequence", Integer, nullable=False),
    Column("source_content_sha256", String(64), nullable=False),
    Column("action_index", Integer, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("executed_subject_id", String(128), nullable=True),
    Column("rejection_reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("source_message_sequence >= 1", name="positive_message_sequence"),
    CheckConstraint("action_index >= 0", name="nonnegative_action_index"),
    CheckConstraint("kind IN ('message','task','topic')", name="kind"),
    CheckConstraint("status IN ('pending','executed','rejected')", name="status"),
    CheckConstraint("version >= 1", name="positive_version"),
    CheckConstraint("length(source_content_sha256) = 64", name="source_content_sha256"),
    UniqueConstraint(
        "project_id",
        "source_agent_run_id",
        "source_message_sequence",
        "action_index",
        name="uq_product_collaboration_draft_source_action",
    ),
)
Index(
    "ix_product_collaboration_drafts_project_status",
    COLLABORATION_ACTION_DRAFTS.c.project_id,
    COLLABORATION_ACTION_DRAFTS.c.status,
)

PROJECT_REPOSITORIES = Table(
    "product_project_repositories",
    PRODUCT_METADATA,
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), primary_key=True),
    Column("repository_id", String(128), primary_key=True),
    Column("connector_id", String(128), nullable=False),
    Column("connector_version", Integer, nullable=False),
    Column("remote_repository_id", String(256), nullable=False),
    Column("default_branch", String(256), nullable=False),
    Column("available_operations", String(512), nullable=False),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("connector_version >= 1", name="positive_connector_version"),
)
Index(
    "ix_product_project_repositories_connector",
    PROJECT_REPOSITORIES.c.connector_id,
    PROJECT_REPOSITORIES.c.connector_version,
)

CODE_CHANGE_DRAFTS = Table(
    "product_code_change_drafts",
    PRODUCT_METADATA,
    Column("draft_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("repository_id", String(128), nullable=False),
    Column("base_commit", String(64), nullable=False),
    Column("files_json", Text, nullable=False),
    Column("patch_text", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("patch_artifact_id", String(128), nullable=True),
    Column("patch_artifact_sha256", String(64), nullable=True),
    CheckConstraint("status IN ('pending','rejected','approved','applied')", name="status"),
    CheckConstraint("version >= 1", name="positive_version"),
    CheckConstraint("length(base_commit) IN (40,64)", name="base_commit"),
    CheckConstraint(
        "patch_artifact_sha256 IS NULL OR length(patch_artifact_sha256)=64",
        name="patch_artifact_sha256",
    ),
)
Index(
    "ix_product_code_drafts_project_status",
    CODE_CHANGE_DRAFTS.c.project_id,
    CODE_CHANGE_DRAFTS.c.status,
)

RESOURCE_VERSIONS = Table(
    "product_resource_versions",
    PRODUCT_METADATA,
    Column("version_id", String(128), primary_key=True),
    Column(
        "resource_id",
        String(128),
        ForeignKey("product_project_resources.resource_id"),
        nullable=False,
    ),
    Column("version_number", Integer, nullable=False),
    Column("artifact_id", String(128), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("parent_version_id", String(128), nullable=True),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("reason", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("version_number >= 1", name="positive_version_number"),
    CheckConstraint("length(artifact_sha256)=64", name="artifact_sha256"),
    UniqueConstraint("resource_id", "version_number", name="uq_product_resource_version_number"),
)
Index(
    "ix_product_resource_versions_resource",
    RESOURCE_VERSIONS.c.resource_id,
    RESOURCE_VERSIONS.c.version_number,
)

RESOURCE_DERIVATIVES = Table(
    "product_resource_derivatives",
    PRODUCT_METADATA,
    Column("derivative_id", String(128), primary_key=True),
    Column(
        "resource_id",
        String(128),
        ForeignKey("product_project_resources.resource_id"),
        nullable=False,
    ),
    Column(
        "version_id",
        String(128),
        ForeignKey("product_resource_versions.version_id"),
        nullable=False,
    ),
    Column("derivative_type", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("summary", String(512), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("error_category", String(64), nullable=True),
    Column("content_artifact_id", String(128), nullable=True),
    Column("content_artifact_sha256", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "derivative_type IN ('plain_text','page_preview','thumbnail','structured_content')",
        name="derivative_type",
    ),
    CheckConstraint("status IN ('ready','failed')", name="status"),
    CheckConstraint("size_bytes >= 0", name="nonnegative_size"),
    CheckConstraint(
        "content_artifact_sha256 IS NULL OR length(content_artifact_sha256)=64",
        name="content_artifact_sha256",
    ),
)
Index(
    "ix_product_resource_derivatives_resource",
    RESOURCE_DERIVATIVES.c.resource_id,
    RESOURCE_DERIVATIVES.c.version_id,
)

DOCUMENT_CHANGE_DRAFTS = Table(
    "product_document_change_drafts",
    PRODUCT_METADATA,
    Column("draft_id", String(128), primary_key=True),
    Column(
        "resource_id",
        String(128),
        ForeignKey("product_project_resources.resource_id"),
        nullable=False,
    ),
    Column(
        "source_version_id",
        String(128),
        ForeignKey("product_resource_versions.version_id"),
        nullable=False,
    ),
    Column("modification_json", Text, nullable=False),
    Column("generated_version_id", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('pending','rejected','approved')", name="status"),
    CheckConstraint("version >= 1", name="positive_version"),
)
Index(
    "ix_product_document_drafts_resource_status",
    DOCUMENT_CHANGE_DRAFTS.c.resource_id,
    DOCUMENT_CHANGE_DRAFTS.c.status,
)

TEAM_AGENT_PROFILES = Table(
    "product_team_agent_profiles",
    PRODUCT_METADATA,
    Column("profile_id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("display_name", String(128), nullable=False),
    Column("tool_policy_id", String(128), nullable=False),
    Column("skill_policy_id", String(128), nullable=False),
    Column("model_policy_id", String(128), nullable=False),
    Column("memory_policy_id", String(128), nullable=False),
    Column("autonomy_level", String(32), nullable=False),
    Column("max_run_budget_profile", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("version >= 1", name="positive_version"),
    CheckConstraint(
        "autonomy_level IN ('supervised','bounded','autonomous')",
        name="autonomy_level",
    ),
    UniqueConstraint("team_id", "version", name="uq_team_agent_profile_version"),
)
Index(
    "ix_product_team_agent_profiles_team",
    TEAM_AGENT_PROFILES.c.team_id,
    TEAM_AGENT_PROFILES.c.version,
)

TEAM_PROJECT_AGENTS = Table(
    "product_team_project_agents",
    PRODUCT_METADATA,
    Column("agent_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("status", String(32), nullable=False, server_default="active"),
    Column("memory_version", Integer, nullable=False, server_default="1"),
    Column("profile_id", String(128), nullable=False),
    Column("profile_version", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('active','archived')", name="status"),
    CheckConstraint("memory_version >= 1", name="positive_memory_version"),
    CheckConstraint("profile_version >= 1", name="positive_profile_version"),
    ForeignKeyConstraint(
        ["profile_id", "profile_version"],
        [
            "product_team_agent_profiles.profile_id",
            "product_team_agent_profiles.version",
        ],
    ),
    UniqueConstraint("project_id", "team_id", name="uq_team_project_agent"),
)
Index(
    "ix_product_team_agents_project_status",
    TEAM_PROJECT_AGENTS.c.project_id,
    TEAM_PROJECT_AGENTS.c.status,
)

PROJECT_CONVERSATIONS = Table(
    "product_project_conversations",
    PRODUCT_METADATA,
    Column("conversation_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column(
        "team_agent_id",
        String(128),
        ForeignKey("product_team_project_agents.agent_id"),
        nullable=False,
    ),
    Column("account_id", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("status", String(32), nullable=False, server_default="active"),
    Column("last_message_sequence", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("archived_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('active','archived')", name="status"),
    CheckConstraint("last_message_sequence >= 0", name="nonnegative_sequence"),
    UniqueConstraint("project_id", "account_id", name="uq_project_account_conversation"),
)
Index(
    "ix_product_conversations_account_updated",
    PROJECT_CONVERSATIONS.c.account_id,
    PROJECT_CONVERSATIONS.c.updated_at,
)

PROJECT_CONVERSATION_MESSAGES = Table(
    "product_project_conversation_messages",
    PRODUCT_METADATA,
    Column(
        "conversation_id",
        String(128),
        ForeignKey("product_project_conversations.conversation_id"),
        primary_key=True,
    ),
    Column("sequence", Integer, primary_key=True),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("turn_id", String(128), nullable=True),
    Column("run_id", String(128), nullable=True),
    Column("message_kind", String(32), nullable=False, server_default="text"),
    Column("attachment_resource_ids", Text, nullable=False, server_default="[]"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("sequence >= 1", name="positive_sequence"),
    CheckConstraint("role IN ('user','assistant','system')", name="role"),
    CheckConstraint("message_kind IN ('text','system')", name="message_kind"),
)
Index(
    "ix_product_messages_conversation_sequence",
    PROJECT_CONVERSATION_MESSAGES.c.conversation_id,
    PROJECT_CONVERSATION_MESSAGES.c.sequence,
)

PROJECT_AGENT_TURNS = Table(
    "product_project_agent_turns",
    PRODUCT_METADATA,
    Column("turn_id", String(128), primary_key=True),
    Column(
        "conversation_id",
        String(128),
        ForeignKey("product_project_conversations.conversation_id"),
        nullable=False,
    ),
    Column("user_message_sequence", Integer, nullable=False),
    Column("assistant_message_sequence", Integer, nullable=True),
    Column("run_id", String(128), nullable=True),
    Column("trigger_kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "trigger_kind IN ('user_message','exchange','planning','exchange_draft')",
        name="trigger_kind",
    ),
    CheckConstraint("status IN ('active','completed','failed','cancelled')", name="turn_status"),
    UniqueConstraint("conversation_id", "idempotency_key", name="uq_turn_idempotency"),
)
Index(
    "ix_product_turns_conversation_status",
    PROJECT_AGENT_TURNS.c.conversation_id,
    PROJECT_AGENT_TURNS.c.status,
)

AGENT_EXCHANGE_DRAFTS = Table(
    "product_agent_exchange_drafts",
    PRODUCT_METADATA,
    Column("draft_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("source_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("source_conversation_id", String(128), nullable=True),
    Column("source_turn_id", String(128), nullable=True),
    Column("purpose", String(512), nullable=False),
    Column("summary", Text, nullable=False),
    Column("request", Text, nullable=False),
    Column("constraints", Text, nullable=False, server_default=""),
    Column("shared_resource_ids", Text, nullable=False, server_default="[]"),
    Column("recipient_team_ids", Text, nullable=False, server_default="[]"),
    Column("content_sha256", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="drafting"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("rejection_reason", Text, nullable=False, server_default=""),
    CheckConstraint("length(content_sha256)=64", name="content_sha256"),
    CheckConstraint("version >= 1", name="positive_version"),
    CheckConstraint("status IN ('drafting','approved','rejected','withdrawn')", name="status"),
    # One draft per generating turn so concurrent terminal projections cannot
    # create two; multiple NULLs stay allowed for manual drafts.
    UniqueConstraint("source_turn_id", name="uq_exchange_drafts_source_turn"),
)
Index(
    "ix_product_exchange_drafts_project_status",
    AGENT_EXCHANGE_DRAFTS.c.project_id,
    AGENT_EXCHANGE_DRAFTS.c.status,
)

AGENT_EXCHANGES = Table(
    "product_agent_exchanges",
    PRODUCT_METADATA,
    Column("exchange_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("source_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("source_conversation_id", String(128), nullable=True),
    Column("source_turn_id", String(128), nullable=True),
    Column("purpose", String(512), nullable=False),
    Column("summary", Text, nullable=False),
    Column("request", Text, nullable=False),
    Column("constraints", Text, nullable=False, server_default=""),
    Column("content_sha256", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="sent"),
    Column("approved_by", String(128), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(content_sha256)=64", name="content_sha256"),
    CheckConstraint("status IN ('sent','responded','closed')", name="status"),
)
Index(
    "ix_product_exchanges_project_created",
    AGENT_EXCHANGES.c.project_id,
    AGENT_EXCHANGES.c.created_at,
)

AGENT_EXCHANGE_RECIPIENTS = Table(
    "product_agent_exchange_recipients",
    PRODUCT_METADATA,
    Column(
        "exchange_id",
        String(128),
        ForeignKey("product_agent_exchanges.exchange_id"),
        primary_key=True,
    ),
    Column(
        "recipient_team_id",
        String(128),
        ForeignKey("product_teams.team_id"),
        primary_key=True,
    ),
    Column("context_snapshot", Text, nullable=False, server_default="{}"),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("response_id", String(128), nullable=True),
    Column("draft_content", Text, nullable=True),
    Column("draft_turn_id", String(128), nullable=True),
    Column("responded_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('pending','drafting','responded','declined')", name="status"),
)
Index(
    "ix_product_exchange_recipients_team_status",
    AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id,
    AGENT_EXCHANGE_RECIPIENTS.c.status,
)
Index(
    "ix_product_exchange_recipients_draft_turn",
    AGENT_EXCHANGE_RECIPIENTS.c.draft_turn_id,
)

AGENT_EXCHANGE_RESPONSES = Table(
    "product_agent_exchange_responses",
    PRODUCT_METADATA,
    Column("response_id", String(128), primary_key=True),
    Column(
        "exchange_id",
        String(128),
        ForeignKey("product_agent_exchanges.exchange_id"),
        nullable=False,
    ),
    Column("recipient_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
    Column("content", Text, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("approved_by", String(128), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False),
    Column("turn_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(content_sha256)=64", name="content_sha256"),
    UniqueConstraint("exchange_id", "recipient_team_id", name="uq_exchange_team_response"),
)
Index(
    "ix_product_exchange_responses_exchange",
    AGENT_EXCHANGE_RESPONSES.c.exchange_id,
    AGENT_EXCHANGE_RESPONSES.c.created_at,
)

PROJECT_PLAN_DRAFTS = Table(
    "product_project_plan_drafts",
    PRODUCT_METADATA,
    Column("draft_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column("source_conversation_id", String(128), nullable=True),
    Column("source_turn_id", String(128), nullable=True),
    Column("source_run_id", String(128), nullable=True),
    Column(
        "schema_version",
        String(64),
        nullable=False,
        server_default="coifesp.project-plan.v1",
    ),
    Column("plan_payload", JSON, nullable=False, server_default="{}"),
    Column("goals", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("phases", Text, nullable=False, server_default="[]"),
    Column("milestones", Text, nullable=False, server_default="[]"),
    Column("risks", Text, nullable=False, server_default="[]"),
    Column("dependencies", Text, nullable=False, server_default="[]"),
    Column("acceptance_criteria", Text, nullable=False, server_default="[]"),
    Column("content_sha256", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="drafting"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("rejection_reason", Text, nullable=False, server_default=""),
    CheckConstraint("length(content_sha256)=64", name="content_sha256"),
    CheckConstraint("version >= 1", name="positive_version"),
    CheckConstraint(
        "schema_version IN ('coifesp.project-plan.v1','coifesp.project-plan.v2')",
        name="schema_version",
    ),
    CheckConstraint("status IN ('drafting','approved','rejected')", name="status"),
    # One draft per source run so projection retries can never duplicate
    # plans even when callbacks race; multiple NULLs stay allowed.
    UniqueConstraint("source_run_id", name="uq_plan_drafts_source_run"),
)
Index(
    "ix_product_plan_drafts_project_status",
    PROJECT_PLAN_DRAFTS.c.project_id,
    PROJECT_PLAN_DRAFTS.c.status,
)

PROJECT_TEAM_REQUIREMENT_DRAFTS = Table(
    "product_project_team_requirement_drafts",
    PRODUCT_METADATA,
    Column("requirement_id", String(128), primary_key=True),
    Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
    Column(
        "plan_draft_id",
        String(128),
        ForeignKey("product_project_plan_drafts.draft_id"),
        nullable=True,
    ),
    Column("team_category", String(32), nullable=False),
    Column("team_count", Integer, nullable=False),
    Column("rationale", Text, nullable=False, server_default=""),
    Column("status", String(32), nullable=False, server_default="drafting"),
    Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("team_count >= 1", name="positive_team_count"),
    CheckConstraint(
        "team_category IN ('product','engineering','quality','design','operations','custom')",
        name="team_category",
    ),
    CheckConstraint("status IN ('drafting','approved','rejected')", name="status"),
)
Index(
    "ix_product_team_requirement_project",
    PROJECT_TEAM_REQUIREMENT_DRAFTS.c.project_id,
    PROJECT_TEAM_REQUIREMENT_DRAFTS.c.status,
)

ALL_PRODUCT_TABLES = (
    TEAMS,
    ACCOUNTS,
    ACCOUNT_REGISTRATIONS,
    ACCOUNT_SESSIONS,
    CONTACT_REQUESTS,
    CONTACTS,
    PROJECTS,
    PROJECT_TEAMS,
    PROJECT_MEMBERSHIPS,
    PROJECT_RESOURCES,
    RESOURCE_SHARES,
    PERSONAL_LIBRARY,
    PROJECT_MESSAGES,
    TEAM_TASKS,
    TASK_SCHEDULE_PROPOSALS,
    PROJECT_ACTIVITIES,
    PROJECT_ACTIVITY_CURSORS,
    PROJECT_AGENT_RUNS,
    INBOX_AGENT_RUNS,
    PROJECT_TOPICS,
    PROJECT_TOPIC_CONTRIBUTIONS,
    COLLABORATION_ACTION_DRAFTS,
    NOTIFICATION_PREFERENCES,
    NOTIFICATIONS,
    NOTIFICATION_DELIVERIES,
    PROJECT_REPOSITORIES,
    CODE_CHANGE_DRAFTS,
    RESOURCE_VERSIONS,
    RESOURCE_DERIVATIVES,
    DOCUMENT_CHANGE_DRAFTS,
    TEAM_AGENT_PROFILES,
    TEAM_PROJECT_AGENTS,
    PROJECT_CONVERSATIONS,
    PROJECT_CONVERSATION_MESSAGES,
    PROJECT_AGENT_TURNS,
    AGENT_EXCHANGE_DRAFTS,
    AGENT_EXCHANGES,
    AGENT_EXCHANGE_RECIPIENTS,
    AGENT_EXCHANGE_RESPONSES,
    PROJECT_PLAN_DRAFTS,
    PROJECT_TEAM_REQUIREMENT_DRAFTS,
)
