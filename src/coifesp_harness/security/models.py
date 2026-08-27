from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, IntEnum
from typing import FrozenSet


class Classification(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


class Action(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE_TOOL = "execute_tool"
    SHARE = "share"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    A2A_SEND = "a2a_send"


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: str
    tenant_id: str
    roles: FrozenSet[str] = field(default_factory=frozenset)
    clearance: Classification = Classification.INTERNAL
    compartments: FrozenSet[str] = field(default_factory=frozenset)
    is_service: bool = False

    def __post_init__(self) -> None:
        if not self.principal_id or not self.tenant_id:
            raise ValueError("principal_id and tenant_id are required")


@dataclass(frozen=True, slots=True)
class ResourceLabel:
    owner_tenant_id: str
    classification: Classification
    compartments: FrozenSet[str] = field(default_factory=frozenset)
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if not self.owner_tenant_id:
            raise ValueError("owner_tenant_id is required")


@dataclass(frozen=True, slots=True)
class DisclosureGrant:
    grant_id: str
    owner_tenant_id: str
    recipient_tenant_id: str
    resource_id: str
    purpose: str
    approved_by: str
    expires_at: datetime
    max_classification: Classification
    compartments: FrozenSet[str] = field(default_factory=frozenset)

    def is_valid_for(
        self,
        *,
        resource: ResourceLabel,
        recipient_tenant_id: str,
        purpose: str,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(UTC)
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return (
            bool(self.approved_by)
            and current < expiry
            and self.owner_tenant_id == resource.owner_tenant_id
            and self.recipient_tenant_id == recipient_tenant_id
            and self.resource_id == resource.resource_id
            and self.purpose == purpose
            and resource.classification <= self.max_classification
            and resource.compartments.issubset(self.compartments)
        )
