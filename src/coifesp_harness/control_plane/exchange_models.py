from __future__ import annotations

from pydantic import BaseModel, Field

from ..product.models import (
    AgentExchange,
    AgentExchangeDraft,
    AgentExchangeRecipient,
    AgentExchangeResponse,
    ExchangeDraftStatus,
    ExchangeRecipientStatus,
    ExchangeStatus,
)


class AgentExchangeDraftView(BaseModel):
    draft_id: str
    project_id: str
    source_team_id: str
    source_conversation_id: str | None
    source_turn_id: str | None
    purpose: str
    summary: str
    request: str
    constraints: str
    shared_resource_ids: list[str]
    recipient_team_ids: list[str]
    content_sha256: str
    status: ExchangeDraftStatus
    version: int
    created_by: str
    created_at: str
    updated_at: str
    approved_at: str | None
    rejection_reason: str

    @classmethod
    def from_draft(cls, value: AgentExchangeDraft) -> "AgentExchangeDraftView":
        return cls(
            draft_id=value.draft_id,
            project_id=value.project_id,
            source_team_id=value.source_team_id,
            source_conversation_id=value.source_conversation_id,
            source_turn_id=value.source_turn_id,
            purpose=value.purpose,
            summary=value.summary,
            request=value.request,
            constraints=value.constraints,
            shared_resource_ids=list(value.shared_resource_ids),
            recipient_team_ids=list(value.recipient_team_ids),
            content_sha256=value.content_sha256,
            status=value.status,
            version=value.version,
            created_by=value.created_by,
            created_at=value.created_at.isoformat(),
            updated_at=value.updated_at.isoformat(),
            approved_at=value.approved_at.isoformat() if value.approved_at else None,
            rejection_reason=value.rejection_reason,
        )


class AgentExchangeView(BaseModel):
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
    approved_at: str | None
    created_at: str

    @classmethod
    def from_exchange(cls, value: AgentExchange) -> "AgentExchangeView":
        return cls(
            exchange_id=value.exchange_id,
            project_id=value.project_id,
            source_team_id=value.source_team_id,
            source_conversation_id=value.source_conversation_id,
            source_turn_id=value.source_turn_id,
            purpose=value.purpose,
            summary=value.summary,
            request=value.request,
            constraints=value.constraints,
            content_sha256=value.content_sha256,
            status=value.status,
            approved_by=value.approved_by,
            approved_at=value.approved_at.isoformat() if value.approved_at else None,
            created_at=value.created_at.isoformat(),
        )


class AgentExchangeRecipientView(BaseModel):
    exchange_id: str
    recipient_team_id: str
    context_snapshot: dict
    status: ExchangeRecipientStatus
    response_id: str | None
    responded_at: str | None
    created_at: str
    draft_content: str | None = None

    @classmethod
    def from_recipient(
        cls, value: AgentExchangeRecipient, *, draft_content: str | None = None
    ) -> "AgentExchangeRecipientView":
        return cls(
            exchange_id=value.exchange_id,
            recipient_team_id=value.recipient_team_id,
            context_snapshot=value.context_snapshot,
            status=value.status,
            response_id=value.response_id,
            responded_at=value.responded_at.isoformat() if value.responded_at else None,
            created_at=value.created_at.isoformat(),
            draft_content=draft_content if draft_content is not None else value.draft_content,
        )


class AgentExchangeResponseView(BaseModel):
    response_id: str
    exchange_id: str
    recipient_team_id: str
    content: str
    content_sha256: str
    approved_by: str
    approved_at: str
    turn_id: str | None
    created_at: str

    @classmethod
    def from_response(cls, value: AgentExchangeResponse) -> "AgentExchangeResponseView":
        return cls(
            response_id=value.response_id,
            exchange_id=value.exchange_id,
            recipient_team_id=value.recipient_team_id,
            content=value.content,
            content_sha256=value.content_sha256,
            approved_by=value.approved_by,
            approved_at=value.approved_at.isoformat(),
            turn_id=value.turn_id,
            created_at=value.created_at.isoformat(),
        )


class AgentExchangeDraftBody(BaseModel):
    purpose: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=50_000)
    request: str = Field(min_length=1, max_length=50_000)
    constraints: str = Field(default="", max_length=10_000)
    shared_resource_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    recipient_team_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    source_conversation_id: str | None = Field(default=None, max_length=128)
    source_turn_id: str | None = Field(default=None, max_length=128)


class AgentExchangeDraftUpdateBody(BaseModel):
    expected_version: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=50_000)
    request: str = Field(min_length=1, max_length=50_000)
    constraints: str = Field(default="", max_length=10_000)
    shared_resource_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    recipient_team_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=32)


class AgentExchangeDraftApproveBody(BaseModel):
    expected_version: int = Field(ge=1)


class AgentExchangeDraftRejectBody(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=10_000)


class AgentExchangeResponseBody(BaseModel):
    content: str = Field(default="", max_length=50_000)
    turn_id: str | None = Field(default=None, max_length=128)


class AgentExchangeGenerateBody(BaseModel):
    recipient_team_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    shared_resource_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    source_conversation_id: str | None = Field(default=None, max_length=128)


class ExchangeGenerateResult(BaseModel):
    turn_id: str
    message: str
