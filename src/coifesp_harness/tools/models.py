from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Awaitable, Callable, FrozenSet

from jsonschema import Draft202012Validator

from ..security.models import Principal, ResourceLabel, RiskLevel
from .approval_review import ApprovalReviewPolicy

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
ArgumentValidator = Callable[[dict[str, Any]], None]


class ExecutionStatus(str, Enum):
    DISPATCH_REQUIRED = "dispatch_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    handler: ToolHandler
    parameters_schema: dict[str, Any]
    required_roles: FrozenSet[str] = field(default_factory=frozenset)
    risk: RiskLevel = RiskLevel.LOW
    timeout_seconds: float = 30.0
    max_output_chars: int = 50_000
    validator: ArgumentValidator | None = None
    approval_review: ApprovalReviewPolicy | None = None
    executor: str = "tool_worker"

    def __post_init__(self) -> None:
        if not self.name or self.timeout_seconds <= 0 or self.max_output_chars <= 0:
            raise ValueError("tool name, positive timeout, and output limit are required")
        if self.executor not in ("tool_worker", "agent_worker"):
            raise ValueError("tool executor must be tool_worker or agent_worker")
        Draft202012Validator.check_schema(self.parameters_schema)
        if self.parameters_schema.get("type") != "object":
            raise ValueError("tool parameters schema must describe a JSON object")


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    tenant_id: str
    principal_id: str
    tool_name: str
    request_digest: str
    approved_by: str
    expires_at: datetime

    def is_valid(self, *, tenant_id: str, principal_id: str, tool_name: str, digest: str) -> bool:
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return (
            bool(self.approved_by)
            and datetime.now(UTC) < expiry
            and self.tenant_id == tenant_id
            and self.principal_id == principal_id
            and self.tool_name == tool_name
            and self.request_digest == digest
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    execution_id: str
    idempotency_key: str
    correlation_id: str
    principal: Principal
    tool_name: str
    arguments: dict[str, Any]
    input_label: ResourceLabel | None = None
    approval: Approval | None = None
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    execution_id: str
    status: ExecutionStatus
    output: Any = None
    error: str | None = None
    audit_event_id: str | None = None
    request_digest: str | None = None
    approval_id: str | None = None
