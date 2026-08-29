from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ProjectProcessCommandStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    STALE = "STALE"


class ProjectOrchestrationDecisionStatus(StrEnum):
    """Durable lifecycle of one complete orchestrator decision batch."""

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    STALE = "STALE"
    REJECTED = "REJECTED"


class ProjectProcessCommandType(StrEnum):
    PROPOSE_TASK = "propose_task"
    PROPOSE_DEPENDENCY = "propose_dependency"
    PROPOSE_RISK = "propose_risk"
    PROPOSE_DECISION = "propose_decision"
    REQUEST_REWORK = "request_rework"
    REQUEST_REPLAN = "request_replan"
    REQUEST_HUMAN_INPUT = "request_human_input"
    REQUEST_HUMAN_GATE = "request_human_gate"


class ProjectPlannerIntentStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PROJECTED = "PROJECTED"
    STALE = "STALE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ProjectPlannerIntent:
    planner_intent_id: str
    process_id: str
    project_id: str
    owner_team_id: str
    reason: str
    based_on_process_version: int
    based_on_event_sequence: int
    graph_snapshot_digest: str
    status: ProjectPlannerIntentStatus
    run_id: str | None
    decision_id: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    projected_at: datetime | None


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
    # The canonical payload is stored alongside its digest so a command can be
    # replayed without relying on the model output or an external event.
    request_json: dict = field(default_factory=dict)

    @property
    def request(self) -> dict:
        """Compatibility spelling used by command executors."""

        return self.request_json


@dataclass(frozen=True, slots=True)
class ProjectOrchestrationDecision:
    """Persisted snapshot guard and outcome for one command batch."""

    decision_id: str
    process_id: str
    project_id: str
    reason: str
    based_on_process_version: int
    based_on_event_sequence: int
    graph_snapshot_digest: str
    command_batch_digest: str
    decision_json: dict
    decision_digest: str
    status: ProjectOrchestrationDecisionStatus
    created_at: datetime
    applied_at: datetime | None

    @property
    def commands_digest(self) -> str:
        """Alias retained for callers that call the batch digest commands digest."""

        return self.command_batch_digest

    @property
    def batch_digest(self) -> str:
        return self.command_batch_digest


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


# Explicit aliases keep the domain vocabulary usable by the process runner
# while retaining the existing ProjectProcess* naming convention.
ProjectProcessDecisionStatus = ProjectOrchestrationDecisionStatus
ProjectProcessDecision = ProjectOrchestrationDecision
