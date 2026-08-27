from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class AgentControlType(str, Enum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"


class AgentControlStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AgentControlCommand:
    run_id: str
    sequence: int
    command_id: str
    command_type: AgentControlType
    status: AgentControlStatus
    content: str
    submitted_by: str
    created_at: datetime
    applied_at: datetime | None = None
    applied_run_version: int | None = None
    rejected_at: datetime | None = None
    rejection_code: str | None = None
