"""Project-scoped adapter for the shared capability and capacity directory.

The generic capability service intentionally serves human callers and therefore
rejects service principals. The Project Orchestrator needs a much narrower
integration boundary: it may select and reserve a capability only for two
teams that already participate in the same Product project. This module is
that boundary. It does not create a second capability registry or infer
requirements from prose.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

from ..audit import AuditEvent
from ..capabilities.models import (
    CapabilityCapacity,
    CapacityReservation,
    TeamCapability,
)
from ..capabilities.repository import (
    CAPABILITY_CAPACITY,
    CAPACITY_RESERVATIONS,
    SQLAlchemyCapabilityRepository,
)
from ..errors import GovernanceConflictError, PolicyDenied
from ..product.repository import PROJECT_TEAMS
from ..security import Classification, Principal

SERVICE_PRINCIPAL_ID = "service:project-orchestrator"
PROJECT_ORCHESTRATOR_ROLE = "project_orchestrator"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_RESERVATION_DAYS = 30


@dataclass(frozen=True, slots=True)
class ProjectCapabilityRequirement:
    """Structured capability request supplied by the orchestrator.

    ``tags``, ``protocol``, ``input_classification``, ``compartments`` and
    ``residency`` are contract values. This object has no title or free-form
    description field: callers must resolve natural-language planning output
    before entering this adapter.
    """

    project_id: str
    consumer_team_id: str
    target_team_id: str
    tags: tuple[str, ...]
    protocol: str
    input_classification: Classification
    compartments: tuple[str, ...]
    residency: tuple[str, ...]
    slots: int

    @property
    def required_tags(self) -> tuple[str, ...]:
        return self.tags

    @property
    def residency_regions(self) -> tuple[str, ...]:
        return self.residency


@dataclass(frozen=True, slots=True)
class ProjectCapabilityMatch:
    """A capability candidate together with its fresh capacity snapshot."""

    capability: TeamCapability
    capacity: CapabilityCapacity
    score: int
    reasons: tuple[str, ...]
    requirement: ProjectCapabilityRequirement | None = None

    @property
    def provider_tenant_id(self) -> str:
        return self.capability.provider_tenant_id

    @property
    def capability_id(self) -> str:
        return self.capability.capability_id

    @property
    def version(self) -> str:
        return self.capability.version


CapabilityRequirement = ProjectCapabilityRequirement
CapabilityMatch = ProjectCapabilityMatch


def _default_clock() -> datetime:
    return datetime.now(UTC)


class ProjectCapabilityAdapter:
    """Narrow Project Orchestrator facade over the shared directory."""

    def __init__(
        self,
        repository: SQLAlchemyCapabilityRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        reservation_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        if reservation_ttl <= timedelta(0) or reservation_ttl > timedelta(days=_MAX_RESERVATION_DAYS):
            raise ValueError("reservation_ttl must be positive and at most 30 days")
        self.repository = repository
        self._clock = clock or _default_clock
        self._reservation_ttl = reservation_ttl

    def match(
        self,
        *,
        principal: Principal,
        requirement: ProjectCapabilityRequirement,
        limit: int = 20,
    ) -> tuple[ProjectCapabilityMatch, ...]:
        """Return deterministic candidates satisfying every explicit filter."""

        self._validate_requirement(requirement)
        self._validate_principal(principal, requirement)
        if not 1 <= limit <= 200:
            raise ValueError("match limit must be between 1 and 200")

        now = self._now()
        with self.repository.transaction(requirement.consumer_team_id) as connection:
            self._assert_project_participants(connection, requirement)
            capabilities = self.repository.list(connection, limit=200)
            capacities = self.repository.capacities(connection)
            capacity_visibility = self._capacity_visibility(connection)
            candidates = []
            for capability in capabilities:
                capacity = capacities.get(
                    (capability.provider_tenant_id, capability.capability_id, capability.version)
                )
                candidate = self._candidate(
                    requirement=requirement,
                    capability=capability,
                    capacity=capacity,
                    capacity_visible_to_consumer=(
                        requirement.consumer_team_id
                        in capacity_visibility.get(
                            (capability.provider_tenant_id, capability.capability_id, capability.version),
                            (),
                        )
                    ),
                    principal=principal,
                    now=now,
                )
                if candidate is not None:
                    candidates.append(candidate)

        return tuple(
            sorted(
                candidates,
                key=lambda value: (
                    -value.score,
                    value.capability.provider_tenant_id,
                    value.capability.capability_id,
                    value.capability.version,
                ),
            )[:limit]
        )

    def using_connection(self, connection: Connection) -> "ProjectCapabilityAdapter":
        return ProjectCapabilityAdapter(
            self.repository.using_connection(connection),
            clock=self._clock,
            reservation_ttl=self._reservation_ttl,
        )

    def release(
        self, *, principal: Principal, requirement: ProjectCapabilityRequirement,
        reservation_id: str,
    ) -> CapacityReservation:
        """Idempotently release this consumer's project reservation, even after expiry."""
        self._validate_requirement(requirement)
        self._validate_principal(principal, requirement)
        self._validate_identifier(reservation_id, "reservation_id")
        with self.repository.transaction(requirement.consumer_team_id) as connection:
            self._assert_project_participants(connection, requirement)
            row = connection.execute(
                select(CAPACITY_RESERVATIONS).where(and_(
                    CAPACITY_RESERVATIONS.c.provider_tenant_id == requirement.target_team_id,
                    CAPACITY_RESERVATIONS.c.reservation_id == reservation_id,
                )).with_for_update()
            ).mappings().one_or_none()
            if row is None or row["consumer_tenant_id"] != requirement.consumer_team_id:
                raise PolicyDenied("project capacity reservation is absent or belongs to another consumer")
            if row["created_by"] != SERVICE_PRINCIPAL_ID:
                raise PolicyDenied("only orchestrator-owned reservations may be released here")
            if row["status"] == "released":
                return self._reservation_from_row(row)
            return self.repository.release_reservation(
                connection, provider_tenant_id=requirement.target_team_id,
                reservation_id=reservation_id, actor_tenant_id=requirement.consumer_team_id,
                actor_id=principal.principal_id,
            )

    def reserve(
        self,
        *,
        principal: Principal,
        requirement: ProjectCapabilityRequirement,
        match: ProjectCapabilityMatch,
        reservation_id: str,
        expires_at: datetime | None = None,
    ) -> CapacityReservation:
        """Revalidate and reserve one matched capability atomically."""

        self._validate_requirement(requirement)
        self._validate_principal(principal, requirement)
        self._validate_identifier(reservation_id, "reservation_id")
        if not isinstance(match, ProjectCapabilityMatch):
            raise TypeError("match must be a ProjectCapabilityMatch")
        if match.capability.provider_tenant_id != requirement.target_team_id:
            raise PolicyDenied("capability provider must equal the explicit target team")

        now = self._now()
        with self.repository.transaction(requirement.consumer_team_id) as connection:
            self._assert_project_participants(connection, requirement)
            capability = self.repository.get(
                connection,
                provider_tenant_id=requirement.target_team_id,
                capability_id=match.capability.capability_id,
                version=match.capability.version,
            )
            if capability.content_digest != match.capability.content_digest:
                raise GovernanceConflictError("capability version changed after matching")

            existing = self._existing_reservation(
                connection,
                provider_tenant_id=requirement.target_team_id,
                reservation_id=reservation_id,
            )
            resolved_expiry = expires_at
            if resolved_expiry is None and existing is not None:
                resolved_expiry = self._aware(existing["expires_at"])
            if resolved_expiry is None:
                resolved_expiry = now + self._reservation_ttl
            self._validate_expiry(resolved_expiry, now, existing=existing)

            capacities = self.repository.capacities(connection)
            capacity = capacities.get(
                (capability.provider_tenant_id, capability.capability_id, capability.version)
            )
            capacity_visibility = self._capacity_visibility(connection)
            capacity_key = (capability.provider_tenant_id, capability.capability_id, capability.version)
            same_existing = self._reservation_matches(
                existing,
                capability=capability,
                requirement=requirement,
                expires_at=resolved_expiry,
            )
            fresh_match = self._candidate(
                requirement=requirement,
                capability=capability,
                capacity=capacity,
                capacity_visible_to_consumer=(
                    requirement.consumer_team_id in capacity_visibility.get(capacity_key, ())
                ),
                principal=principal,
                now=now,
                allow_existing=same_existing,
            )
            if fresh_match is None:
                raise GovernanceConflictError("capability match is stale or capacity is unavailable")
            if capacity is None or capacity.state_version != match.capacity.state_version:
                raise GovernanceConflictError("capability capacity changed after matching")

            try:
                with connection.begin_nested():
                    value, duplicate = self.repository.reserve(
                        connection,
                        reservation_id=reservation_id,
                        capability=capability,
                        consumer_tenant_id=requirement.consumer_team_id,
                        slots=requirement.slots,
                        expires_at=resolved_expiry,
                        actor_id=principal.principal_id,
                    )
            except SQLAlchemyIntegrityError as exc:
                raced = self._existing_reservation(
                    connection,
                    provider_tenant_id=requirement.target_team_id,
                    reservation_id=reservation_id,
                )
                if not self._reservation_matches(
                    raced,
                    capability=capability,
                    requirement=requirement,
                    expires_at=resolved_expiry,
                ):
                    raise GovernanceConflictError("reservation id conflicts with another request") from exc
                value = self._reservation_from_row(raced)
                duplicate = True

            if not duplicate:
                self.repository.audit_log.append_in_transaction(
                    connection,
                    AuditEvent(
                        tenant_id=requirement.consumer_team_id,
                        event_type="project.capacity.reserved",
                        actor_id=principal.principal_id,
                        outcome="reserved",
                        details={
                            "project_id": requirement.project_id,
                            "consumer_team_id": requirement.consumer_team_id,
                            "target_team_id": requirement.target_team_id,
                            "capability_id": capability.capability_id,
                            "version": capability.version,
                            "reservation_id": reservation_id,
                            "slots": requirement.slots,
                        },
                        correlation_id=reservation_id,
                    ),
                )
            return value

    def _candidate(
        self,
        *,
        requirement: ProjectCapabilityRequirement,
        capability: TeamCapability,
        capacity: CapabilityCapacity | None,
        capacity_visible_to_consumer: bool,
        principal: Principal,
        now: datetime,
        allow_existing: bool = False,
    ) -> ProjectCapabilityMatch | None:
        if capability.provider_tenant_id != requirement.target_team_id:
            return None
        if requirement.consumer_team_id not in capability.visible_to_tenants:
            return None
        if requirement.protocol not in capability.protocols:
            return None
        required_tags = set(requirement.tags)
        if not required_tags.issubset(capability.tags):
            return None
        if requirement.input_classification > principal.clearance:
            return None
        if requirement.input_classification > capability.max_input_classification:
            return None
        required_compartments = set(requirement.compartments)
        if not required_compartments.issubset(principal.compartments):
            return None
        if not set(capability.required_compartments).issubset(required_compartments):
            return None
        requested_regions = set(requirement.residency)
        if requested_regions and not requested_regions.intersection(capability.residency_regions):
            return None
        if capacity is None:
            return None
        if not capacity_visible_to_consumer:
            return None
        valid_until = self._aware(capacity.valid_until)
        if valid_until <= now:
            return None
        if capacity.status not in {"available", "limited"}:
            return None
        if capacity.available_slots < requirement.slots and not allow_existing:
            return None

        overlap = len(required_tags.intersection(capability.tags))
        surplus = capacity.available_slots - requirement.slots
        score = overlap * 100 + min(surplus, 20) + (10 if capacity.status == "available" else 0)
        reasons = (
            f"provider={capability.provider_tenant_id}",
            f"protocol={requirement.protocol}",
            f"tag_overlap={overlap}",
            f"classification={requirement.input_classification.name.lower()}",
            f"compartments={len(capability.required_compartments)}",
            f"residency_overlap={len(requested_regions.intersection(capability.residency_regions))}",
            f"available_slots={capacity.available_slots}",
            f"status={capacity.status}",
        )
        return ProjectCapabilityMatch(capability, capacity, score, reasons, requirement)

    @staticmethod
    def _assert_project_participants(
        connection: Connection,
        requirement: ProjectCapabilityRequirement,
    ) -> None:
        required = {requirement.consumer_team_id, requirement.target_team_id}
        rows = connection.execute(
            select(PROJECT_TEAMS.c.team_id).where(
                and_(
                    PROJECT_TEAMS.c.project_id == requirement.project_id,
                    PROJECT_TEAMS.c.team_id.in_(required),
                )
            )
        ).scalars().all()
        if set(rows) != required:
            raise PolicyDenied("consumer and target teams must participate in the project")

    @staticmethod
    def _capacity_visibility(connection: Connection) -> dict[tuple[str, str, str], tuple[str, ...]]:
        rows = connection.execute(
            select(
                CAPABILITY_CAPACITY.c.provider_tenant_id,
                CAPABILITY_CAPACITY.c.capability_id,
                CAPABILITY_CAPACITY.c.version,
                CAPABILITY_CAPACITY.c.visible_to_tenants,
            )
        ).mappings()
        return {
            (row["provider_tenant_id"], row["capability_id"], row["version"]): tuple(
                row["visible_to_tenants"]
            )
            for row in rows
        }

    @staticmethod
    def _validate_principal(
        principal: Principal,
        requirement: ProjectCapabilityRequirement,
    ) -> None:
        if (
            not principal.is_service
            or principal.principal_id != SERVICE_PRINCIPAL_ID
            or PROJECT_ORCHESTRATOR_ROLE not in principal.roles
            or principal.tenant_id != requirement.consumer_team_id
        ):
            raise PolicyDenied("only the consumer-scoped Project Orchestrator may use this adapter")
        if requirement.input_classification > principal.clearance:
            raise PolicyDenied("orchestrator clearance is below the requested input classification")
        if not set(requirement.compartments).issubset(principal.compartments):
            raise PolicyDenied("orchestrator compartments do not cover the request")

    @staticmethod
    def _validate_requirement(requirement: ProjectCapabilityRequirement) -> None:
        if not isinstance(requirement, ProjectCapabilityRequirement):
            raise TypeError("requirement must be a ProjectCapabilityRequirement")
        for value, name in (
            (requirement.project_id, "project_id"),
            (requirement.consumer_team_id, "consumer_team_id"),
            (requirement.target_team_id, "target_team_id"),
        ):
            ProjectCapabilityAdapter._validate_identifier(value, name)
        if not isinstance(requirement.protocol, str) or not requirement.protocol.strip():
            raise ValueError("protocol is required")
        if not isinstance(requirement.input_classification, Classification):
            raise TypeError("input_classification must be a Classification")
        if not 1 <= requirement.slots <= 10_000:
            raise ValueError("slots must be between 1 and 10000")
        for values, name, maximum in (
            (requirement.tags, "tags", 32),
            (requirement.compartments, "compartments", 32),
            (requirement.residency, "residency", 16),
        ):
            if not isinstance(values, tuple):
                raise TypeError(f"{name} must be a tuple of explicit tokens")
            normalized = set(values)
            if len(normalized) != len(values) or len(values) > maximum:
                raise ValueError(f"{name} contains too many or duplicate values")
            if any(not isinstance(value, str) or _TOKEN.fullmatch(value) is None for value in values):
                raise ValueError(f"{name} contains an invalid token")

    @staticmethod
    def _validate_identifier(value: str, name: str) -> None:
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError(f"{name} is invalid")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _validate_expiry(
        cls,
        expires_at: datetime,
        now: datetime,
        *,
        existing,
    ) -> None:
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("reservation expiry must carry an explicit timezone")
        expiry = expires_at.astimezone(UTC)
        existing_expiry = cls._aware(existing["expires_at"]) if existing is not None else None
        if existing_expiry is not None and expiry == existing_expiry:
            return
        if not now < expiry <= now + timedelta(days=_MAX_RESERVATION_DAYS):
            raise ValueError("reservation expiry is invalid")

    @staticmethod
    def _existing_reservation(connection: Connection, *, provider_tenant_id: str, reservation_id: str):
        return connection.execute(
            select(CAPACITY_RESERVATIONS).where(
                and_(
                    CAPACITY_RESERVATIONS.c.provider_tenant_id == provider_tenant_id,
                    CAPACITY_RESERVATIONS.c.reservation_id == reservation_id,
                )
            )
        ).mappings().one_or_none()

    @staticmethod
    def _reservation_matches(
        row,
        *,
        capability: TeamCapability,
        requirement: ProjectCapabilityRequirement,
        expires_at: datetime,
    ) -> bool:
        if row is None:
            return False
        return (
            row["capability_id"] == capability.capability_id
            and row["version"] == capability.version
            and row["consumer_tenant_id"] == requirement.consumer_team_id
            and int(row["slots"]) == requirement.slots
            and ProjectCapabilityAdapter._aware(row["expires_at"]) == expires_at.astimezone(UTC)
        )

    @staticmethod
    def _reservation_from_row(row) -> CapacityReservation:
        return CapacityReservation(
            reservation_id=row["reservation_id"],
            provider_tenant_id=row["provider_tenant_id"],
            capability_id=row["capability_id"],
            version=row["version"],
            consumer_tenant_id=row["consumer_tenant_id"],
            slots=int(row["slots"]),
            status=row["status"],
            expires_at=ProjectCapabilityAdapter._aware(row["expires_at"]),
        )


ProjectOrchestratorCapabilityAdapter = ProjectCapabilityAdapter

__all__ = [
    "PROJECT_ORCHESTRATOR_ROLE",
    "SERVICE_PRINCIPAL_ID",
    "CapabilityMatch",
    "CapabilityRequirement",
    "ProjectCapabilityAdapter",
    "ProjectCapabilityMatch",
    "ProjectCapabilityRequirement",
    "ProjectOrchestratorCapabilityAdapter",
]
