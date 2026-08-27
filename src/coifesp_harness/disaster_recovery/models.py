from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_SEQUENCE = (
    "declare_incident",
    "fence_primary_writes",
    "verify_immutable_backup",
    "restore_postgres_pitr",
    "verify_audit_chain",
    "promote_recovery_region",
    "replay_queues_idempotently",
    "reopen_writes",
)
_REQUIRED_ROLES = frozenset({"incident_commander", "database_recovery", "security_approver"})


class DisasterRecoveryDocumentError(ValueError):
    """Raised when an offline DR attestation document is invalid."""


class DrillDecision(str, Enum):
    BLOCKED = "blocked"
    PASS = "pass"


def _reject_constant(value: str) -> None:
    raise DisasterRecoveryDocumentError(f"non-finite JSON number {value!r} is forbidden")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DisasterRecoveryDocumentError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except DisasterRecoveryDocumentError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise DisasterRecoveryDocumentError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise DisasterRecoveryDocumentError("document root must be an object")
    return value


def _object(value: object, context: str, required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DisasterRecoveryDocumentError(f"{context} must be an object")
    keys = set(value)
    if keys != required:
        missing, unknown = required - keys, keys - required
        if missing:
            raise DisasterRecoveryDocumentError(f"{context} missing fields: {', '.join(sorted(missing))}")
        raise DisasterRecoveryDocumentError(f"{context} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def _string(value: object, context: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or (pattern and pattern.fullmatch(value) is None):
        raise DisasterRecoveryDocumentError(f"{context} has an invalid format")
    return value


def _integer(value: object, context: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise DisasterRecoveryDocumentError(f"{context} must be an integer >= {minimum}")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise DisasterRecoveryDocumentError(f"{context} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class DisasterRecoveryPlan:
    plan_id: str
    primary_region: str
    recovery_region: str
    rpo_seconds: int
    rto_seconds: int
    base_backup_digest: str
    wal_manifest_digest: str
    object_inventory_digest: str
    audit_chain_digest: str
    queue_checkpoint_digest: str
    lease_epoch: int
    canonical_digest: str

    @classmethod
    def from_json(cls, text: str) -> "DisasterRecoveryPlan":
        root = _object(_load(text), "recovery plan", {
            "format_version", "plan_id", "created_at", "primary_region", "recovery_region",
            "object_storage", "postgres_pitr", "dependencies", "object_inventory_digest",
            "audit_chain_digest", "queue_replay", "fencing", "rpo_seconds", "rto_seconds",
            "recovery_sequence", "approvals",
        })
        if root["format_version"] != "1.0.0":
            raise DisasterRecoveryDocumentError("format_version must be 1.0.0")
        plan_id = _string(root["plan_id"], "plan_id", _ID)
        _string(root["created_at"], "created_at", _UTC)
        primary = _string(root["primary_region"], "primary_region", _ID)
        recovery = _string(root["recovery_region"], "recovery_region", _ID)
        if primary == recovery:
            raise DisasterRecoveryDocumentError("recovery_region must differ from primary_region")
        rpo, rto = _integer(root["rpo_seconds"], "rpo_seconds", 1), _integer(root["rto_seconds"], "rto_seconds", 1)
        storage = _object(root["object_storage"], "object_storage", {"provider", "immutable", "retention_days", "inventory_digest"})
        _string(storage["provider"], "object_storage.provider", _ID)
        if not _boolean(storage["immutable"], "object_storage.immutable"):
            raise DisasterRecoveryDocumentError("object_storage.immutable must be true")
        _integer(storage["retention_days"], "object_storage.retention_days", 1)
        inventory = _string(storage["inventory_digest"], "object_storage.inventory_digest", _SHA256)
        if inventory != _string(root["object_inventory_digest"], "object_inventory_digest", _SHA256):
            raise DisasterRecoveryDocumentError("object inventory digest must be bound at plan root")
        pitr = _object(root["postgres_pitr"], "postgres_pitr", {"enabled", "base_backup_digest", "wal_manifest_digest", "recovery_target_utc", "maximum_backup_age_seconds"})
        if not _boolean(pitr["enabled"], "postgres_pitr.enabled"):
            raise DisasterRecoveryDocumentError("postgres_pitr.enabled must be true")
        base = _string(pitr["base_backup_digest"], "postgres_pitr.base_backup_digest", _SHA256)
        wal = _string(pitr["wal_manifest_digest"], "postgres_pitr.wal_manifest_digest", _SHA256)
        _string(pitr["recovery_target_utc"], "postgres_pitr.recovery_target_utc", _UTC)
        if _integer(pitr["maximum_backup_age_seconds"], "postgres_pitr.maximum_backup_age_seconds", 1) > rpo:
            raise DisasterRecoveryDocumentError("maximum backup age exceeds RPO")
        dependencies = _object(root["dependencies"], "dependencies", {"backup_encryption_key_ref", "audit_verification_key_ref", "restore_authorization_ref"})
        for name, value in dependencies.items():
            _string(value, f"dependencies.{name}", _ID)
        if len(set(dependencies.values())) != 3:
            raise DisasterRecoveryDocumentError("key and restore authorization references must be separated")
        audit = _string(root["audit_chain_digest"], "audit_chain_digest", _SHA256)
        queue = _object(root["queue_replay"], "queue_replay", {"checkpoint_digest", "idempotency_required", "dead_letter_review_required"})
        checkpoint = _string(queue["checkpoint_digest"], "queue_replay.checkpoint_digest", _SHA256)
        if not _boolean(queue["idempotency_required"], "queue_replay.idempotency_required") or not _boolean(queue["dead_letter_review_required"], "queue_replay.dead_letter_review_required"):
            raise DisasterRecoveryDocumentError("queue replay must require idempotency and dead-letter review")
        fencing = _object(root["fencing"], "fencing", {"lease_epoch", "primary_write_fence_required", "recovery_epoch_must_increase"})
        epoch = _integer(fencing["lease_epoch"], "fencing.lease_epoch", 1)
        if not _boolean(fencing["primary_write_fence_required"], "fencing.primary_write_fence_required") or not _boolean(fencing["recovery_epoch_must_increase"], "fencing.recovery_epoch_must_increase"):
            raise DisasterRecoveryDocumentError("lease fencing requirements must be true")
        if tuple(root["recovery_sequence"]) != _SEQUENCE:
            raise DisasterRecoveryDocumentError("recovery_sequence must match the mandatory ordered runbook")
        approvals = root["approvals"]
        if not isinstance(approvals, list) or len(approvals) != len(_REQUIRED_ROLES):
            raise DisasterRecoveryDocumentError("approvals must contain each required role exactly once")
        roles, principals = set(), set()
        for index, approval in enumerate(approvals):
            item = _object(approval, f"approvals[{index}]", {"role", "principal", "decision", "plan_digest"})
            roles.add(_string(item["role"], f"approvals[{index}].role", _ID))
            principal = _string(item["principal"], f"approvals[{index}].principal", _ID)
            principals.add(principal)
            if item["decision"] != "approved":
                raise DisasterRecoveryDocumentError("all approvals must be approved")
            _string(item["plan_digest"], f"approvals[{index}].plan_digest", _SHA256)
        if roles != _REQUIRED_ROLES or len(principals) != len(_REQUIRED_ROLES):
            raise DisasterRecoveryDocumentError("approvals require separated required roles and principals")
        # Approval signatures bind the full operational plan.  Approvals themselves
        # are intentionally excluded to avoid a self-referential digest cycle.
        binding = {key: value for key, value in root.items() if key != "approvals"}
        canonical = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        declared = {_string(item["plan_digest"], "approval.plan_digest", _SHA256) for item in approvals}
        if declared != {digest}:
            raise DisasterRecoveryDocumentError("approvals must bind the canonical operational plan")
        return cls(plan_id, primary, recovery, rpo, rto, base, wal, inventory, audit, checkpoint, epoch, digest)


@dataclass(frozen=True, slots=True)
class DrillEvidence:
    drill_id: str
    plan_digest: str
    recovery_region: str
    restore_seconds: int
    recovery_point_lag_seconds: int
    recovered_lease_epoch: int
    base_backup_digest: str
    wal_manifest_digest: str
    object_inventory_digest: str
    audit_chain_digest: str
    queue_checkpoint_digest: str
    primary_fenced: bool
    data_consistent: bool
    audit_chain_verified: bool
    queues_replayed_idempotently: bool
    dead_letters_reviewed: bool

    @classmethod
    def from_json(cls, text: str) -> "DrillEvidence":
        root = _object(_load(text), "drill evidence", {
            "format_version", "drill_id", "plan_digest", "scenario", "recovery_region", "restore_seconds",
            "recovery_point_lag_seconds",
            "recovered_lease_epoch", "base_backup_digest", "wal_manifest_digest", "object_inventory_digest",
            "audit_chain_digest", "queue_checkpoint_digest", "primary_fenced", "data_consistent",
            "audit_chain_verified", "queues_replayed_idempotently", "dead_letters_reviewed", "evidence_digest",
        })
        if root["format_version"] != "1.0.0" or root["scenario"] != "regional_primary_outage":
            raise DisasterRecoveryDocumentError("only format 1.0.0 regional_primary_outage drills are accepted")
        for field in ("drill_id", "recovery_region"):
            _string(root[field], field, _ID)
        for field in ("plan_digest", "base_backup_digest", "wal_manifest_digest", "object_inventory_digest", "audit_chain_digest", "queue_checkpoint_digest", "evidence_digest"):
            _string(root[field], field, _SHA256)
        binding = {key: value for key, value in root.items() if key != "evidence_digest"}
        actual_digest = "sha256:" + hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if root["evidence_digest"] != actual_digest:
            raise DisasterRecoveryDocumentError("evidence_digest does not bind the drill evidence")
        return cls(
            root["drill_id"], root["plan_digest"], root["recovery_region"], _integer(root["restore_seconds"], "restore_seconds"),
            _integer(root["recovery_point_lag_seconds"], "recovery_point_lag_seconds"),
            _integer(root["recovered_lease_epoch"], "recovered_lease_epoch", 1), root["base_backup_digest"], root["wal_manifest_digest"], root["object_inventory_digest"], root["audit_chain_digest"], root["queue_checkpoint_digest"],
            *(_boolean(root[field], field) for field in ("primary_fenced", "data_consistent", "audit_chain_verified", "queues_replayed_idempotently", "dead_letters_reviewed")),
        )


def evaluate_drill(plan: DisasterRecoveryPlan, evidence: DrillEvidence) -> dict[str, object]:
    """Return a deterministic, non-sensitive, fail-closed drill report."""
    checks = {
        "plan_digest_bound": evidence.plan_digest == plan.canonical_digest,
        "recovery_region": evidence.recovery_region == plan.recovery_region,
        "immutable_backup_identity": evidence.base_backup_digest == plan.base_backup_digest and evidence.wal_manifest_digest == plan.wal_manifest_digest and evidence.object_inventory_digest == plan.object_inventory_digest,
        "audit_chain_identity": evidence.audit_chain_digest == plan.audit_chain_digest and evidence.audit_chain_verified,
        "rto": evidence.restore_seconds <= plan.rto_seconds,
        "rpo": evidence.recovery_point_lag_seconds <= plan.rpo_seconds,
        "lease_fencing": evidence.primary_fenced and evidence.recovered_lease_epoch > plan.lease_epoch,
        "data_consistency": evidence.data_consistent,
        "queue_replay": evidence.queue_checkpoint_digest == plan.queue_checkpoint_digest and evidence.queues_replayed_idempotently and evidence.dead_letters_reviewed,
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    return {
        "report_version": "1.0.0",
        "drill_id": evidence.drill_id,
        "plan_id": plan.plan_id,
        "decision": DrillDecision.PASS.value if not failed else DrillDecision.BLOCKED.value,
        "recovery_authorized": not failed,
        "failed_gates": list(failed),
        "gates": [{"gate": name, "passed": checks[name]} for name in sorted(checks)],
    }
