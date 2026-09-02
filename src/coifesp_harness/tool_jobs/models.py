from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class ToolJobStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    AWAITING_SPECIALIST = "awaiting_specialist"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolJob:
    job_id: str
    tenant_id: str
    run_id: str
    call_id: str
    tool_name: str
    idempotency_key: str
    request_digest: str
    status: ToolJobStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    created_by: str
    arguments: dict[str, Any] | None = None
    result: Any = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolJobLease:
    job: ToolJob
    worker_id: str
    lease_token: str
    lease_expires_at: datetime
