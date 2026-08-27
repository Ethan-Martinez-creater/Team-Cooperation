from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

from .models import ClassificationName


class CapabilityPublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    version: str = Field(min_length=5, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=32)
    protocols: list[str] = Field(min_length=1, max_length=3)
    input_contract: str = Field(min_length=1, max_length=1024)
    output_contract: str = Field(min_length=1, max_length=1024)
    max_input_classification: ClassificationName
    required_compartments: list[str] = Field(default_factory=list, max_length=32)
    residency_regions: list[str] = Field(default_factory=list, max_length=16)
    visible_to_tenants: list[str] = Field(min_length=1, max_length=128)


class CapabilityView(BaseModel):
    capability_id: str
    provider_tenant_id: str
    version: str
    name: str
    description: str
    tags: list[str]
    protocols: list[str]
    input_contract: str
    output_contract: str
    max_input_classification: str
    required_compartments: list[str]
    residency_regions: list[str]
    content_digest: str
    published_at: str


class CapabilityPublishResponse(BaseModel):
    capability: CapabilityView
    duplicate: bool


class CapabilityCapacityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(available|limited|unavailable)$")
    available_slots: int = Field(ge=0, le=1_000_000)
    valid_until: datetime
    expected_version: int | None = Field(default=None, ge=1)


class CapabilityCapacityView(BaseModel):
    status: str
    available_slots: int
    valid_until: datetime
    state_version: int


class CapabilityMatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_tags: list[str] = Field(default_factory=list, max_length=32)
    protocol: str = Field(min_length=1, max_length=64)
    input_classification: ClassificationName
    compartments: list[str] = Field(default_factory=list, max_length=32)
    residency_regions: list[str] = Field(default_factory=list, max_length=16)
    limit: int = Field(default=20, ge=1, le=100)


class CapabilityMatchView(BaseModel):
    capability: CapabilityView
    capacity: CapabilityCapacityView
    score: int
    reasons: list[str]


class CapacityReservationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reservation_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    provider_tenant_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    slots: int = Field(ge=1, le=10_000)
    expires_at: datetime


class CapacityReservationView(BaseModel):
    reservation_id: str
    provider_tenant_id: str
    capability_id: str
    version: str
    consumer_tenant_id: str
    slots: int
    status: str
    expires_at: datetime


class CapacityNegotiationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    negotiation_id: str = Field(min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    provider_tenant_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    requested_slots: int = Field(ge=1, le=10_000)
    earliest_start: datetime
    latest_end: datetime
    reason: str = Field(min_length=1, max_length=2_000)


class CapacityNegotiationDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    decision: str = Field(pattern=r"^(accepted|rejected|withdrawn)$")
    reason: str = Field(min_length=1, max_length=2_000)


class CapacityNegotiationView(BaseModel):
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
