from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ContactRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class TeamRelationshipState(str, Enum):
    AVAILABLE = "available"
    PENDING_OUTGOING = "pending_outgoing"
    PENDING_INCOMING = "pending_incoming"
    CONNECTED = "connected"


class ProjectTeamKind(str, Enum):
    PRODUCT = "product"
    ENGINEERING = "engineering"
    QUALITY = "quality"
    DESIGN = "design"
    OPERATIONS = "operations"
    CUSTOM = "custom"


class ProjectRole(str, Enum):
    LEAD = "lead"
    CONTRIBUTOR = "contributor"
    REVIEWER = "reviewer"
    OBSERVER = "observer"


class TeamAccountRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class AccountRegistrationStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"


class DataPropagation(str, Enum):
    TEAM_PRIVATE = "team_private"
    PROJECT_READONLY = "project_readonly"
    PORTABLE = "portable"


class ResourceAction(str, Enum):
    VIEW = "view"
    DOWNLOAD = "download"
    SAVE = "save"
    RESHARE = "reshare"
    AGENT_USE = "agent_use"


class TeamTaskStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class TaskPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


TASK_PRIORITY_RANK = {
    TaskPriority.URGENT: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}

TASK_TERMINAL_STATUSES = frozenset({TeamTaskStatus.VERIFIED, TeamTaskStatus.REJECTED})

TASK_SCHEDULE_DUE_MAX_DAYS = 366


class TaskScheduleProposalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class NotificationCategory(str, Enum):
    TASK = "task"
    MESSAGE = "message"
    RESOURCE = "resource"
    TOPIC = "topic"
    AGENT = "agent"
    DUE_SOON = "due_soon"
    OVERDUE = "overdue"


class ProjectTopicStatus(str, Enum):
    OPEN = "open"
    DECIDED = "decided"
    CANCELLED = "cancelled"


class CollaborationDraftKind(str, Enum):
    MESSAGE = "message"
    TASK = "task"
    TOPIC = "topic"


class CollaborationDraftStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"


class ProjectAgentMode(str, Enum):
    ANALYSIS = "analysis"
    COLLABORATION_ACTIONS = "collaboration_actions"
    DELIVERY_REVIEW = "delivery_review"


class ProjectAgentRunKind(str, Enum):
    CONVERSATION = "conversation"
    PLANNING = "planning"
    TASK_EXECUTION = "task_execution"
    VERIFICATION = "verification"
    REPLANNING = "replanning"
    EXCHANGE_DRAFT = "exchange_draft"
    SPECIALIST = "specialist"


class InboxAgentMode(str, Enum):
    PRIORITIZATION = "prioritization"
    STATUS_BRIEFING = "status_briefing"


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    username: str
    display_name: str
    email: str
    team_id: str
    team_role: TeamAccountRole
    registration_status: AccountRegistrationStatus
    must_change_password: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccountRegistration:
    account_id: str
    username: str
    display_name: str
    email: str
    team_id: str
    status: AccountRegistrationStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamBootstrapAdmin:
    username: str
    initial_password: str
    account: Account


@dataclass(frozen=True, slots=True)
class AccountSession:
    token: str
    account: Account
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ContactRequest:
    request_id: str
    sender_account_id: str
    recipient_account_id: str
    message: str
    status: ContactRequestStatus
    created_at: datetime
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Team:
    team_id: str
    handle: str
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamDirectoryEntry:
    team: Team
    relationship: TeamRelationshipState


@dataclass(frozen=True, slots=True)
class TeamDirectoryPage:
    items: tuple[TeamDirectoryEntry, ...]
    next_after_handle: str | None


@dataclass(frozen=True, slots=True)
class TeamRelationRequest:
    request_id: str
    sender_team_id: str
    recipient_team_id: str
    requested_by: str
    message: str
    status: ContactRequestStatus
    created_at: datetime
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    name: str
    description: str
    owner_team_id: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectTeam:
    team_id: str
    project_id: str
    name: str
    kind: ProjectTeamKind
    assigned_by: str


@dataclass(frozen=True, slots=True)
class ProjectMembership:
    project_id: str
    team_id: str
    account_id: str
    role: ProjectRole
    added_by: str


@dataclass(frozen=True, slots=True)
class ProjectResource:
    resource_id: str
    project_id: str
    owner_team_id: str
    created_by: str
    title: str
    artifact_owner_team_id: str
    artifact_id: str
    artifact_sha256: str
    media_type: str
    propagation: DataPropagation
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    allowed: bool
    reason: str
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectDetail:
    project: Project
    teams: tuple[ProjectTeam, ...]


@dataclass(frozen=True, slots=True)
class TeamRelation:
    team: Team
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectMessage:
    message_id: str
    project_id: str
    source_team_id: str
    target_team_id: str
    created_by: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamTask:
    task_id: str
    project_id: str
    source_team_id: str
    target_team_id: str
    created_by: str
    title: str
    description: str
    acceptance_criteria: str
    status: TeamTaskStatus
    assigned_account_id: str | None
    artifact_resource_ids: tuple[str, ...]
    review_note: str
    created_at: datetime
    updated_at: datetime
    priority: TaskPriority = TaskPriority.NORMAL
    due_at: datetime | None = None
    schedule_version: int = 1
    due_changed_at: datetime | None = None
    due_changed_by: str | None = None
    completed_at: datetime | None = None


def task_is_overdue(status: TeamTaskStatus, due_at: datetime | None, now: datetime) -> bool:
    if due_at is None or status in TASK_TERMINAL_STATUSES:
        return False
    return now > due_at


def task_due_in_seconds(due_at: datetime | None, now: datetime) -> int | None:
    if due_at is None:
        return None
    return int((due_at - now).total_seconds())


def compute_team_task_schedule(
    *,
    status: TeamTaskStatus,
    priority: TaskPriority,
    due_at: datetime | None,
    schedule_version: int,
    now: datetime,
    due_soon_hours: int = 48,
) -> TeamTaskSchedule:
    is_overdue = task_is_overdue(status, due_at, now)
    due_in = task_due_in_seconds(due_at, now)
    is_due_soon = not is_overdue and due_in is not None and 0 <= due_in <= due_soon_hours * 3600
    return TeamTaskSchedule(
        is_overdue=is_overdue,
        due_in_seconds=due_in,
        is_due_soon=is_due_soon,
        schedule_version=schedule_version,
        priority=priority,
        due_at=due_at,
    )


@dataclass(frozen=True, slots=True)
class ProjectActivity:
    sequence: int
    project_id: str
    actor_account_id: str
    actor_team_id: str
    event_type: str
    subject_id: str
    target_team_id: str | None
    summary: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectNotificationSummary:
    project_id: str
    project_name: str
    unread_count: int
    latest_sequence: int
    last_read_sequence: int


@dataclass(frozen=True, slots=True)
class CollaborationInboxAction:
    project_name: str
    action: str
    task: TeamTask


@dataclass(frozen=True, slots=True)
class CollaborationInboxActivity:
    project_name: str
    activity: ProjectActivity


@dataclass(frozen=True, slots=True)
class CollaborationInbox:
    actions: tuple[CollaborationInboxAction, ...]
    unread_activities: tuple[CollaborationInboxActivity, ...]
    action_count: int
    unread_count: int


@dataclass(frozen=True, slots=True)
class InboxAgentRun:
    run_id: str
    team_id: str
    created_by: str
    mode: InboxAgentMode
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectTopic:
    topic_id: str
    project_id: str
    proposed_by_team_id: str
    created_by: str
    title: str
    context: str
    origin: str
    source_agent_run_id: str | None
    status: ProjectTopicStatus
    decision: str
    decided_by_team_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectTopicContribution:
    contribution_id: str
    topic_id: str
    project_id: str
    team_id: str
    created_by: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CollaborationActionDraft:
    draft_id: str
    project_id: str
    source_agent_run_id: str
    source_message_sequence: int
    source_content_sha256: str
    action_index: int
    kind: CollaborationDraftKind
    payload: dict
    status: CollaborationDraftStatus
    version: int
    created_by: str
    team_id: str
    executed_subject_id: str | None
    rejection_reason: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectAgentRun:
    project_id: str
    run_id: str
    team_id: str
    created_by: str
    mode: ProjectAgentMode
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskScheduleProposal:
    proposal_id: str
    project_id: str
    task_id: str
    proposed_by: str
    proposed_by_team_id: str
    decided_by_team_id: str | None
    old_priority: TaskPriority
    new_priority: TaskPriority
    old_due_at: datetime | None
    new_due_at: datetime | None
    reason: str
    decision_reason: str
    status: TaskScheduleProposalStatus
    version: int
    schedule_version: int
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationPreference:
    account_id: str
    notify_tasks: bool
    notify_messages: bool
    notify_resources: bool
    notify_topics: bool
    notify_agent_events: bool
    notify_due_soon: bool
    notify_overdue: bool
    due_soon_hours: int
    time_zone: str
    quiet_start_minute: int | None
    quiet_end_minute: int | None
    updated_at: datetime | None = None

    def category_enabled(self, category: NotificationCategory) -> bool:
        return {
            NotificationCategory.TASK: self.notify_tasks,
            NotificationCategory.MESSAGE: self.notify_messages,
            NotificationCategory.RESOURCE: self.notify_resources,
            NotificationCategory.TOPIC: self.notify_topics,
            NotificationCategory.AGENT: self.notify_agent_events,
            NotificationCategory.DUE_SOON: self.notify_due_soon,
            NotificationCategory.OVERDUE: self.notify_overdue,
        }[category]


@dataclass(frozen=True, slots=True)
class Notification:
    notification_id: str
    account_id: str
    project_id: str
    activity_sequence: int
    category: NotificationCategory
    title: str
    summary: str
    subject_id: str
    read_at: datetime | None
    archived_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: tuple[Notification, ...]
    unread_count: int
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TeamTaskSchedule:
    """Server-computed scheduling projection for inbox and task views."""

    is_overdue: bool
    due_in_seconds: int | None
    is_due_soon: bool
    schedule_version: int
    priority: TaskPriority
    due_at: datetime | None


class CodeDraftStatus(str, Enum):
    PENDING = "pending"
    REJECTED = "rejected"
    APPROVED = "approved"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class ProjectRepository:
    project_id: str
    repository_id: str
    connector_id: str
    connector_version: int
    remote_repository_id: str
    default_branch: str
    available_operations: tuple[str, ...]
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RepositoryEntry:
    path: str
    kind: str  # blob | tree | commit
    mode: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class RepositoryBlob:
    path: str
    commit: str
    size_bytes: int
    sha256: str
    text: str | None  # None when the blob is binary or exceeds text limits


@dataclass(frozen=True, slots=True)
class CodeSearchHit:
    path: str
    line: int
    content: str


@dataclass(frozen=True, slots=True)
class CodeChangeDraft:
    draft_id: str
    project_id: str
    repository_id: str
    base_commit: str
    files: tuple[str, ...]
    patch_text: str
    status: CodeDraftStatus
    version: int
    created_by: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    patch_artifact_id: str | None
    patch_artifact_sha256: str | None


class DerivativeType(str, Enum):
    PLAIN_TEXT = "plain_text"
    PAGE_PREVIEW = "page_preview"
    THUMBNAIL = "thumbnail"
    STRUCTURED_CONTENT = "structured_content"


class DerivativeStatus(str, Enum):
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResourceVersion:
    version_id: str
    resource_id: str
    version_number: int
    artifact_id: str
    artifact_sha256: str
    parent_version_id: str | None
    created_by: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceDerivative:
    derivative_id: str
    resource_id: str
    version_id: str
    derivative_type: DerivativeType
    status: DerivativeStatus
    summary: str
    size_bytes: int
    error_category: str | None
    created_at: datetime


class DocumentChangeDraftStatus(str, Enum):
    PENDING = "pending"
    REJECTED = "rejected"
    APPROVED = "approved"


@dataclass(frozen=True, slots=True)
class DocumentChangeDraft:
    draft_id: str
    resource_id: str
    source_version_id: str
    modification_json: str
    generated_version_id: str | None
    status: DocumentChangeDraftStatus
    version: int
    created_by: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnTriggerKind(str, Enum):
    USER_MESSAGE = "user_message"
    EXCHANGE = "exchange"
    EXCHANGE_DRAFT = "exchange_draft"
    PLANNING = "planning"


class TurnStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConversationMessageKind(str, Enum):
    TEXT = "text"
    SYSTEM = "system"


class TeamProjectAgentStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class TeamAgentProfile:
    profile_id: str
    team_id: str
    version: int
    display_name: str
    tool_policy_id: str
    skill_policy_id: str
    model_policy_id: str
    memory_policy_id: str
    autonomy_level: str
    max_run_budget_profile: dict
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamProjectAgent:
    agent_id: str
    project_id: str
    team_id: str
    status: TeamProjectAgentStatus
    memory_version: int
    created_at: datetime
    updated_at: datetime
    profile_id: str | None = None
    profile_version: int = 1


@dataclass(frozen=True, slots=True)
class ProjectConversation:
    conversation_id: str
    project_id: str
    team_agent_id: str
    account_id: str
    status: ConversationStatus
    last_message_sequence: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProjectConversationMessage:
    conversation_id: str
    sequence: int
    role: str
    content: str
    turn_id: str | None
    run_id: str | None
    message_kind: ConversationMessageKind
    created_at: datetime
    attachment_resource_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectAgentTurn:
    turn_id: str
    conversation_id: str
    user_message_sequence: int
    assistant_message_sequence: int | None
    run_id: str | None
    trigger_kind: TurnTriggerKind
    status: TurnStatus
    idempotency_key: str
    created_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProjectWorkspaceSnapshot:
    project: Project
    teams: tuple[ProjectTeam, ...]
    conversation: ProjectConversation | None
    task_count: int
    resource_count: int
    pending_draft_count: int
    unread_activity_count: int


class ExchangeDraftStatus(str, Enum):
    DRAFTING = "drafting"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class ExchangeStatus(str, Enum):
    SENT = "sent"
    RESPONDED = "responded"
    CLOSED = "closed"


class ExchangeRecipientStatus(str, Enum):
    PENDING = "pending"
    DRAFTING = "drafting"
    RESPONDED = "responded"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class AgentExchangeDraft:
    draft_id: str
    project_id: str
    source_team_id: str
    source_conversation_id: str | None
    source_turn_id: str | None
    purpose: str
    summary: str
    request: str
    constraints: str
    shared_resource_ids: tuple[str, ...]
    recipient_team_ids: tuple[str, ...]
    content_sha256: str
    status: ExchangeDraftStatus
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    rejection_reason: str


@dataclass(frozen=True, slots=True)
class AgentExchange:
    exchange_id: str
    project_id: str
    source_team_id: str
    source_conversation_id: str | None
    source_turn_id: str | None
    purpose: str
    summary: str
    request: str
    constraints: str
    content_sha256: str
    status: ExchangeStatus
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AgentExchangeRecipient:
    exchange_id: str
    recipient_team_id: str
    context_snapshot: dict
    status: ExchangeRecipientStatus
    response_id: str | None
    responded_at: datetime | None
    created_at: datetime
    draft_content: str | None = None
    draft_turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentExchangeResponse:
    response_id: str
    exchange_id: str
    recipient_team_id: str
    content: str
    content_sha256: str
    approved_by: str
    approved_at: datetime
    turn_id: str | None
    created_at: datetime


class PlanDraftStatus(str, Enum):
    DRAFTING = "drafting"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ProjectPlanDraft:
    draft_id: str
    project_id: str
    source_conversation_id: str | None
    source_turn_id: str | None
    source_run_id: str | None
    schema_version: str
    plan_payload: dict
    goals: str
    scope: str
    phases: tuple[dict, ...]
    milestones: tuple[dict, ...]
    risks: tuple[dict, ...]
    dependencies: tuple[dict, ...]
    acceptance_criteria: tuple[str, ...]
    content_sha256: str
    status: PlanDraftStatus
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    rejection_reason: str


@dataclass(frozen=True, slots=True)
class ProjectTeamRequirementDraft:
    requirement_id: str
    project_id: str
    plan_draft_id: str | None
    team_category: str
    team_count: int
    rationale: str
    status: PlanDraftStatus
    created_by: str
    created_at: datetime
    approved_at: datetime | None
