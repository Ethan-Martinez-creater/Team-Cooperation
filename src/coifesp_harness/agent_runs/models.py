from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class DurableRunStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_TOOL = "awaiting_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATES = frozenset(
    {
        DurableRunStatus.COMPLETED,
        DurableRunStatus.FAILED,
        DurableRunStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class DurableAgentRun:
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


@dataclass(frozen=True, slots=True)
class AgentRunLease:
    run: DurableAgentRun
    worker_id: str
    lease_token: str
    lease_expires_at: datetime
    checkpoint: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DurableAgentEvent:
    run_id: str
    sequence: int
    event_id: str
    event_type: str
    data: dict[str, Any]
    occurred_at: datetime
