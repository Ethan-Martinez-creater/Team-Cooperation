from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..security import Classification


@dataclass(frozen=True, slots=True)
class TeamCapability:
    capability_id: str
    provider_tenant_id: str
    version: str
    name: str
    description: str
    tags: tuple[str, ...]
    protocols: tuple[str, ...]
    input_contract: str
    output_contract: str
    max_input_classification: Classification
    required_compartments: tuple[str, ...]
    residency_regions: tuple[str, ...]
    visible_to_tenants: tuple[str, ...]
    content_digest: str
    published_by: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class CapabilityPublishResult:
    capability: TeamCapability
    duplicate: bool


@dataclass(frozen=True, slots=True)
class CapabilityCapacity:
    provider_tenant_id: str
    capability_id: str
    version: str
    status: str
    available_slots: int
    valid_until: datetime
    state_version: int


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    capability: TeamCapability
    capacity: CapabilityCapacity
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapacityReservation:
    reservation_id: str
    provider_tenant_id: str
    capability_id: str
    version: str
    consumer_tenant_id: str
    slots: int
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CapacityNegotiation:
    negotiation_id: str
    provider_tenant_id: str
    consumer_tenant_id: str
    capability_id: str
    version: str
    requested_slots: int
    earliest_start: datetime
    latest_end: datetime
    status: str
    state_version: int
