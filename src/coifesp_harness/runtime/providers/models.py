from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ...errors import HarnessError
from ...security import Classification
from ..models import ModelCapability, ModelProvider
from .tokenizers import ProviderTokenCounter

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProviderFailureKind(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    QUOTA = "quota"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    CAPACITY = "capacity"
    SERVER = "server"
    CONTENT_POLICY = "content_policy"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


_RETRYABLE_FAILURES = frozenset(
    {
        ProviderFailureKind.RATE_LIMIT,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.CONNECTION,
        ProviderFailureKind.CAPACITY,
        ProviderFailureKind.SERVER,
    }
)


class ProviderInvocationError(HarnessError):
    """A normalized provider failure that never exposes remote response bodies."""

    def __init__(
        self,
        *,
        kind: ProviderFailureKind,
        provider_id: str,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"model provider invocation failed ({kind.value})")
        self.kind = kind
        self.provider_id = provider_id
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE_FAILURES


class ModelRoutingError(HarnessError):
    """No registered provider can safely and economically serve the request."""


class ModelCostBudgetExceeded(ModelRoutingError):
    """Otherwise eligible routes exceed the remaining durable cost budget."""


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    model: str
    capabilities: frozenset[ModelCapability]
    max_data_classification: Classification
    region: str
    external: bool
    context_window_tokens: int
    max_output_tokens: int
    input_microusd_per_million_tokens: int
    output_microusd_per_million_tokens: int
    priority: int = 100
    max_concurrency: int = 32

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.provider_id) or not self.model or len(self.model) > 256:
            raise ValueError("provider descriptor identity is invalid")
        if not _IDENTIFIER.fullmatch(self.region):
            raise ValueError("provider region is invalid")
        if (
            self.context_window_tokens <= 0
            or self.max_output_tokens <= 0
            or self.max_output_tokens > self.context_window_tokens
            or min(
                self.input_microusd_per_million_tokens,
                self.output_microusd_per_million_tokens,
            )
            < 0
            or not 0 <= self.priority <= 10_000
            or not 1 <= self.max_concurrency <= 100_000
        ):
            raise ValueError("provider descriptor limits are invalid")

    def estimate_cost_microusd(self, *, input_tokens: int, output_tokens: int) -> int:
        if min(input_tokens, output_tokens) < 0:
            raise ValueError("token estimates cannot be negative")
        numerator = (
            input_tokens * self.input_microusd_per_million_tokens
            + output_tokens * self.output_microusd_per_million_tokens
        )
        return (numerator + 999_999) // 1_000_000


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    descriptor: ProviderDescriptor
    provider: ModelProvider
    token_counter: ProviderTokenCounter | None = None


class ModelGatewayObserver:
    def record_model_attempt(
        self,
        *,
        provider_id: str,
        outcome: str,
        failure_kind: str | None,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
    ) -> None: ...
