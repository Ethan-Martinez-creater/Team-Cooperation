from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProjectProcessPhase(StrEnum):
    INTAKE = "INTAKE"
    ANALYSIS = "ANALYSIS"
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    INTEGRATION = "INTEGRATION"
    VERIFICATION = "VERIFICATION"
    DELIVERY = "DELIVERY"
    TERMINAL = "TERMINAL"


class ProjectProcessStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProjectProcessWaitReason(StrEnum):
    NONE = "NONE"
    HUMAN_INPUT = "HUMAN_INPUT"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    TEAM_RESPONSE = "TEAM_RESPONSE"
    AGENT_RUN = "AGENT_RUN"
    TOOL_JOB = "TOOL_JOB"
    DEPENDENCY = "DEPENDENCY"
    VERIFICATION = "VERIFICATION"
    SCHEDULE = "SCHEDULE"


TERMINAL_STATUSES = frozenset(
    {
        ProjectProcessStatus.COMPLETED,
        ProjectProcessStatus.FAILED,
        ProjectProcessStatus.CANCELLED,
    }
)

BLOCKED_REASON_PRIORITY = (
    ProjectProcessWaitReason.TEAM_RESPONSE,
    ProjectProcessWaitReason.DEPENDENCY,
    ProjectProcessWaitReason.VERIFICATION,
)


@dataclass(frozen=True, slots=True)
class ProjectProcess:
    process_id: str
    project_id: str
    phase: ProjectProcessPhase
    status: ProjectProcessStatus
    wait_reason: ProjectProcessWaitReason
    version: int
    root_goal_id: str | None
    active_plan_id: str | None
    execution_policy_id: str
    execution_policy_version: int
    started_by: str
    started_at: datetime
    updated_at: datetime
    last_event_sequence: int
    last_orchestration_sequence: int
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProjectProcessEvent:
    event_id: str
    process_id: str
    project_id: str
    sequence: int
    event_type: str
    idempotency_key: str
    transition_key: str | None
    schema_version: str
    subject_type: str
    subject_id: str
    source_aggregate_version: int | None
    process_version_before: int
    process_version_after: int | None
    initiated_by: str
    executed_as: str
    correlation_id: str
    causation_id: str | None
    payload: dict
    payload_sha256: str
    occurred_at: datetime


def validate_process_state(
    phase: ProjectProcessPhase,
    status: ProjectProcessStatus,
    wait_reason: ProjectProcessWaitReason,
) -> None:
    if status in {ProjectProcessStatus.WAITING, ProjectProcessStatus.BLOCKED}:
        if wait_reason is ProjectProcessWaitReason.NONE:
            raise ValueError("waiting or blocked process requires a wait reason")
    elif wait_reason is not ProjectProcessWaitReason.NONE:
        raise ValueError("ready, running and terminal process cannot have a wait reason")
    if phase is ProjectProcessPhase.TERMINAL:
        if status not in TERMINAL_STATUSES:
            raise ValueError("terminal phase requires a terminal status")
    elif status in TERMINAL_STATUSES:
        raise ValueError("terminal status requires the terminal phase")


def select_blocked_wait_reason(reasons) -> ProjectProcessWaitReason:
    available = {ProjectProcessWaitReason(item) for item in reasons}
    for candidate in BLOCKED_REASON_PRIORITY:
        if candidate in available:
            return candidate
    raise ValueError("no supported blocked wait reason was provided")
