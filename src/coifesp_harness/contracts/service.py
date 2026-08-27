from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Connection

from ..audit import AuditEvent
from ..collaboration.governance_models import CollaborationRole
from ..collaboration.repository import GOVERNANCE_ASSIGNMENTS, GOVERNANCE_MEMBERS
from ..errors import GovernanceConflictError, GovernanceError, PolicyDenied, ResourceNotFound
from ..security import Principal
from .models import Compatibility, ContractKind, ImpactSeverity, ImpactState, SemanticVersion, validate_digest, validate_id
from .repository import (
    CHANGE_IMPACTS,
    CONTRACT_DEPENDENCIES,
    CONTRACT_RELEASES,
    CONTRACTS,
    SQLAlchemyContractRepository,
)

_CLAUSE = re.compile(r"^(>=|<=|>|<|=)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class ContractCommandResult:
    result_id: str
    duplicate: bool
    impacts_created: int = 0


def _digest(command: str, payload: dict) -> str:
    encoded = json.dumps({"command": command, **payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _satisfies(version: str, constraint: str) -> bool:
    candidate = SemanticVersion.parse(version)
    if candidate.prerelease:
        raise GovernanceError("prerelease versions cannot satisfy production dependencies")
    value = constraint.strip()
    if value.startswith("^"):
        floor = SemanticVersion.parse(value[1:])
        ceiling = SemanticVersion(floor.major + 1, 0, 0) if floor.major else (SemanticVersion(0, floor.minor + 1, 0) if floor.minor else SemanticVersion(0, 0, floor.patch + 1))
        return floor <= candidate < ceiling
    if value.startswith("~"):
        floor = SemanticVersion.parse(value[1:])
        return floor <= candidate < SemanticVersion(floor.major, floor.minor + 1, 0)
    for raw in value.split(","):
        match = _CLAUSE.fullmatch(raw.strip())
        if match is None:
            raise GovernanceError("version constraint must be exact, caret, tilde, or comma-separated comparisons")
        op = match[1] or "="
        target = SemanticVersion(int(match[2]), int(match[3]), int(match[4]))
        if not {"=": candidate == target, ">": candidate > target, ">=": candidate >= target, "<": candidate < target, "<=": candidate <= target}[op]:
            return False
    return True


class ContractCoordinationService:
    """Durable bilateral contract and change-impact coordination boundary."""

    def __init__(self, repository: SQLAlchemyContractRepository) -> None:
        self.repository = repository

    def register_contract(self, *, principal: Principal, idempotency_key: str, program_id: str, contract_id: str, producer_assignment_id: str, name: str, kind: ContractKind, visible_to_tenants: frozenset[str]) -> ContractCommandResult:
        validate_id(contract_id, "contract_id")
        if not name or len(name) > 256:
            raise ValueError("contract name is invalid")
        command = "contract.register"
        digest = _digest(command, {"program_id": program_id, "contract_id": contract_id, "producer_assignment_id": producer_assignment_id, "name": name, "kind": kind.value, "visible": sorted(visible_to_tenants)})
        with self.repository.transaction(principal.tenant_id) as connection:
            duplicate = self._claim(connection, principal, idempotency_key, program_id, command, digest)
            if duplicate:
                return duplicate
            assignment = self._assignment(connection, program_id, producer_assignment_id)
            member = self._member(connection, program_id, principal)
            producer_tenant = self._member_tenant(connection, program_id, assignment["assignee_id"])
            if principal.tenant_id != producer_tenant or (principal.principal_id != assignment["assignee_id"] and member["role"] != CollaborationRole.LEAD.value):
                raise PolicyDenied("only the producer assignee or its tenant lead may register a contract")
            allowed = frozenset(assignment["visible_to_tenants"])
            if not visible_to_tenants or producer_tenant not in visible_to_tenants or not visible_to_tenants.issubset(allowed):
                raise PolicyDenied("contract visibility must be explicit and within assignment visibility")
            now = datetime.now(UTC)
            connection.execute(insert(CONTRACTS).values(program_id=program_id, contract_id=contract_id, producer_tenant_id=producer_tenant, producer_assignment_id=producer_assignment_id, name=name, kind=kind.value, visible_to_tenants=sorted(visible_to_tenants), aggregate_version=1, created_by=principal.principal_id, created_at=now))
            self._event(connection, principal, program_id, contract_id, "registered", visible_to_tenants, {"contract_id": contract_id, "kind": kind.value, "producer_assignment_id": producer_assignment_id})
            self.repository.complete(connection, tenant_id=principal.tenant_id, key=idempotency_key, result_id=contract_id)
            return ContractCommandResult(contract_id, False)

    def release(self, *, principal: Principal, idempotency_key: str, program_id: str, contract_id: str, version: str, content_digest: str, artifact_ref: str, compatibility: Compatibility, predecessor_version: str | None) -> ContractCommandResult:
        parsed = SemanticVersion.parse(version)
        if parsed.prerelease:
            raise GovernanceError("production contract releases cannot be prereleases")
        validate_digest(content_digest)
        if not artifact_ref or len(artifact_ref) > 1024:
            raise ValueError("artifact_ref is invalid")
        command = "contract.release"
        digest = _digest(command, {"program_id": program_id, "contract_id": contract_id, "version": version, "content_digest": content_digest, "artifact_ref": artifact_ref, "compatibility": compatibility.value, "predecessor_version": predecessor_version})
        with self.repository.transaction(principal.tenant_id) as connection:
            duplicate = self._claim(connection, principal, idempotency_key, program_id, command, digest)
            if duplicate:
                return duplicate
            contract = self._contract_for_update(connection, program_id, contract_id)
            member = self._member(connection, program_id, principal)
            if principal.tenant_id != contract["producer_tenant_id"] or member["role"] != CollaborationRole.LEAD.value:
                raise PolicyDenied("only a producer-tenant lead may release a contract")
            latest = connection.execute(select(CONTRACT_RELEASES).where(and_(CONTRACT_RELEASES.c.program_id == program_id, CONTRACT_RELEASES.c.contract_id == contract_id)).order_by(CONTRACT_RELEASES.c.released_at.desc()).limit(1).with_for_update()).mappings().one_or_none()
            if latest is None:
                if predecessor_version is not None:
                    raise GovernanceConflictError("the first release cannot name a predecessor")
            else:
                if predecessor_version != latest["version"]:
                    raise GovernanceConflictError("release predecessor is stale")
                if SemanticVersion.parse(version) <= SemanticVersion.parse(latest["version"]):
                    raise GovernanceConflictError("contract versions must increase monotonically")
                if latest["content_digest"] == content_digest:
                    compatibility = Compatibility.COMPATIBLE
                elif parsed.major > SemanticVersion.parse(latest["version"]).major:
                    # A producer declaration can never downgrade a major-version
                    # change to compatible; consumers still decide acceptance.
                    compatibility = Compatibility.BREAKING
            now = datetime.now(UTC)
            connection.execute(insert(CONTRACT_RELEASES).values(program_id=program_id, contract_id=contract_id, version=version, content_digest=content_digest, artifact_ref=artifact_ref, compatibility=compatibility.value, predecessor_version=predecessor_version, visible_to_tenants=contract["visible_to_tenants"], released_by=principal.principal_id, released_at=now))
            impacts = 0
            if latest is not None and latest["content_digest"] != content_digest:
                dependencies = connection.execute(select(CONTRACT_DEPENDENCIES).where(and_(CONTRACT_DEPENDENCIES.c.program_id == program_id, CONTRACT_DEPENDENCIES.c.contract_id == contract_id))).mappings().all()
                for dependency in dependencies:
                    allowed = _satisfies(version, dependency["version_constraint"])
                    severity = ImpactSeverity.BLOCKING if compatibility is Compatibility.BREAKING or not allowed else (ImpactSeverity.ACTION_REQUIRED if compatibility is Compatibility.UNKNOWN else ImpactSeverity.NOTICE)
                    impact_id = hashlib.sha256(f"{program_id}\n{dependency['dependency_id']}\n{version}".encode()).hexdigest()
                    visibility = frozenset({contract["producer_tenant_id"], dependency["consumer_tenant_id"]})
                    connection.execute(insert(CHANGE_IMPACTS).values(program_id=program_id, impact_id=impact_id, dependency_id=dependency["dependency_id"], contract_id=contract_id, consumer_tenant_id=dependency["consumer_tenant_id"], consumer_assignment_id=dependency["consumer_assignment_id"], from_version=dependency["baseline_version"], to_version=version, from_digest=dependency["baseline_digest"], to_digest=content_digest, severity=int(severity), compatibility=compatibility.value, state=ImpactState.PENDING.value, state_version=1, consumer_note=None, remediation=None, acknowledged_by=None, accepted_by=None, visible_to_tenants=sorted(visibility), created_at=now, updated_at=now))
                    self._event(connection, principal, program_id, impact_id, "impact_created", visibility, {"impact_id": impact_id, "contract_id": contract_id, "consumer_assignment_id": dependency["consumer_assignment_id"], "from_version": dependency["baseline_version"], "to_version": version, "compatibility": compatibility.value, "severity": int(severity)})
                    impacts += 1
            connection.execute(update(CONTRACTS).where(and_(CONTRACTS.c.program_id == program_id, CONTRACTS.c.contract_id == contract_id)).values(aggregate_version=CONTRACTS.c.aggregate_version + 1))
            self._event(connection, principal, program_id, contract_id, "released", frozenset(contract["visible_to_tenants"]), {"contract_id": contract_id, "version": version, "digest": content_digest, "compatibility": compatibility.value, "impacts_created": impacts})
            self.repository.complete(connection, tenant_id=principal.tenant_id, key=idempotency_key, result_id=version)
            return ContractCommandResult(version, False, impacts)

    def declare_dependency(self, *, principal: Principal, idempotency_key: str, program_id: str, dependency_id: str, contract_id: str, consumer_assignment_id: str, version_constraint: str, baseline_version: str) -> ContractCommandResult:
        validate_id(dependency_id, "dependency_id")
        _satisfies(baseline_version, version_constraint)
        command = "contract.dependency.declare"
        digest = _digest(command, {"program_id": program_id, "dependency_id": dependency_id, "contract_id": contract_id, "consumer_assignment_id": consumer_assignment_id, "version_constraint": version_constraint, "baseline_version": baseline_version})
        with self.repository.transaction(principal.tenant_id) as connection:
            duplicate = self._claim(connection, principal, idempotency_key, program_id, command, digest)
            if duplicate:
                return duplicate
            contract = self._contract_for_update(connection, program_id, contract_id)
            assignment = self._assignment(connection, program_id, consumer_assignment_id)
            member = self._member(connection, program_id, principal)
            consumer_tenant = self._member_tenant(connection, program_id, assignment["assignee_id"])
            if principal.tenant_id != consumer_tenant or (principal.principal_id != assignment["assignee_id"] and member["role"] != CollaborationRole.LEAD.value):
                raise PolicyDenied("only the consumer assignee or its tenant lead may declare a dependency")
            if consumer_tenant not in contract["visible_to_tenants"]:
                raise ResourceNotFound("contract is absent or hidden")
            release = connection.execute(select(CONTRACT_RELEASES).where(and_(CONTRACT_RELEASES.c.program_id == program_id, CONTRACT_RELEASES.c.contract_id == contract_id, CONTRACT_RELEASES.c.version == baseline_version))).mappings().one_or_none()
            if release is None:
                raise ResourceNotFound("baseline contract release is absent or hidden")
            visibility = frozenset({contract["producer_tenant_id"], consumer_tenant})
            now = datetime.now(UTC)
            connection.execute(insert(CONTRACT_DEPENDENCIES).values(program_id=program_id, dependency_id=dependency_id, contract_id=contract_id, consumer_tenant_id=consumer_tenant, consumer_assignment_id=consumer_assignment_id, version_constraint=version_constraint, baseline_version=baseline_version, baseline_digest=release["content_digest"], visible_to_tenants=sorted(visibility), created_by=principal.principal_id, created_at=now))
            self._event(connection, principal, program_id, dependency_id, "dependency_declared", visibility, {"dependency_id": dependency_id, "contract_id": contract_id, "consumer_assignment_id": consumer_assignment_id, "version_constraint": version_constraint, "baseline_version": baseline_version})
            self.repository.complete(connection, tenant_id=principal.tenant_id, key=idempotency_key, result_id=dependency_id)
            return ContractCommandResult(dependency_id, False)

    def respond_to_impact(self, *, principal: Principal, idempotency_key: str, program_id: str, impact_id: str, block: bool, note: str) -> ContractCommandResult:
        if not note:
            raise GovernanceError("impact response requires a note")
        target = ImpactState.BLOCKED if block else ImpactState.ACKNOWLEDGED
        return self._transition(principal, idempotency_key, program_id, impact_id, "impact.respond", target, consumer_note=note, allowed={ImpactState.PENDING, ImpactState.REMEDIATION_PROPOSED}, consumer=True)

    def propose_remediation(self, *, principal: Principal, idempotency_key: str, program_id: str, impact_id: str, remediation: str) -> ContractCommandResult:
        if not remediation:
            raise GovernanceError("remediation is required")
        return self._transition(principal, idempotency_key, program_id, impact_id, "impact.remediation.propose", ImpactState.REMEDIATION_PROPOSED, remediation=remediation, allowed={ImpactState.ACKNOWLEDGED, ImpactState.BLOCKED}, producer_lead=True)

    def accept_impact(self, *, principal: Principal, idempotency_key: str, program_id: str, impact_id: str, note: str) -> ContractCommandResult:
        if not note:
            raise GovernanceError("impact acceptance requires a note")
        return self._transition(principal, idempotency_key, program_id, impact_id, "impact.accept", ImpactState.ACCEPTED, consumer_note=note, allowed={ImpactState.ACKNOWLEDGED, ImpactState.REMEDIATION_PROPOSED}, consumer=True, accept=True)

    def assert_assignment_verifiable(self, connection: Connection, *, program_id: str, assignment_id: str) -> None:
        unresolved = connection.execute(select(CHANGE_IMPACTS.c.impact_id).where(and_(CHANGE_IMPACTS.c.program_id == program_id, CHANGE_IMPACTS.c.consumer_assignment_id == assignment_id, CHANGE_IMPACTS.c.severity >= int(ImpactSeverity.ACTION_REQUIRED), CHANGE_IMPACTS.c.state != ImpactState.ACCEPTED.value))).scalars().all()
        if unresolved:
            raise GovernanceError(f"assignment has unresolved contract impacts: {sorted(unresolved)}")

    def _transition(self, principal, key, program_id, impact_id, command, target, *, allowed, consumer_note=None, remediation=None, consumer=False, producer_lead=False, accept=False):
        digest = _digest(command, {"program_id": program_id, "impact_id": impact_id, "target": target.value, "consumer_note": consumer_note, "remediation": remediation})
        with self.repository.transaction(principal.tenant_id) as connection:
            duplicate = self._claim(connection, principal, key, program_id, command, digest)
            if duplicate:
                return duplicate
            impact = connection.execute(select(CHANGE_IMPACTS).where(and_(CHANGE_IMPACTS.c.program_id == program_id, CHANGE_IMPACTS.c.impact_id == impact_id)).with_for_update()).mappings().one_or_none()
            if impact is None:
                raise ResourceNotFound("impact is absent or hidden")
            member = self._member(connection, program_id, principal)
            contract = self._contract_for_update(connection, program_id, impact["contract_id"])
            assignment = self._assignment(connection, program_id, impact["consumer_assignment_id"])
            if consumer and (principal.tenant_id != impact["consumer_tenant_id"] or (principal.principal_id != assignment["assignee_id"] and member["role"] != CollaborationRole.LEAD.value)):
                raise PolicyDenied("only the consumer assignee or its tenant lead may decide this impact")
            if producer_lead and (principal.tenant_id != contract["producer_tenant_id"] or member["role"] != CollaborationRole.LEAD.value):
                raise PolicyDenied("only a producer-tenant lead may propose remediation")
            if ImpactState(impact["state"]) not in allowed:
                raise GovernanceConflictError("impact state transition is invalid")
            values = {"state": target.value, "state_version": impact["state_version"] + 1, "updated_at": datetime.now(UTC)}
            if consumer_note is not None:
                values.update(consumer_note=consumer_note, acknowledged_by=principal.principal_id)
            if remediation is not None:
                values["remediation"] = remediation
            if accept:
                sibling_versions = connection.execute(
                    select(CHANGE_IMPACTS.c.to_version).where(
                        and_(
                            CHANGE_IMPACTS.c.program_id == program_id,
                            CHANGE_IMPACTS.c.dependency_id == impact["dependency_id"],
                        )
                    )
                ).scalars().all()
                if any(
                    SemanticVersion.parse(candidate)
                    > SemanticVersion.parse(impact["to_version"])
                    for candidate in sibling_versions
                ):
                    raise GovernanceConflictError(
                        "an obsolete impact cannot replace a newer dependency baseline"
                    )
                values["accepted_by"] = principal.principal_id
                connection.execute(update(CONTRACT_DEPENDENCIES).where(and_(CONTRACT_DEPENDENCIES.c.program_id == program_id, CONTRACT_DEPENDENCIES.c.dependency_id == impact["dependency_id"])).values(baseline_version=impact["to_version"], baseline_digest=impact["to_digest"]))
            connection.execute(update(CHANGE_IMPACTS).where(and_(CHANGE_IMPACTS.c.program_id == program_id, CHANGE_IMPACTS.c.impact_id == impact_id, CHANGE_IMPACTS.c.state_version == impact["state_version"])).values(**values))
            visibility = frozenset(impact["visible_to_tenants"])
            self._event(connection, principal, program_id, impact_id, target.value, visibility, {"impact_id": impact_id, "state": target.value, "note_digest": hashlib.sha256((consumer_note or remediation or "").encode()).hexdigest()})
            self.repository.complete(connection, tenant_id=principal.tenant_id, key=key, result_id=impact_id)
            return ContractCommandResult(impact_id, False)

    def _claim(self, connection, principal, key, program_id, command, digest):
        claim = self.repository.claim(connection, tenant_id=principal.tenant_id, key=key, program_id=program_id, command_type=command, digest=digest)
        return ContractCommandResult(claim.result_id or "", True) if claim.duplicate else None

    @staticmethod
    def _member(connection, program_id, principal):
        row = connection.execute(select(GOVERNANCE_MEMBERS).where(and_(GOVERNANCE_MEMBERS.c.program_id == program_id, GOVERNANCE_MEMBERS.c.principal_id == principal.principal_id))).mappings().one_or_none()
        if row is None or row["tenant_id"] != principal.tenant_id:
            raise PolicyDenied("principal is not a matching governance member")
        return row

    @staticmethod
    def _member_tenant(connection, program_id, principal_id):
        row = connection.execute(select(GOVERNANCE_MEMBERS.c.tenant_id).where(and_(GOVERNANCE_MEMBERS.c.program_id == program_id, GOVERNANCE_MEMBERS.c.principal_id == principal_id))).scalar_one_or_none()
        if row is None:
            raise GovernanceError("assignment assignee is not a governance member")
        return row

    @staticmethod
    def _assignment(connection, program_id, assignment_id):
        row = connection.execute(select(GOVERNANCE_ASSIGNMENTS).where(and_(GOVERNANCE_ASSIGNMENTS.c.program_id == program_id, GOVERNANCE_ASSIGNMENTS.c.assignment_id == assignment_id))).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("assignment is absent or hidden")
        return row

    @staticmethod
    def _contract_for_update(connection, program_id, contract_id):
        row = connection.execute(select(CONTRACTS).where(and_(CONTRACTS.c.program_id == program_id, CONTRACTS.c.contract_id == contract_id)).with_for_update()).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("contract is absent or hidden")
        return row

    def _event(self, connection, principal, program_id, subject_id, suffix, visibility, payload):
        self.repository.append_event(connection, event=AuditEvent(tenant_id=principal.tenant_id, event_type=f"contract.{suffix}", actor_id=principal.principal_id, outcome="recorded", details={"program_id": program_id, "subject_id": subject_id, **payload}, correlation_id=program_id), subject_id=subject_id, visibility=visibility, payload=payload)
