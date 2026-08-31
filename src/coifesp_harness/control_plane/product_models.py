from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field

from ..product import (
    AccountRegistrationStatus,
    ContactRequestStatus,
    DataPropagation,
    InboxAgentMode,
    NotificationCategory,
    ProjectAgentMode,
    ProjectTeamKind,
    ProjectTopicStatus,
    TeamAccountRole,
    TeamRelationshipState,
    TeamTaskStatus,
    TaskPriority,
    TaskScheduleProposalStatus,
)
from .models import StrictModel


class AccountRegistrationBody(StrictModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=1024)
    team_id: str = Field(min_length=1, max_length=128)


class AccountRegistrationView(StrictModel):
    account_id: str
    username: str
    display_name: str
    email: str
    team_id: str
    status: AccountRegistrationStatus
    created_at: datetime


class AccountRegistrationDecisionBody(StrictModel):
    accept: bool


class TeamRegistrationBody(StrictModel):
    handle: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=128)


class TeamView(StrictModel):
    team_id: str
    handle: str
    name: str
    created_at: datetime


class TeamDirectoryEntryView(StrictModel):
    team: TeamView
    relationship: TeamRelationshipState


class TeamDirectoryPageView(StrictModel):
    items: list[TeamDirectoryEntryView]
    next_after_handle: str | None


class TeamRegistrationView(StrictModel):
    team: TeamView
    administrator_username: str
    administrator_initial_password: str


class SessionCreateBody(StrictModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class AccountView(StrictModel):
    account_id: str
    username: str
    display_name: str
    email: str
    team_id: str
    team_role: TeamAccountRole
    registration_status: AccountRegistrationStatus
    must_change_password: bool
    created_at: datetime


class InitialPasswordChangeBody(StrictModel):
    login: str = Field(min_length=1, max_length=320)
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class SessionView(StrictModel):
    access_token: str
    token_type: str = "Bearer"
    expires_at: datetime
    account: AccountView


class TeamRelationCreateBody(StrictModel):
    recipient_team_handle: str = Field(min_length=3, max_length=64)
    message: str = Field(default="", max_length=1000)


class TeamRelationDecisionBody(StrictModel):
    accept: bool


class TeamRelationView(StrictModel):
    request_id: str
    sender_team_id: str
    recipient_team_id: str
    requested_by: str
    message: str
    status: ContactRequestStatus
    created_at: datetime
    decided_at: datetime | None


class TeamRelationSummaryView(StrictModel):
    team: TeamView
    created_at: datetime


class ProjectCreateBody(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=10_000)
    owner_assignment_name: str = Field(min_length=1, max_length=128)
    owner_kind: ProjectTeamKind
    initial_brief: str | None = Field(default=None, max_length=50_000)


class ProjectView(StrictModel):
    project_id: str
    name: str
    description: str
    owner_team_id: str
    created_by: str
    created_at: datetime


class ProjectCreateResultView(ProjectView):
    conversation_id: str | None = None


class ProjectTeamAddBody(StrictModel):
    team_id: str = Field(min_length=1, max_length=128)
    assignment_name: str = Field(min_length=1, max_length=128)
    kind: ProjectTeamKind


class ProjectTeamView(StrictModel):
    team_id: str
    project_id: str
    name: str
    kind: ProjectTeamKind
    assigned_by: str


class ProjectDetailView(StrictModel):
    project: ProjectView
    teams: list[ProjectTeamView]


class ProjectResourceCreateBody(StrictModel):
    title: str = Field(min_length=1, max_length=256)
    propagation: DataPropagation
    artifact_id: str = Field(min_length=1, max_length=128)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectResourceView(StrictModel):
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


class ProjectResourceShareBody(StrictModel):
    recipient_team_id: str = Field(min_length=1, max_length=128)


class ProjectMessageCreateBody(StrictModel):
    target_team_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=20_000)


class ProjectMessageView(StrictModel):
    message_id: str
    project_id: str
    source_team_id: str
    target_team_id: str
    created_by: str
    content: str
    created_at: datetime


class TeamTaskCreateBody(StrictModel):
    target_team_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=20_000)
    acceptance_criteria: str = Field(min_length=1, max_length=20_000)
    priority: TaskPriority = TaskPriority.NORMAL
    due_at: datetime | None = None


class TeamTaskDecisionBody(StrictModel):
    accept: bool
    expected_contract_version: int | None = Field(default=None, ge=1, strict=True)


class TeamTaskContractBody(StrictModel):
    expected_version: int = Field(ge=0, strict=True)
    process_id: str = Field(min_length=1, max_length=128)
    work_node_id: str = Field(min_length=1, max_length=128)
    requested_capability: dict
    input_manifest: dict
    output_contract: dict
    verification_policy: dict
    autonomy_requirement: str = Field(min_length=1, max_length=32)


class TeamTaskAssignBody(StrictModel):
    account_id: str = Field(min_length=1, max_length=128)


class TeamTaskSubmitBody(StrictModel):
    resource_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class TeamTaskReviewBody(StrictModel):
    accept: bool
    note: str = Field(default="", max_length=20_000)


class TeamTaskView(StrictModel):
    task_id: str
    project_id: str
    source_team_id: str
    target_team_id: str
    created_by: str | None
    produced_by_principal_id: str | None = None
    source_planner_run_id: str | None = None
    source_planner_command_id: str | None = None
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
    schedule_version: int = Field(default=1, ge=1)
    due_changed_at: datetime | None = None
    due_changed_by: str | None = None
    completed_at: datetime | None = None
    is_overdue: bool = False
    is_due_soon: bool = False
    due_in_seconds: int | None = None


class TaskScheduleChangeBody(StrictModel):
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    clear_due_at: bool = False
    expected_schedule_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2_000)


class TaskScheduleProposalView(StrictModel):
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
    version: int = Field(ge=1)
    schedule_version: int = Field(ge=1)
    created_at: datetime
    decided_at: datetime | None


class TaskScheduleChangeResultView(StrictModel):
    result: Literal["updated", "proposed"]
    task: TeamTaskView | None = None
    proposal: TaskScheduleProposalView | None = None


class TaskScheduleProposalDecisionBody(StrictModel):
    accept: bool
    reason: str = Field(default="", max_length=2_000)
    expected_proposal_version: int = Field(ge=1)


class ProjectActivityView(StrictModel):
    sequence: int
    project_id: str
    actor_account_id: str
    actor_team_id: str
    event_type: str
    subject_id: str
    target_team_id: str | None
    summary: str
    created_at: datetime


class AgentProjectBriefView(StrictModel):
    project_id: str
    brief: str


class ProjectNotificationView(StrictModel):
    project_id: str
    project_name: str
    unread_count: int = Field(ge=0)
    latest_sequence: int = Field(ge=0)
    last_read_sequence: int = Field(ge=0)


class CollaborationInboxTaskView(StrictModel):
    task_id: str
    project_id: str
    source_team_id: str
    target_team_id: str
    title: str
    description: str
    acceptance_criteria: str
    status: TeamTaskStatus
    assigned_to_me: bool
    updated_at: datetime
    priority: TaskPriority = TaskPriority.NORMAL
    due_at: datetime | None = None
    is_overdue: bool = False
    is_due_soon: bool = False
    due_in_seconds: int | None = None
    schedule_version: int = Field(default=1, ge=1)


class CollaborationInboxActionView(StrictModel):
    project_id: str
    project_name: str
    action: Literal["respond", "assign", "start", "submit", "review"]
    task: CollaborationInboxTaskView


class CollaborationInboxActivityView(StrictModel):
    project_id: str
    project_name: str
    sequence: int = Field(ge=1)
    actor_team_id: str
    event_type: str
    subject_id: str
    target_team_id: str | None
    summary: str
    created_at: datetime


class CollaborationInboxView(StrictModel):
    action_count: int = Field(ge=0)
    unread_count: int = Field(ge=0)
    actions: list[CollaborationInboxActionView]
    unread_activities: list[CollaborationInboxActivityView]


class InboxAgentRunView(StrictModel):
    run_id: str
    team_id: str
    created_by: str
    mode: InboxAgentMode
    created_at: datetime


class ProjectTopicCreateBody(StrictModel):
    title: str = Field(min_length=1, max_length=256)
    context: str = Field(min_length=1, max_length=50_000)
    source_agent_run_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )


class ProjectTopicView(StrictModel):
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


class ProjectTopicContributionBody(StrictModel):
    content: str = Field(min_length=1, max_length=20_000)


class ProjectTopicContributionView(StrictModel):
    contribution_id: str
    topic_id: str
    project_id: str
    team_id: str
    created_by: str
    content: str
    created_at: datetime


class ProjectTopicDecisionBody(StrictModel):
    decision: str = Field(min_length=1, max_length=50_000)


class CollaborationDraftView(StrictModel):
    draft_id: str
    project_id: str
    source_agent_run_id: str
    source_message_sequence: int = Field(ge=1)
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_index: int = Field(ge=0)
    kind: str
    payload: dict
    status: str
    version: int = Field(ge=1)
    created_by: str
    team_id: str
    executed_subject_id: str | None
    rejection_reason: str
    created_at: datetime
    updated_at: datetime


class CollaborationDraftUpdateBody(StrictModel):
    expected_version: int = Field(ge=1)
    payload: dict


class CollaborationDraftDecisionBody(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2_000)


class ProjectAgentRunView(StrictModel):
    project_id: str
    run_id: str
    team_id: str
    created_by: str
    mode: ProjectAgentMode
    created_at: datetime


class NotificationPreferenceView(StrictModel):
    notify_tasks: bool = True
    notify_messages: bool = True
    notify_resources: bool = True
    notify_topics: bool = True
    notify_agent_events: bool = True
    notify_due_soon: bool = True
    notify_overdue: bool = True
    due_soon_hours: int = Field(default=48, ge=1, le=336)
    time_zone: str = Field(default="UTC", min_length=1, max_length=64)
    quiet_start_minute: int | None = Field(default=None, ge=0, le=1439)
    quiet_end_minute: int | None = Field(default=None, ge=0, le=1439)
    updated_at: datetime | None = None
    quiet_now: bool = False


class NotificationView(StrictModel):
    notification_id: str
    account_id: str
    project_id: str
    activity_sequence: int = Field(ge=0)
    category: NotificationCategory
    title: str
    summary: str
    subject_id: str
    read_at: datetime | None
    archived_at: datetime | None
    created_at: datetime


class NotificationPageView(StrictModel):
    items: list[NotificationView]
    unread_count: int = Field(ge=0)
    next_cursor: str | None = None


class NotificationIdsBody(StrictModel):
    notification_ids: tuple[str, ...] = Field(min_length=1, max_length=200)


class ProjectRepositoryView(StrictModel):
    project_id: str
    repository_id: str
    connector_id: str
    connector_version: int = Field(ge=1)
    remote_repository_id: str
    default_branch: str
    available_operations: list[str]
    created_by: str
    created_at: datetime


class DocumentVersionView(StrictModel):
    version_id: str
    resource_id: str
    version_number: int = Field(ge=1)
    artifact_id: str
    artifact_sha256: str
    parent_version_id: str | None = None
    created_by: str
    reason: str
    created_at: datetime


class DocumentDerivativeView(StrictModel):
    derivative_id: str
    resource_id: str
    version_id: str
    derivative_type: str
    status: str
    summary: str
    size_bytes: int = Field(ge=0)
    error_category: str | None = None
    created_at: datetime


class DocumentDerivativeContent(StrictModel):
    media_type: str
    title: str
    items: list[dict]
    error_category: str | None = None


class DocumentDraftView(StrictModel):
    draft_id: str
    resource_id: str
    source_version_id: str
    modification_json: str
    generated_version_id: str | None = None
    status: str
    version: int = Field(ge=1)
    created_by: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None


class DocumentDraftCreateBody(StrictModel):
    source_version_id: str
    modification: dict
    reason: str


class DocumentDraftDecideBody(StrictModel):
    approve: bool
    expected_version: int = Field(ge=1)
