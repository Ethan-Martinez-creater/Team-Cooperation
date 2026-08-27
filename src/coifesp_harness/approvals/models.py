from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..security.models import Classification


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    tenant_id: str
    requester_id: str
    tool_name: str
    request_digest: str
    reason_digest: str
    origin: str
    review_projection: dict[str, Any] | None
    projection_digest: str | None
    classification: Classification
    compartments: frozenset[str]
    status: ApprovalStatus
    required_approver_role: str
    expires_at: datetime
    created_at: datetime
    version: int
    decided_by: str | None = None
    decided_at: datetime | None = None
    consumed_by_execution_id: str | None = None
    consumed_at: datetime | None = None
