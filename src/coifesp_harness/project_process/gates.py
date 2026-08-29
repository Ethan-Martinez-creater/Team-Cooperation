from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProjectInputRequestStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ProjectGateStatus(StrEnum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class DeliveryGateDecision(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class ProjectGateType(StrEnum):
    BUDGET = "BUDGET"
    DELIVERY = "DELIVERY"


PROJECT_GATE_DECISIONS = {
    ProjectGateType.BUDGET: ("INCREASE_BUDGET", "REDUCE_SCOPE", "TERMINATE"),
    ProjectGateType.DELIVERY: ("ACCEPT", "REJECT"),
}


@dataclass(frozen=True, slots=True)
class ProjectInputRequest:
    request_id: str
    process_id: str
    project_id: str
    work_node_id: str | None
    requested_by_run_id: str | None
    requested_by_agent_id: str
    question: str
    input_schema: dict
    context_projection: dict
    status: ProjectInputRequestStatus
    response: dict | None
    version: int
    created_by: str
    created_at: datetime
    answered_by: str | None
    answered_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProjectGate:
    gate_id: str
    process_id: str
    project_id: str
    gate_type: str
    subject_type: str
    subject_id: str
    required_roles: tuple[str, ...]
    allowed_decisions: tuple[str, ...]
    status: ProjectGateStatus
    decision: str | None
    reason: str
    version: int
    created_by: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
