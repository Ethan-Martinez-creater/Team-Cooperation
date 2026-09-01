from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..product.models import (
    ConversationMessageKind,
    ConversationStatus,
    ProjectAgentTurn,
    ProjectConversation,
    ProjectConversationMessage,
    TurnStatus,
    TurnTriggerKind,
)
from .product_models import ProjectTeamView, ProjectView


class ProjectConversationView(BaseModel):
    conversation_id: str
    project_id: str
    team_agent_id: str
    account_id: str
    status: ConversationStatus
    last_message_sequence: int
    created_at: str
    updated_at: str
    archived_at: str | None = None

    @classmethod
    def from_conversation(cls, value: ProjectConversation) -> "ProjectConversationView":
        return cls(
            conversation_id=value.conversation_id,
            project_id=value.project_id,
            team_agent_id=value.team_agent_id,
            account_id=value.account_id,
            status=value.status,
            last_message_sequence=value.last_message_sequence,
            created_at=value.created_at.isoformat(),
            updated_at=value.updated_at.isoformat(),
            archived_at=value.archived_at.isoformat() if value.archived_at else None,
        )


class ProjectConversationMessageView(BaseModel):
    conversation_id: str
    sequence: int
    role: str
    content: str
    turn_id: str | None
    run_id: str | None
    message_kind: ConversationMessageKind
    attachment_resource_ids: tuple[str, ...] = ()
    created_at: str

    @classmethod
    def from_message(
        cls, value: ProjectConversationMessage
    ) -> "ProjectConversationMessageView":
        return cls(
            conversation_id=value.conversation_id,
            sequence=value.sequence,
            role=value.role,
            content=value.content,
            turn_id=value.turn_id,
            run_id=value.run_id,
            message_kind=value.message_kind,
            attachment_resource_ids=value.attachment_resource_ids,
            created_at=value.created_at.isoformat(),
        )


class ProjectAgentTurnView(BaseModel):
    turn_id: str
    conversation_id: str
    user_message_sequence: int
    assistant_message_sequence: int | None
    run_id: str | None
    trigger_kind: TurnTriggerKind
    status: TurnStatus
    idempotency_key: str
    created_at: str
    completed_at: str | None = None

    @classmethod
    def from_turn(cls, value: ProjectAgentTurn) -> "ProjectAgentTurnView":
        return cls(
            turn_id=value.turn_id,
            conversation_id=value.conversation_id,
            user_message_sequence=value.user_message_sequence,
            assistant_message_sequence=value.assistant_message_sequence,
            run_id=value.run_id,
            trigger_kind=value.trigger_kind,
            status=value.status,
            idempotency_key=value.idempotency_key,
            created_at=value.created_at.isoformat(),
            completed_at=value.completed_at.isoformat() if value.completed_at else None,
        )


class WorkspaceProjectView(BaseModel):
    project: ProjectView
    conversation_id: str | None
    pending_count: int = 0


class WorkspaceView(BaseModel):
    project: ProjectView
    teams: list[ProjectTeamView]
    conversation: ProjectConversationView | None
    task_count: int
    resource_count: int
    pending_draft_count: int
    unread_activity_count: int


class HarnessProcessView(BaseModel):
    phase: str
    phase_label: str
    status: str
    wait_reason: str
    semantic_status: str
    next_step: str
    version: int
    updated_at: str | None


class HarnessWorkNodeView(BaseModel):
    node_id: str
    type: str
    label: str
    status: str | None = None
    team_name: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class HarnessWorkEdgeView(BaseModel):
    source: str
    target: str
    type: str


class HarnessWorkGraphView(BaseModel):
    nodes: list[HarnessWorkNodeView]
    edges: list[HarnessWorkEdgeView]


class HarnessTaskView(BaseModel):
    task_id: str
    title: str
    status: str
    team_id: str
    team_name: str | None = None
    priority: str
    due_at: str | None = None
    contract_ready: bool
    contract_version: int | None = None


class HarnessActivityView(BaseModel):
    category: str
    label: str
    status: str
    occurred_at: str | None


class HarnessBlockerView(BaseModel):
    kind: str
    label: str
    created_at: str | None


class HarnessVerificationView(BaseModel):
    total: int
    pending: int
    passed: int
    failed: int
    stale: int


class HarnessCompletionView(BaseModel):
    tasks_done: int
    tasks_total: int
    contract_status: str | None = None
    delivery_status: str | None = None
    evaluation_passed: bool | None = None
    updated_at: str | None = None


class ProjectHarnessView(BaseModel):
    process: HarnessProcessView | None
    work_graph: HarnessWorkGraphView
    tasks: list[HarnessTaskView]
    activity: list[HarnessActivityView]
    blockers: list[HarnessBlockerView]
    verification: HarnessVerificationView
    completion: HarnessCompletionView


class MessageSendBody(BaseModel):
    content: str = Field(default="", max_length=50_000)
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    attachment_resource_ids: tuple[str, ...] = Field(
        default_factory=tuple, max_length=32
    )
    expected_last_sequence: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _require_content_or_attachment(self) -> "MessageSendBody":
        if not self.content.strip() and not self.attachment_resource_ids:
            raise ValueError("message content or at least one attachment is required")
        return self


class MessageSendResult(BaseModel):
    message: ProjectConversationMessageView
    turn: ProjectAgentTurnView
    run: dict | None = None


class MessagePageView(BaseModel):
    items: list[ProjectConversationMessageView]
    conversation: ProjectConversationView


ConversationPropagation = Literal["team_private", "project_readonly", "portable"]


class ResourcePropagationBody(BaseModel):
    propagation: ConversationPropagation
    expected_propagation: ConversationPropagation
