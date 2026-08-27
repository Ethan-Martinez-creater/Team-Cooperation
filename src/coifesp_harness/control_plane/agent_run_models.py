from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from ..agent_runs import DurableRunStatus
from ..product import InboxAgentMode, ProjectAgentMode
from .models import StrictModel


class RepositoryContextSelection(StrictModel):
    repository_id: str = Field(min_length=1, max_length=128)
    commit: str = Field(min_length=40, max_length=40, pattern=r"[0-9a-fA-F]{40}")
    paths: tuple[str, ...] = Field(min_length=1, max_length=32)

    ("paths")

    def unique_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not path or len(path) > 512 for path in value):
            raise ValueError("repository paths are invalid")
        return value


class DocumentContextSelection(StrictModel):
    resource_id: str = Field(min_length=1, max_length=128)
    derivative_id: str = Field(min_length=1, max_length=128)
    item_indexes: tuple[int, ...] = Field(min_length=1, max_length=128)

    ("item_indexes")

    def unique_item_indexes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if tuple(sorted(set(value))) != value or any(index < 0 for index in value):
            raise ValueError("document item indexes must be unique, nonnegative, and ordered")
        return value


class AgentRunCreateBody(StrictModel):
    run_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    correlation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    checkpoint: dict[str, Any]
    max_failures: int = Field(default=3, ge=1, le=20)
    project_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    project_resource_ids: tuple[str, ...] = Field(default=(), max_length=64)
    repository_context: tuple[RepositoryContextSelection, ...] = Field(default=(), max_length=8)
    document_context: tuple[DocumentContextSelection, ...] = Field(default=(), max_length=16)
    include_project_brief: bool = False
    project_agent_mode: ProjectAgentMode | None = None
    inbox_agent_mode: InboxAgentMode | None = None
    tool_ids: tuple[str, ...] = Field(default=(), max_length=32)
    skill_refs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("project_resource_ids")
    @classmethod
    def unique_project_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item or len(item) > 128 for item in value):
            raise ValueError("project resource identifiers are invalid")
        return value

    @field_validator("tool_ids", "skill_refs")
    @classmethod
    def unique_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item or len(item) > 160 for item in value):
            raise ValueError("tool/skill references are invalid")
        return value

    @model_validator(mode="after")
    def valid_project_context(self):
        project_context_selected = bool(
            self.project_resource_ids
            or self.include_project_brief
            or self.repository_context
            or self.document_context
        )
        if self.project_id is not None and not project_context_selected:
            raise ValueError("project_id requires project context")
        if self.project_id is None and project_context_selected:
            raise ValueError("project Agent context requires project_id")
        if (self.project_id is None) != (self.project_agent_mode is None):
            raise ValueError("project Agent mode and project_id must be provided together")
        if self.inbox_agent_mode is not None and self.project_id is not None:
            raise ValueError("collaboration inbox and project Agent modes are mutually exclusive")
        return self


class AgentRunResponse(StrictModel):
    run_id: str
    tenant_id: str
    owner_principal_id: str
    correlation_id: str
    status: DurableRunStatus
    version: int
    turns: int
    tool_calls: int
    total_tokens: int
    model_cost_microusd: int
    pending_call_id: str | None
    pending_approval_id: str | None
    failure_count: int
    max_failures: int
    next_attempt_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class AgentConversationMessage(StrictModel):
    sequence: int = Field(ge=1)
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str
    name: str | None = None


class AgentConversationResponse(StrictModel):
    run_id: str
    run_version: int = Field(ge=1)
    messages: tuple[AgentConversationMessage, ...]


class AgentControlSubmitBody(StrictModel):
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    command_type: str = Field(pattern=r"^(steer|follow_up)$")
    content: str = Field(min_length=1, max_length=65_536)
    expected_run_version: int = Field(ge=1)


class AgentControlCommandResponse(StrictModel):
    run_id: str
    sequence: int
    command_id: str
    command_type: str
    status: str
    submitted_by: str
    created_at: datetime
    applied_at: datetime | None
    applied_run_version: int | None
    rejected_at: datetime | None
    rejection_code: str | None
    run: AgentRunResponse


class AgentRunResumeBody(StrictModel):
    approval_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    expected_version: int = Field(ge=1)


class AgentRunLeaseBody(StrictModel):
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class AgentRunLeaseTokenBody(StrictModel):
    lease_token: str = Field(min_length=32, max_length=256)


class AgentRunHeartbeatBody(AgentRunLeaseTokenBody):
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class AgentRunHeartbeatResponse(StrictModel):
    lease_expires_at: datetime


class AgentRunLeaseResponse(StrictModel):
    run: AgentRunResponse
    lease_token: str
    lease_expires_at: datetime
    checkpoint: dict[str, Any]


class AgentRunCheckpointBody(AgentRunLeaseTokenBody):
    target: DurableRunStatus
    checkpoint: dict[str, Any]
    turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    model_cost_microusd: int = Field(default=0, ge=0)
    pending_call_id: str | None = Field(default=None, max_length=128)
    pending_approval_id: str | None = Field(default=None, max_length=128)
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    applied_control_sequences: tuple[int, ...] = Field(default=(), max_length=100)

    @field_validator("applied_control_sequences")
    @classmethod
    def validate_control_sequences(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(sequence <= 0 for sequence in value) or tuple(sorted(set(value))) != value:
            raise ValueError("applied control sequences must be unique, positive, and ordered")
        return value


class AgentRunRetryBody(AgentRunLeaseTokenBody):
    error_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    delay_seconds: int = Field(ge=0, le=3600)


class AgentRunAbortBody(AgentRunLeaseTokenBody):
    error_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
