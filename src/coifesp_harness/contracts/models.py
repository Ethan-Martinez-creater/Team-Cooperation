from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ContractKind(str, Enum):
    OPENAPI = "openapi"
    JSON_SCHEMA = "json_schema"
    PROTOBUF = "protobuf"
    ASYNCAPI = "asyncapi"
    DATA = "data"
    DOCUMENT = "document"
    GENERIC = "generic"


class Compatibility(str, Enum):
    COMPATIBLE = "compatible"
    BREAKING = "breaking"
    UNKNOWN = "unknown"


class ImpactSeverity(IntEnum):
    NOTICE = 0
    ACTION_REQUIRED = 1
    BLOCKING = 2


class ImpactState(str, Enum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    BLOCKED = "blocked"
    REMEDIATION_PROPOSED = "remediation_proposed"
    ACCEPTED = "accepted"


@dataclass(frozen=True, slots=True, order=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        match = _SEMVER.fullmatch(value)
        if match is None:
            raise ValueError("version must be strict semantic version without build metadata")
        return cls(int(match[1]), int(match[2]), int(match[3]), match[4])

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        return f"{value}-{self.prerelease}" if self.prerelease else value


@dataclass(frozen=True, slots=True)
class ContractRecord:
    contract_id: str
    program_id: str
    producer_tenant_id: str
    producer_assignment_id: str
    name: str
    kind: ContractKind
    visible_to_tenants: frozenset[str]
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContractRelease:
    contract_id: str
    version: str
    content_digest: str
    artifact_ref: str
    compatibility: Compatibility
    predecessor_version: str | None
    released_by: str
    released_at: datetime


@dataclass(frozen=True, slots=True)
class ContractDependency:
    dependency_id: str
    contract_id: str
    consumer_tenant_id: str
    consumer_assignment_id: str
    version_constraint: str
    baseline_version: str
    baseline_digest: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChangeImpact:
    impact_id: str
    dependency_id: str
    contract_id: str
    consumer_tenant_id: str
    consumer_assignment_id: str
    from_version: str
    to_version: str
    from_digest: str
    to_digest: str
    severity: ImpactSeverity
    compatibility: Compatibility
    state: ImpactState
    consumer_note: str | None
    remediation: str | None
    acknowledged_by: str | None
    accepted_by: str | None
    created_at: datetime
    updated_at: datetime


def validate_id(value: str, name: str) -> None:
    if _ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def validate_digest(value: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("lowercase SHA-256 digest is required")
