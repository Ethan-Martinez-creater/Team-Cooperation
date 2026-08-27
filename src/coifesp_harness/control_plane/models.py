from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..memory import MemoryKind, MemoryScope, MemoryStatus
from ..artifacts import ArtifactKind


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProblemDetails(StrictModel):
    type: str
    title: str
    status: int
    detail: str
    request_id: str
    errors: list[dict[str, str]] | None = None


class HealthResponse(StrictModel):
    status: str


class IdentityResponse(StrictModel):
    principal_id: str
    tenant_id: str
    roles: list[str]
    clearance: str
    compartments: list[str]
    expires_at: datetime
    token_id: str | None = Field(default=None)


class ClassificationName(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class MemoryCreateBody(StrictModel):
    memory_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    scope: MemoryScope
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=100_000)
    classification: ClassificationName
    compartments: list[str] = Field(default_factory=list, max_length=64)
    project_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    expires_at: datetime | None = None

    @field_validator("compartments")
    @classmethod
    def unique_compartments(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("compartments must be unique")
        if any(
            not item
            or len(item) > 128
            or not item[0].isalnum()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789._:@/-"
                for character in item
            )
            for item in value
        ):
            raise ValueError("a compartment identifier is invalid")
        return value


class MemoryWriteResponse(StrictModel):
    memory_id: str
    status: MemoryStatus
    version: int
    admission_reason: str
    duplicate: bool


class MemoryViewResponse(StrictModel):
    memory_id: str
    scope: MemoryScope
    kind: MemoryKind
    content: str
    classification: ClassificationName
    compartments: list[str]
    source_type: str
    trust_level: str
    status: MemoryStatus
    created_at: datetime
    expires_at: datetime | None
    version: int


class MemoryReviewBody(StrictModel):
    expected_version: int = Field(ge=1)
    approve: bool
    reason: str = Field(min_length=1, max_length=2_000)


class MemoryReviewResponse(StrictModel):
    memory_id: str
    status: MemoryStatus


class MemorySearchResponse(StrictModel):
    memory: MemoryViewResponse
    lexical_score: float
    semantic_score: float | None
    combined_score: float


class MemoryDeletionRequestBody(StrictModel):
    request_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)


class MemoryDeletionDecisionBody(StrictModel):
    approve: bool
    reason: str = Field(min_length=1, max_length=2_000)


class MemoryDeletionResponse(StrictModel):
    request_id: str
    memory_id: str
    status: str


class MemoryLegalHoldBody(StrictModel):
    hold_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reason: str = Field(min_length=1, max_length=2_000)


class MemoryLegalHoldReleaseBody(StrictModel):
    reason: str = Field(min_length=1, max_length=2_000)


class CheckpointToolCallBody(StrictModel):
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict


class CheckpointMessageBody(StrictModel):
    role: str = Field(pattern=r"^(system|user|assistant|tool)$")
    content: str = Field(max_length=100_000)
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=128)
    tool_calls: list[CheckpointToolCallBody] = Field(default_factory=list, max_length=256)


class SemanticSummaryBody(StrictModel):
    objective: str = Field(min_length=1, max_length=4_000)
    constraints: list[str] = Field(default_factory=list, max_length=256)
    decisions: list[str] = Field(default_factory=list, max_length=256)
    open_items: list[str] = Field(default_factory=list, max_length=256)
    verified_facts: list[str] = Field(default_factory=list, max_length=256)


class SemanticCheckpointCreateBody(StrictModel):
    checkpoint_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    conversation_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    messages: list[CheckpointMessageBody] = Field(min_length=1, max_length=10_000)
    summary: SemanticSummaryBody
    classification: ClassificationName
    compartments: list[str] = Field(default_factory=list, max_length=64)


class SemanticCheckpointReviewBody(StrictModel):
    expected_version: int = Field(ge=1)
    approve: bool
    reason: str = Field(min_length=1, max_length=2_000)


class SemanticCheckpointResponse(StrictModel):
    checkpoint_id: str
    conversation_id: str
    status: str
    version: int
    source_digest: str
    summary: SemanticSummaryBody


class SemanticCheckpointResumeResponse(StrictModel):
    role: str
    content: str


class ArtifactPublishBody(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    kind: ArtifactKind
    media_type: str = Field(min_length=1, max_length=256)
    content_uri: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    classification: ClassificationName
    compartments: list[str] = Field(default_factory=list, max_length=64)
    source_tool: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    created_at: datetime
    visible_to_tenants: list[str] = Field(min_length=1, max_length=128)


class ArtifactUploadMetadata(StrictModel):
    artifact_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    kind: ArtifactKind
    classification: ClassificationName
    compartments: list[str] = Field(default_factory=list, max_length=64)
    visible_to_tenants: list[str] = Field(min_length=1, max_length=128)


class ArtifactView(StrictModel):
    owner_tenant_id: str
    artifact_id: str
    kind: ArtifactKind
    media_type: str
    content_uri: str
    sha256: str
    size_bytes: int
    classification: ClassificationName
    compartments: list[str]
    producer_principal_id: str
    source_tool: str
    source_version: str
    created_at: datetime
    visible_to_tenants: list[str]


class ConnectorProposalBody(StrictModel):
    connector_id: str = Field(min_length=1, max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    base_url: str = Field(min_length=1, max_length=2048)
    token_endpoint: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret_env: str = Field(pattern=r"^COIFESP_CONNECTOR_[A-Z0-9_]{1,80}_CLIENT_SECRET$")
    scopes: list[str] = Field(min_length=1, max_length=32)
    allowed_paths: list[str] = Field(min_length=1, max_length=128)
    max_classification: ClassificationName
    timeout_seconds: float = Field(default=15, ge=0.1, le=120)
    max_response_bytes: int = Field(default=1_048_576, ge=1024, le=16*1024*1024)
    max_attempts: int = Field(default=3, ge=1, le=5)
    circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    circuit_cooldown_seconds: float = Field(default=30, ge=1, le=3600)


class ConnectorDecisionBody(StrictModel):
    approve: bool
    reason: str = Field(min_length=1, max_length=2_000)


class ConnectorStateView(StrictModel):
    connector_id: str
    status: str
    version: int


class ConnectorAvailableView(StrictModel):
    connector_id: str
    auth_type: str
    status: str
    proposed_action: str
    version: int
    created_at: datetime
    reviewed_at: datetime | None


class ConnectorRevisionView(StrictModel):
    connector_id: str
    version: int
    proposed_action: str
    status: str
    base_url: str
    token_endpoint: str
    client_id: str
    client_secret_env: str
    scopes: list[str]
    allowed_paths: list[str]
    max_classification: int
    timeout_millis: int
    max_response_bytes: int
    max_attempts: int
    circuit_failure_threshold: int
    circuit_cooldown_millis: int
    config_digest: str
    created_by: str
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
