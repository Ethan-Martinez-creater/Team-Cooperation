from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class ExecutionTask:
    task_id: str
    tenant_id: str
    queue: str
    payload: dict[str, Any]
    request_digest: str
    status: TaskStatus
    priority: int
    max_attempts: int
    attempt_count: int
    available_at: datetime
    dependencies: tuple[str, ...]
    created_by: str
    program_id: str | None = None
    assignment_id: str | None = None
    project_id: str | None = None
    process_id: str | None = None
    team_task_id: str | None = None
    work_node_id: str | None = None
    contract_version: int | None = None
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TaskLease:
    task: ExecutionTask
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
