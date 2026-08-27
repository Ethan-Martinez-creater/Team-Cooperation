from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


class ReleaseDocumentError(ValueError):
    """Raised when a release-governance document is not strictly valid."""


class InternalInterface(str, Enum):
    DB_SCHEMA = "db_schema"
    CONTROL_PLANE = "control_plane"
    WORKER = "worker"
    CHECKPOINT = "checkpoint"
    ENVELOPE = "envelope"
    AUDIT = "audit"


class CoexistenceMode(str, Enum):
    UNCHANGED = "unchanged"
    BIDIRECTIONAL = "bidirectional"


class ReleaseDecision(str, Enum):
    BLOCKED = "blocked"
    HOLD = "hold"
    PROMOTE = "promote"
    COMPLETE = "complete"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True, order=True)
class StableVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: object, context: str) -> "StableVersion":
        if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
            raise ReleaseDocumentError(f"{context} must be a stable strict semantic version")
        major, minor, patch = value.split(".")
        return cls(int(major), int(minor), int(patch))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def _reject_constant(value: str) -> None:
    raise ReleaseDocumentError(f"non-finite JSON number {value!r} is forbidden")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseDocumentError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_strict_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ReleaseDocumentError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ReleaseDocumentError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseDocumentError("document root must be an object")
    return value


def _object(
    value: object,
    context: str,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseDocumentError(f"{context} must be an object")
    keys = frozenset(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ReleaseDocumentError(f"{context} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ReleaseDocumentError(f"{context} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def _list(value: object, context: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = " non-empty" if nonempty else ""
        raise ReleaseDocumentError(f"{context} must be a{suffix} array")
    return value


def _string(value: object, context: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseDocumentError(f"{context} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ReleaseDocumentError(f"{context} has an invalid format")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ReleaseDocumentError(f"{context} must be a boolean")
    return value


def _integer(value: object, context: str, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ReleaseDocumentError(f"{context} must be an integer {bounds}")
    return value


def _number(
    value: object,
    context: str,
    *,
    minimum: float,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise ReleaseDocumentError(f"{context} must be a number")
    result = float(value)
    outside_minimum = result <= minimum if minimum_exclusive else result < minimum
    if not math.isfinite(result) or outside_minimum or (maximum is not None and result > maximum):
        raise ReleaseDocumentError(f"{context} is outside its allowed range")
    return result


def _interface_versions(value: object, context: str) -> Mapping[InternalInterface, StableVersion]:
    expected = frozenset(item.value for item in InternalInterface)
    source = _object(value, context, required=expected)
    parsed = {
        interface: StableVersion.parse(source[interface.value], f"{context}.{interface.value}")
        for interface in InternalInterface
    }
    return MappingProxyType(parsed)


def _interface_modes(value: object, context: str) -> Mapping[InternalInterface, CoexistenceMode]:
    expected = frozenset(item.value for item in InternalInterface)
    source = _object(value, context, required=expected)
    parsed: dict[InternalInterface, CoexistenceMode] = {}
    for interface in InternalInterface:
        raw = _string(source[interface.value], f"{context}.{interface.value}")
        try:
            parsed[interface] = CoexistenceMode(raw)
        except ValueError as exc:
            raise ReleaseDocumentError(
                f"{context}.{interface.value} must be unchanged or bidirectional"
            ) from exc
    return MappingProxyType(parsed)


@dataclass(frozen=True, slots=True)
class SupportedRelease:
    version: StableVersion
    interfaces: Mapping[InternalInterface, StableVersion]


@dataclass(frozen=True, slots=True)
class TransitionRule:
    from_release: StableVersion
    to_release: StableVersion
    strategy: str
    rollback_safe: bool
    interfaces: Mapping[InternalInterface, CoexistenceMode]


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    allowed: bool
    rollback_safe: bool
    reasons: tuple[str, ...]
    transition: TransitionRule | None = None


@dataclass(frozen=True, slots=True)
class CompatibilityMatrix:
    format_version: str
    releases: tuple[SupportedRelease, ...]
    transitions: tuple[TransitionRule, ...]

    @classmethod
    def from_json(cls, text: str) -> "CompatibilityMatrix":
        root = _object(
            load_strict_json(text),
            "compatibility matrix",
            required=frozenset(
                {
                    "format_version",
                    "default_policy",
                    "excluded_frozen_protocols",
                    "releases",
                    "transitions",
                }
            ),
        )
        if root["format_version"] != "1.0.0":
            raise ReleaseDocumentError("compatibility matrix format_version must be 1.0.0")
        if root["default_policy"] != "deny":
            raise ReleaseDocumentError("compatibility matrix default_policy must be deny")
        excluded = _list(root["excluded_frozen_protocols"], "excluded_frozen_protocols")
        if (
            len(excluded) != 2
            or any(not isinstance(item, str) for item in excluded)
            or set(excluded) != {"mcp", "a2a"}
        ):
            raise ReleaseDocumentError(
                "excluded_frozen_protocols must explicitly contain only mcp and a2a"
            )

        releases: list[SupportedRelease] = []
        seen_releases: set[StableVersion] = set()
        for index, raw in enumerate(_list(root["releases"], "releases", nonempty=True)):
            item = _object(
                raw,
                f"releases[{index}]",
                required=frozenset({"release_version", "interfaces"}),
            )
            version = StableVersion.parse(
                item["release_version"], f"releases[{index}].release_version"
            )
            if version in seen_releases:
                raise ReleaseDocumentError(f"duplicate supported release {version}")
            seen_releases.add(version)
            releases.append(
                SupportedRelease(
                    version=version,
                    interfaces=_interface_versions(
                        item["interfaces"], f"releases[{index}].interfaces"
                    ),
                )
            )

        by_release = {release.version: release for release in releases}
        transitions: list[TransitionRule] = []
        seen_edges: set[tuple[StableVersion, StableVersion, str]] = set()
        for index, raw in enumerate(_list(root["transitions"], "transitions")):
            item = _object(
                raw,
                f"transitions[{index}]",
                required=frozenset(
                    {"from_release", "to_release", "strategy", "rollback_safe", "interfaces"}
                ),
            )
            source = StableVersion.parse(item["from_release"], f"transitions[{index}].from_release")
            target = StableVersion.parse(item["to_release"], f"transitions[{index}].to_release")
            strategy = _string(item["strategy"], f"transitions[{index}].strategy")
            if strategy != "canary":
                raise ReleaseDocumentError("only the canary transition strategy is supported")
            if source not in by_release or target not in by_release:
                raise ReleaseDocumentError(
                    f"transitions[{index}] references an unsupported release"
                )
            if target <= source:
                raise ReleaseDocumentError(f"transitions[{index}] must move to a newer release")
            edge = (source, target, strategy)
            if edge in seen_edges:
                raise ReleaseDocumentError(
                    f"duplicate transition {source} -> {target} ({strategy})"
                )
            seen_edges.add(edge)
            modes = _interface_modes(item["interfaces"], f"transitions[{index}].interfaces")
            for interface in InternalInterface:
                changed = (
                    by_release[source].interfaces[interface]
                    != by_release[target].interfaces[interface]
                )
                if changed == (modes[interface] is CoexistenceMode.UNCHANGED):
                    raise ReleaseDocumentError(
                        f"transitions[{index}].interfaces.{interface.value} contradicts release versions"
                    )
            transitions.append(
                TransitionRule(
                    from_release=source,
                    to_release=target,
                    strategy=strategy,
                    rollback_safe=_boolean(
                        item["rollback_safe"], f"transitions[{index}].rollback_safe"
                    ),
                    interfaces=modes,
                )
            )
        return cls(format_version="1.0.0", releases=tuple(releases), transitions=tuple(transitions))

    def assess(
        self, source: StableVersion, target: StableVersion, strategy: str
    ) -> CompatibilityAssessment:
        releases = {release.version for release in self.releases}
        if source not in releases or target not in releases:
            return CompatibilityAssessment(False, False, ("release_not_explicitly_supported",))
        for transition in self.transitions:
            if (
                transition.from_release == source
                and transition.to_release == target
                and transition.strategy == strategy
            ):
                return CompatibilityAssessment(True, transition.rollback_safe, (), transition)
        return CompatibilityAssessment(False, False, ("transition_not_explicitly_allowed",))

    def canonical_digest(self) -> str:
        payload = {
            "default_policy": "deny",
            "excluded_frozen_protocols": ["a2a", "mcp"],
            "format_version": self.format_version,
            "releases": [
                {
                    "interfaces": {
                        interface.value: str(release.interfaces[interface])
                        for interface in InternalInterface
                    },
                    "release_version": str(release.version),
                }
                for release in self.releases
            ],
            "transitions": [
                {
                    "from_release": str(rule.from_release),
                    "interfaces": {
                        interface.value: rule.interfaces[interface].value
                        for interface in InternalInterface
                    },
                    "rollback_safe": rule.rollback_safe,
                    "strategy": rule.strategy,
                    "to_release": str(rule.to_release),
                }
                for rule in self.transitions
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    artifact_uri: str
    artifact_digest: str
    image_ref: str
    sbom_digest: str
    provenance_digest: str
    produced_by: str


@dataclass(frozen=True, slots=True)
class Approval:
    role: str
    principal: str
    decision: str
    artifact_digest: str
    compatibility_matrix_digest: str


@dataclass(frozen=True, slots=True)
class SLOPolicy:
    availability_target: float
    max_error_budget_burn_rate: float
    max_p95_latency_ms: float


@dataclass(frozen=True, slots=True)
class RolloutStage:
    name: str
    traffic_percent: int
    observation_window_seconds: int
    minimum_requests: int


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    format_version: str
    release_id: str
    requested_by: str
    source_release: StableVersion
    target_release: StableVersion
    compatibility_matrix_digest: str
    artifact: ArtifactIdentity
    approvals: tuple[Approval, ...]
    automatic_rollback: bool
    slo: SLOPolicy
    stages: tuple[RolloutStage, ...]

    @classmethod
    def from_json(cls, text: str) -> "ReleasePlan":
        root = _object(
            load_strict_json(text),
            "release plan",
            required=frozenset(
                {
                    "format_version",
                    "release_id",
                    "requested_by",
                    "source_release",
                    "target_release",
                    "compatibility_matrix_digest",
                    "artifact",
                    "approvals",
                    "automatic_rollback",
                    "slo",
                    "stages",
                }
            ),
        )
        if root["format_version"] != "1.0.0":
            raise ReleaseDocumentError("release plan format_version must be 1.0.0")
        release_id = _string(root["release_id"], "release_id", pattern=_NAME)
        requested_by = _string(root["requested_by"], "requested_by", pattern=_NAME)
        source = StableVersion.parse(root["source_release"], "source_release")
        target = StableVersion.parse(root["target_release"], "target_release")
        if target <= source:
            raise ReleaseDocumentError("target_release must be newer than source_release")
        matrix_digest = _string(
            root["compatibility_matrix_digest"], "compatibility_matrix_digest", pattern=_SHA256
        )

        artifact_raw = _object(
            root["artifact"],
            "artifact",
            required=frozenset(
                {
                    "artifact_uri",
                    "artifact_digest",
                    "image_ref",
                    "sbom_digest",
                    "provenance_digest",
                    "produced_by",
                }
            ),
        )
        artifact = ArtifactIdentity(
            artifact_uri=_string(artifact_raw["artifact_uri"], "artifact.artifact_uri"),
            artifact_digest=_string(
                artifact_raw["artifact_digest"], "artifact.artifact_digest", pattern=_SHA256
            ),
            image_ref=_string(artifact_raw["image_ref"], "artifact.image_ref", pattern=_IMAGE),
            sbom_digest=_string(
                artifact_raw["sbom_digest"], "artifact.sbom_digest", pattern=_SHA256
            ),
            provenance_digest=_string(
                artifact_raw["provenance_digest"], "artifact.provenance_digest", pattern=_SHA256
            ),
            produced_by=_string(artifact_raw["produced_by"], "artifact.produced_by", pattern=_NAME),
        )
        if requested_by == artifact.produced_by:
            raise ReleaseDocumentError("release requester and artifact producer must be distinct")

        approvals: list[Approval] = []
        seen_roles: set[str] = set()
        seen_principals: set[str] = set()
        for index, raw in enumerate(_list(root["approvals"], "approvals", nonempty=True)):
            item = _object(
                raw,
                f"approvals[{index}]",
                required=frozenset(
                    {
                        "role",
                        "principal",
                        "decision",
                        "artifact_digest",
                        "compatibility_matrix_digest",
                    }
                ),
            )
            role = _string(item["role"], f"approvals[{index}].role")
            principal = _string(item["principal"], f"approvals[{index}].principal", pattern=_NAME)
            if item["decision"] != "approved":
                raise ReleaseDocumentError("every release approval must explicitly be approved")
            approved_artifact = _string(
                item["artifact_digest"], f"approvals[{index}].artifact_digest", pattern=_SHA256
            )
            approved_matrix = _string(
                item["compatibility_matrix_digest"],
                f"approvals[{index}].compatibility_matrix_digest",
                pattern=_SHA256,
            )
            if approved_artifact != artifact.artifact_digest or approved_matrix != matrix_digest:
                raise ReleaseDocumentError(
                    "approvals must be bound to the exact artifact and matrix digests"
                )
            if role in seen_roles:
                raise ReleaseDocumentError(f"duplicate approval role {role!r}")
            if principal in seen_principals:
                raise ReleaseDocumentError("approval principals must be distinct")
            if principal in {requested_by, artifact.produced_by}:
                raise ReleaseDocumentError(
                    "requester and artifact producer cannot approve the release"
                )
            seen_roles.add(role)
            seen_principals.add(principal)
            approvals.append(
                Approval(
                    role=role,
                    principal=principal,
                    decision="approved",
                    artifact_digest=approved_artifact,
                    compatibility_matrix_digest=approved_matrix,
                )
            )
        required_roles = {"artifact_attestor", "release_manager", "risk_approver"}
        if seen_roles != required_roles:
            raise ReleaseDocumentError(
                "approvals must contain exactly artifact_attestor, release_manager, and risk_approver"
            )

        slo_raw = _object(
            root["slo"],
            "slo",
            required=frozenset(
                {"availability_target", "max_error_budget_burn_rate", "max_p95_latency_ms"}
            ),
        )
        availability = _number(
            slo_raw["availability_target"],
            "slo.availability_target",
            minimum=0.0,
            maximum=1.0,
            minimum_exclusive=True,
        )
        if availability >= 1.0:
            raise ReleaseDocumentError("slo.availability_target must be less than 1")
        slo = SLOPolicy(
            availability_target=availability,
            max_error_budget_burn_rate=_number(
                slo_raw["max_error_budget_burn_rate"],
                "slo.max_error_budget_burn_rate",
                minimum=0.0,
                minimum_exclusive=True,
            ),
            max_p95_latency_ms=_number(
                slo_raw["max_p95_latency_ms"],
                "slo.max_p95_latency_ms",
                minimum=0.0,
                minimum_exclusive=True,
            ),
        )

        stages: list[RolloutStage] = []
        seen_names: set[str] = set()
        previous_traffic = 0
        for index, raw in enumerate(_list(root["stages"], "stages", nonempty=True)):
            item = _object(
                raw,
                f"stages[{index}]",
                required=frozenset(
                    {"name", "traffic_percent", "observation_window_seconds", "minimum_requests"}
                ),
            )
            name = _string(item["name"], f"stages[{index}].name", pattern=_NAME)
            traffic = _integer(
                item["traffic_percent"], f"stages[{index}].traffic_percent", minimum=1, maximum=100
            )
            if name in seen_names or traffic <= previous_traffic:
                raise ReleaseDocumentError(
                    "stage names must be unique and traffic must strictly increase"
                )
            seen_names.add(name)
            previous_traffic = traffic
            stages.append(
                RolloutStage(
                    name=name,
                    traffic_percent=traffic,
                    observation_window_seconds=_integer(
                        item["observation_window_seconds"],
                        f"stages[{index}].observation_window_seconds",
                        minimum=60,
                    ),
                    minimum_requests=_integer(
                        item["minimum_requests"], f"stages[{index}].minimum_requests", minimum=1
                    ),
                )
            )
        if len(stages) < 2 or stages[0].traffic_percent >= 100 or stages[-1].traffic_percent != 100:
            raise ReleaseDocumentError("stages must start with a canary and end at 100 percent")

        return cls(
            format_version="1.0.0",
            release_id=release_id,
            requested_by=requested_by,
            source_release=source,
            target_release=target,
            compatibility_matrix_digest=matrix_digest,
            artifact=artifact,
            approvals=tuple(approvals),
            automatic_rollback=_boolean(root["automatic_rollback"], "automatic_rollback"),
            slo=slo,
            stages=tuple(stages),
        )


@dataclass(frozen=True, slots=True)
class StageObservation:
    stage: str
    artifact_digest: str
    image_ref: str
    elapsed_seconds: int
    requests: int
    errors: int
    p95_latency_ms: float


def parse_observations(text: str) -> tuple[StageObservation, ...]:
    root = _object(
        load_strict_json(text),
        "observations",
        required=frozenset({"format_version", "observations"}),
    )
    if root["format_version"] != "1.0.0":
        raise ReleaseDocumentError("observations format_version must be 1.0.0")
    observations: list[StageObservation] = []
    seen: set[str] = set()
    for index, raw in enumerate(_list(root["observations"], "observations")):
        item = _object(
            raw,
            f"observations[{index}]",
            required=frozenset(
                {
                    "stage",
                    "artifact_digest",
                    "image_ref",
                    "elapsed_seconds",
                    "requests",
                    "errors",
                    "p95_latency_ms",
                }
            ),
        )
        stage = _string(item["stage"], f"observations[{index}].stage", pattern=_NAME)
        if stage in seen:
            raise ReleaseDocumentError(f"duplicate observation for stage {stage!r}")
        seen.add(stage)
        requests = _integer(item["requests"], f"observations[{index}].requests", minimum=0)
        errors = _integer(
            item["errors"], f"observations[{index}].errors", minimum=0, maximum=requests
        )
        observations.append(
            StageObservation(
                stage=stage,
                artifact_digest=_string(
                    item["artifact_digest"],
                    f"observations[{index}].artifact_digest",
                    pattern=_SHA256,
                ),
                image_ref=_string(
                    item["image_ref"], f"observations[{index}].image_ref", pattern=_IMAGE
                ),
                elapsed_seconds=_integer(
                    item["elapsed_seconds"], f"observations[{index}].elapsed_seconds", minimum=0
                ),
                requests=requests,
                errors=errors,
                p95_latency_ms=_number(
                    item["p95_latency_ms"], f"observations[{index}].p95_latency_ms", minimum=0.0
                ),
            )
        )
    return tuple(observations)


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    passed: bool
    observed: float | int | str | None
    threshold: float | int | str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "observed": self.observed,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: str
    status: str
    gates: tuple[GateResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "status": self.status,
            "gates": [g.to_dict() for g in self.gates],
        }


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    release_id: str
    decision: ReleaseDecision
    promotion_authorized: bool
    next_stage: str | None
    reasons: tuple[str, ...]
    compatibility_matrix_digest: str
    artifact_digest: str
    image_ref: str
    stages: tuple[StageResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": "1.0.0",
            "release_id": self.release_id,
            "decision": self.decision.value,
            "promotion_authorized": self.promotion_authorized,
            "next_stage": self.next_stage,
            "reasons": list(self.reasons),
            "compatibility_matrix_digest": self.compatibility_matrix_digest,
            "artifact_digest": self.artifact_digest,
            "image_ref": self.image_ref,
            "stages": [stage.to_dict() for stage in self.stages],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _report(
    plan: ReleasePlan,
    matrix: CompatibilityMatrix,
    decision: ReleaseDecision,
    reasons: Sequence[str],
    stages: Sequence[StageResult],
    *,
    next_stage: str | None = None,
) -> ReleaseGateReport:
    return ReleaseGateReport(
        release_id=plan.release_id,
        decision=decision,
        promotion_authorized=decision in {ReleaseDecision.PROMOTE, ReleaseDecision.COMPLETE},
        next_stage=next_stage,
        reasons=tuple(reasons),
        compatibility_matrix_digest=matrix.canonical_digest(),
        artifact_digest=plan.artifact.artifact_digest,
        image_ref=plan.artifact.image_ref,
        stages=tuple(stages),
    )


def evaluate_release(
    plan: ReleasePlan,
    matrix: CompatibilityMatrix,
    observations: Sequence[StageObservation],
) -> ReleaseGateReport:
    matrix_digest = matrix.canonical_digest()
    if plan.compatibility_matrix_digest != matrix_digest:
        return _report(
            plan, matrix, ReleaseDecision.BLOCKED, ("compatibility_matrix_digest_mismatch",), ()
        )
    compatibility = matrix.assess(plan.source_release, plan.target_release, "canary")
    if not compatibility.allowed:
        return _report(plan, matrix, ReleaseDecision.BLOCKED, compatibility.reasons, ())
    if plan.automatic_rollback and not compatibility.rollback_safe:
        return _report(
            plan, matrix, ReleaseDecision.BLOCKED, ("automatic_rollback_not_compatible",), ()
        )

    stage_order = {stage.name: index for index, stage in enumerate(plan.stages)}
    observed = {item.stage: item for item in observations}
    if any(name not in stage_order for name in observed):
        return _report(plan, matrix, ReleaseDecision.BLOCKED, ("unknown_observation_stage",), ())
    observed_indexes = [stage_order[item.stage] for item in observations]
    if observed_indexes != list(range(len(observed_indexes))):
        return _report(plan, matrix, ReleaseDecision.BLOCKED, ("observation_sequence_gap",), ())

    results: list[StageResult] = []
    for index, stage in enumerate(plan.stages):
        item = observed.get(stage.name)
        if item is None:
            decision = ReleaseDecision.PROMOTE if results else ReleaseDecision.HOLD
            reason = "next_stage_authorized" if results else "awaiting_canary_observation"
            return _report(
                plan,
                matrix,
                decision,
                (reason,),
                results,
                next_stage=stage.name,
            )

        identity_ok = (
            item.artifact_digest == plan.artifact.artifact_digest
            and item.image_ref == plan.artifact.image_ref
        )
        enough_window = item.elapsed_seconds >= stage.observation_window_seconds
        enough_requests = item.requests >= stage.minimum_requests
        error_rate = (item.errors / item.requests) if item.requests else 0.0
        availability = 1.0 - error_rate
        error_budget = 1.0 - plan.slo.availability_target
        burn_rate = error_rate / error_budget
        gates = (
            GateResult(
                "immutable_identity",
                identity_ok,
                item.artifact_digest,
                plan.artifact.artifact_digest,
            ),
            GateResult(
                "observation_window",
                enough_window,
                item.elapsed_seconds,
                stage.observation_window_seconds,
            ),
            GateResult("minimum_requests", enough_requests, item.requests, stage.minimum_requests),
            GateResult(
                "availability_slo",
                availability >= plan.slo.availability_target,
                availability,
                plan.slo.availability_target,
            ),
            GateResult(
                "error_budget_burn",
                burn_rate <= plan.slo.max_error_budget_burn_rate,
                burn_rate,
                plan.slo.max_error_budget_burn_rate,
            ),
            GateResult(
                "p95_latency",
                item.p95_latency_ms <= plan.slo.max_p95_latency_ms,
                item.p95_latency_ms,
                plan.slo.max_p95_latency_ms,
            ),
        )
        if not identity_ok:
            results.append(StageResult(stage.name, "failed", gates))
            decision = (
                ReleaseDecision.ROLLBACK
                if plan.automatic_rollback and compatibility.rollback_safe
                else ReleaseDecision.BLOCKED
            )
            return _report(
                plan, matrix, decision, ("deployed_artifact_identity_mismatch",), results
            )
        if not enough_window or not enough_requests:
            results.append(StageResult(stage.name, "observing", gates))
            return _report(
                plan,
                matrix,
                ReleaseDecision.HOLD,
                ("observation_window_or_sample_incomplete",),
                results,
                next_stage=stage.name,
            )
        if not all(gate.passed for gate in gates):
            results.append(StageResult(stage.name, "failed", gates))
            decision = (
                ReleaseDecision.ROLLBACK
                if plan.automatic_rollback and compatibility.rollback_safe
                else ReleaseDecision.BLOCKED
            )
            return _report(plan, matrix, decision, ("slo_or_error_budget_gate_failed",), results)
        results.append(StageResult(stage.name, "passed", gates))
        if index == len(plan.stages) - 1:
            return _report(
                plan, matrix, ReleaseDecision.COMPLETE, ("all_release_gates_passed",), results
            )

    raise AssertionError("unreachable release evaluation state")
