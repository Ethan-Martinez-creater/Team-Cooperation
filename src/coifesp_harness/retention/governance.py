"""Versioned, fail-closed retention planning and receipt-only execution.

This module never opens or deletes a storage object.  An application supplies a
``StorageExecutor`` which is responsible for carrying out an approved action and
returning a durable receipt.  Manifests and reports intentionally retain only a
stable storage reference and a content digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol, Sequence

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STABLE_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s?#]+$")


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a safe stable identifier")


def _aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class RetentionCategory(str, Enum):
    ARTIFACT = "artifact"
    AUDIT_LOG = "audit_log"
    MEMORY = "memory"
    EVALUATION_REPORT = "evaluation_report"
    RELEASE_REPORT = "release_report"


class RetentionAction(str, Enum):
    RETAIN = "retain"
    DELETE = "delete"
    CRYPTO_SHRED = "crypto_shred"
    ARCHIVE = "archive"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """An exact category/classification/compartment rule; no wildcard matching."""

    category: RetentionCategory
    classification: str
    compartment: str
    minimum_days: int
    maximum_days: int
    expiry_action: RetentionAction

    def __post_init__(self) -> None:
        _identifier(self.classification, "classification")
        _identifier(self.compartment, "compartment")
        if type(self.minimum_days) is not int or type(self.maximum_days) is not int:
            raise ValueError("retention durations must be integer days")
        if self.minimum_days < 0 or self.maximum_days < self.minimum_days:
            raise ValueError("retention rule duration bounds are invalid")
        if self.expiry_action not in {
            RetentionAction.DELETE,
            RetentionAction.CRYPTO_SHRED,
            RetentionAction.ARCHIVE,
        }:
            raise ValueError("expiry_action must be delete, crypto_shred, or archive")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """A tenant-scoped policy.  Version 1.0.0 is deliberately the only schema."""

    policy_id: str
    version: str
    tenant_id: str
    authored_by: str
    approved_by: str
    rules: tuple[RetentionRule, ...]
    format_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _identifier(self.tenant_id, "tenant_id")
        _identifier(self.authored_by, "authored_by")
        _identifier(self.approved_by, "approved_by")
        if self.authored_by == self.approved_by:
            raise ValueError("policy author and approver must be different principals")
        if self.format_version != "1.0.0" or _SEMVER.fullmatch(self.version) is None:
            raise ValueError("policy requires stable version 1.0.0 schema and semantic version")
        rules = tuple(self.rules)
        if not rules or any(not isinstance(rule, RetentionRule) for rule in rules):
            raise ValueError("policy requires one or more retention rules")
        keys = [(r.category, r.classification, r.compartment) for r in rules]
        if len(keys) != len(set(keys)):
            raise ValueError("policy rules must have unique category/classification/compartment keys")
        object.__setattr__(self, "rules", rules)

    def rule_for(self, target: "RetentionTarget") -> RetentionRule | None:
        for rule in self.rules:
            if (rule.category, rule.classification, rule.compartment) == (
                target.category,
                target.classification,
                target.compartment,
            ):
                return rule
        return None


@dataclass(frozen=True, slots=True)
class RetentionTarget:
    """A minimal storage manifest, deliberately excluding object payload/metadata."""

    reference: str
    digest: str
    tenant_id: str
    category: RetentionCategory
    classification: str
    compartment: str
    created_at: datetime
    committed_retain_until: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or _STABLE_REFERENCE.fullmatch(self.reference) is None:
            raise ValueError("reference must be a stable scheme-qualified reference without query data")
        if not isinstance(self.digest, str) or _SHA256.fullmatch(self.digest) is None:
            raise ValueError("digest must be a lowercase SHA-256 digest")
        for field_name in ("tenant_id", "classification", "compartment"):
            _identifier(getattr(self, field_name), field_name)
        _aware(self.created_at, "created_at")
        _aware(self.committed_retain_until, "committed_retain_until")
        if self.committed_retain_until < self.created_at:
            raise ValueError("committed_retain_until cannot precede creation")


@dataclass(frozen=True, slots=True)
class LegalHold:
    target_reference: str
    tenant_id: str
    compartment: str
    hold_id: str
    placed_by: str
    active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.target_reference, str) or _STABLE_REFERENCE.fullmatch(self.target_reference) is None:
            raise ValueError("hold target must be a stable scheme-qualified reference")
        for field_name in ("tenant_id", "compartment", "hold_id", "placed_by"):
            _identifier(getattr(self, field_name), field_name)
        if type(self.active) is not bool:
            raise ValueError("active must be a boolean")


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    reference: str
    digest: str
    action: RetentionAction
    reason: str
    effective_retain_until: datetime
    policy_id: str
    policy_version: str

    def canonical(self) -> dict[str, str]:
        return {
            "action": self.action.value,
            "digest": self.digest,
            "effective_retain_until": self.effective_retain_until.astimezone(UTC).isoformat(),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason": self.reason,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class RetentionBatch:
    batch_id: str
    tenant_id: str
    requested_by: str
    execution_operator: str
    policy: RetentionPolicy
    targets: tuple[RetentionTarget, ...]
    as_of: datetime
    dry_run: bool = True

    def __post_init__(self) -> None:
        for field_name in ("batch_id", "tenant_id", "requested_by", "execution_operator"):
            _identifier(getattr(self, field_name), field_name)
        _aware(self.as_of, "as_of")
        if self.tenant_id != self.policy.tenant_id:
            raise ValueError("batch and policy tenant must match")
        if self.execution_operator in {self.policy.authored_by, self.policy.approved_by}:
            raise ValueError("execution operator must be separated from policy author and approver")
        if self.execution_operator == self.requested_by:
            raise ValueError("execution operator must be separated from batch requester")
        targets = tuple(self.targets)
        if not targets:
            raise ValueError("batch requires at least one target")
        if any(target.tenant_id != self.tenant_id for target in targets):
            raise ValueError("cross-tenant target in retention batch")
        references = [target.reference for target in targets]
        if len(references) != len(set(references)):
            raise ValueError("batch target references must be unique")
        object.__setattr__(self, "targets", tuple(sorted(targets, key=lambda item: item.reference)))

    def fingerprint(self, holds: Sequence[LegalHold] = ()) -> str:
        payload = {
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "batch_id": self.batch_id,
            "dry_run": self.dry_run,
            "execution_operator": self.execution_operator,
            "policy": {
                "approved_by": self.policy.approved_by,
                "authored_by": self.policy.authored_by,
                "format_version": self.policy.format_version,
                "id": self.policy.policy_id,
                "rules": [
                    [rule.category.value, rule.classification, rule.compartment,
                     rule.minimum_days, rule.maximum_days, rule.expiry_action.value]
                    for rule in self.policy.rules
                ],
                "tenant_id": self.policy.tenant_id,
                "version": self.policy.version,
            },
            "requested_by": self.requested_by,
            "targets": [
                [
                    target.reference,
                    target.digest,
                    target.category.value,
                    target.classification,
                    target.compartment,
                    target.created_at.astimezone(UTC).isoformat(),
                    target.committed_retain_until.astimezone(UTC).isoformat(),
                ]
                for target in self.targets
            ],
            "tenant_id": self.tenant_id,
            "holds": sorted(
                [hold.target_reference, hold.tenant_id, hold.compartment, hold.hold_id,
                 hold.placed_by, hold.active]
                for hold in holds
            ),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StorageReceipt:
    reference: str
    action: RetentionAction
    idempotency_key: str
    receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or _STABLE_REFERENCE.fullmatch(self.reference) is None:
            raise ValueError("receipt reference is invalid")
        if self.action not in {RetentionAction.DELETE, RetentionAction.CRYPTO_SHRED, RetentionAction.ARCHIVE}:
            raise ValueError("receipt action is invalid")
        if not isinstance(self.idempotency_key, str) or _SHA256.fullmatch(self.idempotency_key) is None:
            raise ValueError("receipt idempotency key must be a SHA-256 digest")
        _identifier(self.receipt_id, "receipt_id")


class StorageExecutor(Protocol):
    """External storage boundary. Implementations must provide idempotent actions."""

    def execute(self, decision: RetentionDecision, *, idempotency_key: str) -> StorageReceipt: ...


@dataclass(frozen=True, slots=True)
class BatchItemReport:
    reference: str
    digest: str
    action: RetentionAction
    status: str
    reason: str
    receipt_id: str | None = None


@dataclass(frozen=True, slots=True)
class BatchReport:
    batch_id: str
    fingerprint: str
    dry_run: bool
    policy_id: str
    policy_version: str
    items: tuple[BatchItemReport, ...]

    @property
    def complete(self) -> bool:
        return all(item.status != "failed" for item in self.items)

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "batch_id": self.batch_id,
                "dry_run": self.dry_run,
                "fingerprint": self.fingerprint,
                "items": [
                    {
                        "action": item.action.value,
                        "digest": item.digest,
                        "reason": item.reason,
                        "receipt_id": item.receipt_id,
                        "reference": item.reference,
                        "status": item.status,
                    }
                    for item in self.items
                ],
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class RetentionGovernance:
    """In-memory coordinator with replay-safe batches and retryable receipts."""

    def __init__(self) -> None:
        self._fingerprints: dict[str, str] = {}
        self._receipts: dict[tuple[str, str], StorageReceipt] = {}

    def decide(
        self, policy: RetentionPolicy, target: RetentionTarget, *, as_of: datetime,
        holds: Sequence[LegalHold] = (),
    ) -> RetentionDecision:
        _aware(as_of, "as_of")
        if policy.tenant_id != target.tenant_id:
            return self._deny(policy, target, "cross_tenant_access_denied")
        rule = policy.rule_for(target)
        if rule is None:
            return self._deny(policy, target, "no_matching_policy_rule")
        minimum_until = target.created_at + timedelta(days=rule.minimum_days)
        maximum_until = target.created_at + timedelta(days=rule.maximum_days)
        # A historic commitment that exceeds a new policy maximum is preserved;
        # compliance retention is never shortened by a later policy update.
        effective_until = max(target.committed_retain_until, minimum_until)
        if target.committed_retain_until < minimum_until:
            return self._deny(policy, target, "committed_retention_below_policy_minimum", effective_until)
        if any(
            hold.active
            and hold.target_reference == target.reference
            and hold.tenant_id == target.tenant_id
            and hold.compartment == target.compartment
            for hold in holds
        ):
            return RetentionDecision(target.reference, target.digest, RetentionAction.RETAIN, "legal_hold", effective_until, policy.policy_id, policy.version)
        if target.committed_retain_until > maximum_until:
            return RetentionDecision(target.reference, target.digest, RetentionAction.RETAIN, "historic_commitment_preserved", effective_until, policy.policy_id, policy.version)
        if as_of < effective_until:
            return RetentionDecision(target.reference, target.digest, RetentionAction.RETAIN, "minimum_or_committed_retention_active", effective_until, policy.policy_id, policy.version)
        return RetentionDecision(target.reference, target.digest, rule.expiry_action, "retention_expired", effective_until, policy.policy_id, policy.version)

    @staticmethod
    def _deny(policy: RetentionPolicy, target: RetentionTarget, reason: str, effective_until: datetime | None = None) -> RetentionDecision:
        return RetentionDecision(target.reference, target.digest, RetentionAction.DENY, reason, effective_until or target.committed_retain_until, policy.policy_id, policy.version)

    def run(self, batch: RetentionBatch, *, executor: StorageExecutor | None = None, holds: Sequence[LegalHold] = ()) -> BatchReport:
        fingerprint = batch.fingerprint(holds)
        previous = self._fingerprints.setdefault(batch.batch_id, fingerprint)
        if previous != fingerprint:
            raise ValueError("batch_id was already used with different immutable input")
        if not batch.dry_run and executor is None:
            raise ValueError("an executor is required for a non-dry-run batch")

        reports: list[BatchItemReport] = []
        for target in batch.targets:
            decision = self.decide(batch.policy, target, as_of=batch.as_of, holds=holds)
            key = (batch.batch_id, target.reference)
            if batch.dry_run or decision.action in {RetentionAction.RETAIN, RetentionAction.DENY}:
                reports.append(BatchItemReport(target.reference, target.digest, decision.action, "planned", decision.reason))
                continue
            receipt = self._receipts.get(key)
            if receipt is not None:
                reports.append(BatchItemReport(target.reference, target.digest, decision.action, "executed", decision.reason, receipt.receipt_id))
                continue
            idempotency_key = hashlib.sha256(f"{fingerprint}:{target.reference}".encode()).hexdigest()
            try:
                receipt = executor.execute(decision, idempotency_key=idempotency_key)
                if receipt.reference != target.reference or receipt.action != decision.action or receipt.idempotency_key != idempotency_key:
                    raise ValueError("storage executor returned a receipt for a different operation")
                self._receipts[key] = receipt
                reports.append(BatchItemReport(target.reference, target.digest, decision.action, "executed", decision.reason, receipt.receipt_id))
            except Exception as exc:  # executor failures remain visible and retryable
                reports.append(BatchItemReport(target.reference, target.digest, decision.action, "failed", type(exc).__name__))
        return BatchReport(batch.batch_id, fingerprint, batch.dry_run, batch.policy.policy_id, batch.policy.version, tuple(reports))


class FakeStorageExecutor:
    """Test-only receipt ledger; it deliberately performs no storage mutation."""

    def __init__(self, fail_references: frozenset[str] = frozenset()) -> None:
        self.fail_references = fail_references
        self.calls: list[tuple[str, RetentionAction, str]] = []
        self._receipts: dict[str, StorageReceipt] = {}

    def execute(self, decision: RetentionDecision, *, idempotency_key: str) -> StorageReceipt:
        if decision.reference in self.fail_references:
            raise RuntimeError("simulated_storage_failure")
        existing = self._receipts.get(idempotency_key)
        if existing is not None:
            return existing
        self.calls.append((decision.reference, decision.action, idempotency_key))
        receipt = StorageReceipt(decision.reference, decision.action, idempotency_key, "fake-" + idempotency_key[:24])
        self._receipts[idempotency_key] = receipt
        return receipt
