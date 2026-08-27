from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from ..audit import AuditEvent
from ..contracts import SemanticVersion
from ..errors import PolicyDenied
from ..security import Classification, Principal
from ..security.redaction import SecretRedactor
from .models import (CapabilityCapacity, CapabilityMatch, CapabilityPublishResult,
    CapacityNegotiation, CapacityReservation, TeamCapability)
from .repository import SQLAlchemyCapabilityRepository

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROTOCOLS = frozenset({"a2a-1.0", "mcp", "human"})


class CapabilityDirectoryService:
    def __init__(self, repository: SQLAlchemyCapabilityRepository) -> None:
        self.repository = repository
        self.redactor = SecretRedactor()

    def publish(self, *, principal: Principal, idempotency_key: str, capability_id: str,
                version: str, name: str, description: str, tags: tuple[str, ...],
                protocols: tuple[str, ...], input_contract: str, output_contract: str,
                max_input_classification: Classification,
                required_compartments: tuple[str, ...], residency_regions: tuple[str, ...],
                visible_to_tenants: tuple[str, ...]) -> CapabilityPublishResult:
        if principal.is_service or "capability_publisher" not in principal.roles:
            raise PolicyDenied("capability publication requires capability_publisher")
        self._identifier(capability_id, "capability_id")
        self._identifier(idempotency_key, "idempotency_key")
        SemanticVersion.parse(version)
        if not name.strip() or len(name) > 256 or not description.strip() or len(description) > 2000:
            raise ValueError("capability name or description is invalid")
        tags = self._tokens(tags, "tags", 32)
        protocols = self._tokens(protocols, "protocols", 3)
        if not protocols or not set(protocols).issubset(_PROTOCOLS):
            raise ValueError("capability protocol is not supported")
        compartments = self._tokens(required_compartments, "required_compartments", 32)
        regions = self._tokens(residency_regions, "residency_regions", 16)
        tenants = tuple(sorted(set(visible_to_tenants)))
        if principal.tenant_id not in tenants or len(tenants) > 128:
            raise ValueError("provider tenant must be included in capability visibility")
        for tenant in tenants:
            self._identifier(tenant, "visible tenant")
        for contract in (input_contract, output_contract):
            if not contract or len(contract) > 1024 or any(c.isspace() for c in contract):
                raise ValueError("capability contracts must be bounded opaque references")
        public = dict(
            capability_id=capability_id, provider_tenant_id=principal.tenant_id, version=version,
            name=name.strip(), description=description.strip(), tags=list(tags), protocols=list(protocols),
            input_contract=input_contract, output_contract=output_contract,
            max_input_classification=int(max_input_classification),
            required_compartments=list(compartments), residency_regions=list(regions),
            visible_to_tenants=list(tenants),
        )
        canonical = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if self.redactor.redact(canonical).findings:
            raise PolicyDenied("capability metadata appears to contain secret material")
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        request_digest = hashlib.sha256((canonical + "\n" + idempotency_key).encode()).hexdigest()
        now = datetime.now(UTC)
        with self.repository.transaction(principal.tenant_id) as connection:
            duplicate = self.repository.claim(connection, tenant_id=principal.tenant_id,
                key=idempotency_key, digest=request_digest, capability_id=capability_id, version=version)
            if duplicate:
                return CapabilityPublishResult(self.repository.get(connection,
                    provider_tenant_id=principal.tenant_id, capability_id=capability_id, version=version), True)
            values = {**public, "content_digest": digest, "published_by": principal.principal_id,
                      "published_at": now}
            capability = self.repository.insert(connection, values=values, event=AuditEvent(
                tenant_id=principal.tenant_id, event_type="capability.published",
                actor_id=principal.principal_id, outcome="published",
                details={"capability_id": capability_id, "version": version, "content_digest": digest},
                correlation_id=capability_id,
            ))
            return CapabilityPublishResult(capability, False)

    def discover(self, *, principal: Principal, protocol: str | None = None,
                 tag: str | None = None, limit: int = 100) -> tuple[TeamCapability, ...]:
        if principal.is_service or not 1 <= limit <= 200:
            raise PolicyDenied("capability discovery is not authorized")
        with self.repository.transaction(principal.tenant_id) as connection:
            values = self.repository.list(connection, protocol=protocol, tag=tag, limit=limit)
        return tuple(item for item in values if
            principal.tenant_id in item.visible_to_tenants and
            item.max_input_classification <= principal.clearance and
            set(item.required_compartments).issubset(principal.compartments))

    def declare_capacity(self, *, principal: Principal, provider_tenant_id: str,
                         capability_id: str, version: str, status: str,
                         available_slots: int, valid_until: datetime,
                         expected_version: int | None = None) -> CapabilityCapacity:
        if principal.is_service or "capability_publisher" not in principal.roles or principal.tenant_id != provider_tenant_id:
            raise PolicyDenied("capacity declaration requires provider capability_publisher")
        if status not in {"available", "limited", "unavailable"} or not 0 <= available_slots <= 1_000_000:
            raise ValueError("capacity declaration is invalid")
        now = datetime.now(UTC)
        if valid_until.tzinfo is None or not now < valid_until <= now + timedelta(days=366):
            raise ValueError("capacity validity is invalid")
        with self.repository.transaction(principal.tenant_id) as connection:
            capability = self.repository.get(connection, provider_tenant_id=provider_tenant_id,
                capability_id=capability_id, version=version)
            value = self.repository.upsert_capacity(connection, capability=capability,
                status=status, available_slots=available_slots, valid_until=valid_until,
                expected_version=expected_version, actor_id=principal.principal_id)
            self.repository.audit_log.append_in_transaction(connection, AuditEvent(
                tenant_id=principal.tenant_id, event_type="capability.capacity_declared",
                actor_id=principal.principal_id, outcome=status,
                details={"capability_id": capability_id, "version": version,
                    "available_slots": available_slots, "state_version": value.state_version},
                correlation_id=capability_id))
            return value

    def match(self, *, principal: Principal, required_tags: tuple[str, ...],
              protocol: str, input_classification: Classification,
              compartments: tuple[str, ...], residency_regions: tuple[str, ...],
              limit: int = 20) -> tuple[CapabilityMatch, ...]:
        tags = set(self._tokens(required_tags, "required_tags", 32))
        requested_compartments = set(self._tokens(compartments, "compartments", 32))
        regions = set(self._tokens(residency_regions, "residency_regions", 16))
        if input_classification > principal.clearance or not requested_compartments.issubset(principal.compartments):
            raise PolicyDenied("capability match input is not authorized")
        values = self.discover(principal=principal, protocol=protocol, limit=200)
        with self.repository.transaction(principal.tenant_id) as connection:
            capacities = self.repository.capacities(connection)
        now = datetime.now(UTC); result = []
        for item in values:
            capacity = capacities.get((item.provider_tenant_id, item.capability_id, item.version))
            if (not capacity or capacity.valid_until <= now or capacity.status == "unavailable"
                    or capacity.available_slots == 0 or input_classification > item.max_input_classification
                    or not set(item.required_compartments).issubset(requested_compartments)
                    or (regions and not regions.intersection(item.residency_regions))):
                continue
            overlap = len(tags.intersection(item.tags)); missing = len(tags - set(item.tags))
            score = 50 + overlap * 15 - missing * 20 + min(capacity.available_slots, 20)
            if capacity.status == "limited": score -= 10
            result.append(CapabilityMatch(item, capacity, max(0, score),
                (f"tag_overlap={overlap}", f"missing_tags={missing}",
                 f"available_slots={capacity.available_slots}", f"status={capacity.status}")))
        return tuple(sorted(result, key=lambda x: (-x.score, x.capability.provider_tenant_id,
            x.capability.capability_id))[:limit])

    def reserve(self, *, principal: Principal, reservation_id: str,
                provider_tenant_id: str, capability_id: str, version: str,
                slots: int, expires_at: datetime) -> CapacityReservation:
        self._identifier(reservation_id, "reservation_id")
        if principal.is_service or not 1 <= slots <= 10_000 or expires_at.tzinfo is None:
            raise PolicyDenied("capacity reservation is not authorized")
        now = datetime.now(UTC)
        if not now < expires_at <= now + timedelta(days=30):
            raise ValueError("capacity reservation validity is invalid")
        with self.repository.transaction(principal.tenant_id) as connection:
            capability = self.repository.get(connection, provider_tenant_id=provider_tenant_id,
                capability_id=capability_id, version=version)
            if principal.tenant_id not in capability.visible_to_tenants:
                raise PolicyDenied("capability is not visible to the consumer")
            value, duplicate = self.repository.reserve(connection, reservation_id=reservation_id,
                capability=capability, consumer_tenant_id=principal.tenant_id,
                slots=slots, expires_at=expires_at, actor_id=principal.principal_id)
            if not duplicate:
                self.repository.audit_log.append_in_transaction(connection, AuditEvent(
                    tenant_id=principal.tenant_id, event_type="capability.capacity_reserved",
                    actor_id=principal.principal_id, outcome="active",
                    details={"reservation_id": reservation_id, "provider_tenant_id": provider_tenant_id,
                        "capability_id": capability_id, "version": version, "slots": slots},
                    correlation_id=reservation_id))
            return value

    def release_reservation(self, *, principal: Principal, provider_tenant_id: str,
                            reservation_id: str) -> CapacityReservation:
        if principal.is_service:
            raise PolicyDenied("capacity reservation release is not authorized")
        with self.repository.transaction(principal.tenant_id) as connection:
            value = self.repository.release_reservation(connection,
                provider_tenant_id=provider_tenant_id, reservation_id=reservation_id,
                actor_tenant_id=principal.tenant_id, actor_id=principal.principal_id)
            self.repository.audit_log.append_in_transaction(connection, AuditEvent(
                tenant_id=principal.tenant_id, event_type="capability.capacity_released",
                actor_id=principal.principal_id, outcome="released",
                details={"reservation_id": reservation_id,
                    "provider_tenant_id": provider_tenant_id},
                correlation_id=reservation_id))
            return value

    def propose_negotiation(self, *, principal: Principal, negotiation_id: str,
                            provider_tenant_id: str, capability_id: str, version: str,
                            requested_slots: int, earliest_start: datetime,
                            latest_end: datetime, reason: str) -> CapacityNegotiation:
        self._identifier(negotiation_id, "negotiation_id")
        now = datetime.now(UTC)
        if (principal.is_service or not 1 <= requested_slots <= 10_000
                or earliest_start.tzinfo is None or latest_end.tzinfo is None
                or not now <= earliest_start < latest_end <= now + timedelta(days=366)
                or not reason.strip() or len(reason) > 2_000):
            raise PolicyDenied("capacity negotiation proposal is not authorized")
        with self.repository.transaction(principal.tenant_id) as connection:
            capability = self.repository.get(connection, provider_tenant_id=provider_tenant_id,
                capability_id=capability_id, version=version)
            if principal.tenant_id not in capability.visible_to_tenants:
                raise PolicyDenied("capability is not visible to the consumer")
            value = self.repository.propose_negotiation(connection, capability=capability,
                negotiation_id=negotiation_id, consumer_tenant_id=principal.tenant_id,
                requested_slots=requested_slots, earliest_start=earliest_start,
                latest_end=latest_end, reason_digest=hashlib.sha256(reason.encode()).hexdigest(),
                actor_id=principal.principal_id)
            self.repository.audit_log.append_in_transaction(connection, AuditEvent(
                tenant_id=principal.tenant_id, event_type="capability.negotiation_proposed",
                actor_id=principal.principal_id, outcome="proposed",
                details={"negotiation_id": negotiation_id,
                    "provider_tenant_id": provider_tenant_id}, correlation_id=negotiation_id))
            return value

    def decide_negotiation(self, *, principal: Principal, provider_tenant_id: str,
                           negotiation_id: str, expected_version: int,
                           decision: str, reason: str) -> CapacityNegotiation:
        if principal.is_service or decision not in {"accepted", "rejected", "withdrawn"}:
            raise PolicyDenied("capacity negotiation decision is not authorized")
        if not reason.strip() or len(reason) > 2_000:
            raise ValueError("capacity negotiation decision reason is invalid")
        with self.repository.transaction(principal.tenant_id) as connection:
            value = self.repository.decide_negotiation(connection,
                provider_tenant_id=provider_tenant_id, negotiation_id=negotiation_id,
                actor_tenant_id=principal.tenant_id, actor_id=principal.principal_id,
                expected_version=expected_version, target=decision,
                reason_digest=hashlib.sha256(reason.encode()).hexdigest())
            self.repository.audit_log.append_in_transaction(connection, AuditEvent(
                tenant_id=principal.tenant_id, event_type="capability.negotiation_decided",
                actor_id=principal.principal_id, outcome=decision,
                details={"negotiation_id": negotiation_id,
                    "provider_tenant_id": provider_tenant_id}, correlation_id=negotiation_id))
            return value

    @staticmethod
    def _identifier(value: str, name: str) -> None:
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError(f"{name} is invalid")

    @staticmethod
    def _tokens(values: tuple[str, ...], name: str, maximum: int) -> tuple[str, ...]:
        result = tuple(sorted(set(values)))
        if len(result) > maximum or any(_TOKEN.fullmatch(value) is None for value in result):
            raise ValueError(f"{name} is invalid")
        return result
