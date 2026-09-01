from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from ..execution import TaskStatus
from .models import StrictModel

Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class ExecutionEnqueueBody(StrictModel):
    task_id: Identifier
    program_id: Identifier
    assignment_id: Identifier
    queue: Identifier
    payload: dict[str, Any]
    dependencies: list[Identifier] = Field(default_factory=list, max_length=256)
    priority: int = Field(default=0, ge=-1000, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=100)


class ProjectWorkExecutionEnqueueBody(StrictModel):
    process_id: Identifier
    team_task_id: Identifier
    work_node_id: Identifier
    contract_version: int = Field(ge=1)


class ExecutionTaskResponse(StrictModel):
    task_id: str
    tenant_id: str
    program_id: str | None
    assignment_id: str | None
    project_id: str | None
    process_id: str | None
    team_task_id: str | None
    work_node_id: str | None
    contract_version: int | None
    queue: str
    status: TaskStatus
    priority: int
    max_attempts: int
    attempt_count: int
    available_at: datetime
    dependencies: list[str]
    cancel_requested: bool
    result: dict[str, Any] | None
    error_code: str | None


class ExecutionLeaseBody(StrictModel):
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class ExecutionLeaseResponse(StrictModel):
    task: ExecutionTaskResponse
    lease_token: str
    lease_expires_at: datetime


class LeaseTokenBody(StrictModel):
    lease_token: str = Field(min_length=32, max_length=256)


class HeartbeatBody(LeaseTokenBody):
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class ExecutionSuccessBody(LeaseTokenBody):
    result: dict[str, Any]


class ExecutionFailureBody(LeaseTokenBody):
    error_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    retryable: bool
    retry_delay_seconds: int = Field(default=30, ge=0, le=86_400)


class ExecutionStatusResponse(StrictModel):
    task_id: str
    status: TaskStatus
    lease_expires_at: datetime | None = None
