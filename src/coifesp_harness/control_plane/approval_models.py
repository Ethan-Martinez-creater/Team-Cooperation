from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from ..approvals import ApprovalStatus
from .models import StrictModel


class ApprovalCreateBody(StrictModel):
    approval_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    tool_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=8_192)
    expires_in_seconds: int = Field(default=900, ge=60, le=86_400)
    classification: str = Field(
        default="internal",
        pattern=r"^(public|internal|confidential|restricted)$",
    )
    compartments: list[str] = Field(default_factory=list, max_length=64)


class ApprovalDecisionBody(StrictModel):
    approve: bool
    expected_version: int = Field(ge=1)


class ApprovalRevokeBody(StrictModel):
    expected_version: int = Field(ge=1)


class ApprovalResponse(StrictModel):
    approval_id: str
    tenant_id: str
    requester_id: str
    tool_name: str
    request_digest: str
    reason_digest: str
    origin: str
    review_projection: dict[str, Any] | None
    projection_digest: str | None
    classification: str
    compartments: list[str]
    status: ApprovalStatus
    required_approver_role: str
    expires_at: datetime
    created_at: datetime
    version: int
    decided_by: str | None
    decided_at: datetime | None
    consumed_by_execution_id: str | None
    consumed_at: datetime | None
