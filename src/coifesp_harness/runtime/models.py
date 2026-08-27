from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol

from ..security.models import Classification, Principal

if TYPE_CHECKING:
    from ..context.models import ContextBudget, ContextItem


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    provider_request_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    cost_microusd: int = 0

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cost_microusd) < 0:
            raise ValueError("model response usage cannot be negative")


class ModelCapability(str, Enum):
    TOOL_CALLING = "tool_calling"
    STREAMING = "streaming"
    JSON_OUTPUT = "json_output"
    VISION = "vision"


@dataclass(frozen=True, slots=True)
class ModelRoutePolicy:
    data_classification: Classification = Classification.INTERNAL
    required_capabilities: frozenset[ModelCapability] = frozenset()
    allowed_provider_ids: frozenset[str] = frozenset()
    residency_regions: frozenset[str] = frozenset()
    allow_external_egress: bool = False
    max_call_cost_microusd: int | None = None
    max_output_tokens: int | None = None
    max_call_total_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("model call cost ceiling", self.max_call_cost_microusd),
            ("model output token ceiling", self.max_output_tokens),
            ("model call token ceiling", self.max_call_total_tokens),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        for values in (self.allowed_provider_ids, self.residency_regions):
            if any(not value or len(value) > 128 for value in values):
                raise ValueError("model route policy identifiers are invalid")


class ModelStreamEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    event_type: ModelStreamEventType
    sequence: int
    text_delta: str | None = None
    response: LLMResponse | None = None
    provider_id: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("model stream sequence must be positive")
        if self.event_type is ModelStreamEventType.TEXT_DELTA:
            if not self.text_delta or self.response is not None:
                raise ValueError("model text delta event is invalid")
        elif self.event_type is ModelStreamEventType.COMPLETED:
            if self.response is None or self.text_delta is not None:
                raise ValueError("model completion stream event is invalid")


class ModelProvider(Protocol):
    async def complete(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        correlation_id: str,
        max_output_tokens: int | None = None,
    ) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class ControlDirective:
    sequence: int
    command_id: str
    command_type: str
    content: str


class RuntimeControlSource(Protocol):
    async def poll(self, *, after_sequence: int) -> tuple[ControlDirective, ...]: ...


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_turns: int = 20
    max_tool_calls: int = 50
    max_total_tokens: int = 100_000
    max_model_cost_microusd: int = 10_000_000

    def __post_init__(self) -> None:
        if min(
            self.max_turns,
            self.max_tool_calls,
            self.max_total_tokens,
            self.max_model_cost_microusd,
        ) <= 0:
            raise ValueError("run budgets must be positive")


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    call_id: str
    approval_id: str

    def __post_init__(self) -> None:
        if not self.call_id or not self.approval_id:
            raise ValueError("approval binding identifiers are required")


@dataclass(frozen=True, slots=True)
class RunUsage:
    turns: int
    tool_calls: int
    total_tokens: int
    model_cost_microusd: int = 0

    def __post_init__(self) -> None:
        if min(self.turns, self.tool_calls, self.total_tokens, self.model_cost_microusd) < 0:
            raise ValueError("run usage counters cannot be negative")


@dataclass(frozen=True, slots=True)
class AuthorizedTool:
    tool_id: str
    version: str
    schema_digest: str


@dataclass(frozen=True, slots=True)
class AuthorizedSkill:
    name: str
    version: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class ToolAuthorization:
    """Immutable per-run tool and skill authorization snapshot."""

    tools: tuple[AuthorizedTool, ...] = ()
    skills: tuple[AuthorizedSkill, ...] = ()
    catalog_digest: str | None = None

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(item.tool_id for item in self.tools)

    @property
    def skill_bindings(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.name, item.version) for item in self.skills)


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    run_id: str
    correlation_id: str
    principal: Principal
    messages: tuple[Message, ...]
    budget: RunBudget = field(default_factory=RunBudget)
    context_items: tuple[ContextItem, ...] = ()
    context_budget: ContextBudget | None = None
    context_purpose: str = "agent.run"
    approval_bindings: tuple[ApprovalBinding, ...] = ()
    resume_usage: RunUsage | None = None
    control_cursor: int = 0
    control_source: RuntimeControlSource | None = None
    model_route_policy: ModelRoutePolicy = field(default_factory=ModelRoutePolicy)
    tool_authorization: ToolAuthorization | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.correlation_id:
            raise ValueError("run_id and correlation_id are required")
        if not self.context_purpose.strip():
            raise ValueError("context_purpose is required")
        if len({item.call_id for item in self.approval_bindings}) != len(self.approval_bindings):
            raise ValueError("approval binding call IDs must be unique")
        if self.approval_bindings and self.resume_usage is None:
            raise ValueError("approval resume requires cumulative run usage")
        if self.control_cursor < 0:
            raise ValueError("control cursor cannot be negative")
        if self.tool_authorization is not None:
            tools = self.tool_authorization.tools
            if len({item.tool_id for item in tools}) != len(tools):
                raise ValueError("authorized tools contain duplicates")
            skills = self.tool_authorization.skills
            if len({item.name for item in skills}) != len(skills):
                raise ValueError("authorized skills reference a skill twice")
        if any(
            item.label.classification > self.model_route_policy.data_classification
            for item in self.context_items
        ):
            raise ValueError(
                "model route data classification cannot be lower than context data"
            )


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_type: str
    run_id: str
    sequence: int
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    run_id: str
    status: str
    messages: tuple[Message, ...]
    turns: int
    tool_calls: int
    total_tokens: int
    model_cost_microusd: int
    events: tuple[AgentEvent, ...]
    control_cursor: int = 0
    applied_control_sequences: tuple[int, ...] = ()


EventCallback = Protocol
EventStream = AsyncIterator[AgentEvent]
