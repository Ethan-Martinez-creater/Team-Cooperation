from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProjectProcessCommandStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    STALE = "STALE"


class ProjectProcessCommandType(StrEnum):
    PROPOSE_TASK = "propose_task"
    PROPOSE_DEPENDENCY = "propose_dependency"
    PROPOSE_RISK = "propose_risk"
    PROPOSE_DECISION = "propose_decision"
    REQUEST_REWORK = "request_rework"
    REQUEST_REPLAN = "request_replan"
    REQUEST_HUMAN_INPUT = "request_human_input"
    REQUEST_HUMAN_GATE = "request_human_gate"


class ProjectProcessOutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ProjectProcessCommand:
    command_id: str
    process_id: str
    project_id: str
    decision_id: str
    command_type: ProjectProcessCommandType
    request_digest: str
    based_on_process_version: int
    based_on_event_sequence: int
    graph_snapshot_digest: str
    status: ProjectProcessCommandStatus
    result_subject_id: str | None
    created_at: datetime
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProjectProcessOutboxEntry:
    outbox_id: str
    event_id: str
    process_id: str
    project_id: str
    status: ProjectProcessOutboxStatus
    attempt_count: int
    available_at: datetime
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    published_at: datetime | None
    last_error: str | None
