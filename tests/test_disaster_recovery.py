import json
from pathlib import Path

import pytest

from coifesp_harness.disaster_recovery import (
    DisasterRecoveryDocumentError,
    DisasterRecoveryPlan,
    DrillEvidence,
    evaluate_drill,
)

ROOT = Path(__file__).resolve().parents[1]


def seal_evidence(evidence_raw):
    import hashlib
    binding = {key: value for key, value in evidence_raw.items() if key != "evidence_digest"}
    evidence_raw["evidence_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return evidence_raw


def documents():
    plan_path = ROOT / "deploy/disaster-recovery/recovery-plan.json"
    plan_raw = json.loads(plan_path.read_text(encoding="utf-8"))
    binding = {key: value for key, value in plan_raw.items() if key != "approvals"}
    import hashlib

    digest = "sha256:" + hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    for approval in plan_raw["approvals"]:
        approval["plan_digest"] = digest
    evidence_raw = json.loads(
        (ROOT / "deploy/disaster-recovery/drill-evidence-pass.json").read_text(encoding="utf-8")
    )
    evidence_raw["plan_digest"] = digest
    seal_evidence(evidence_raw)
    return plan_raw, evidence_raw


def parsed():
    plan_raw, evidence_raw = documents()
    return DisasterRecoveryPlan.from_json(json.dumps(plan_raw)), DrillEvidence.from_json(json.dumps(evidence_raw))


def test_regional_outage_drill_passes_with_deterministic_non_sensitive_report():
    plan, evidence = parsed()
    report = evaluate_drill(plan, evidence)
    assert report["decision"] == "pass"
    assert report["recovery_authorized"] is True
    assert report["failed_gates"] == []
    assert [entry["gate"] for entry in report["gates"]] == sorted(entry["gate"] for entry in report["gates"])
    assert "backup-key-v1" not in json.dumps(report)


@pytest.mark.parametrize("field", ["primary_fenced", "data_consistent", "audit_chain_verified", "queues_replayed_idempotently", "dead_letters_reviewed"])
def test_safety_evidence_failure_fails_closed(field):
    plan_raw, evidence_raw = documents()
    evidence_raw[field] = False
    seal_evidence(evidence_raw)
    report = evaluate_drill(DisasterRecoveryPlan.from_json(json.dumps(plan_raw)), DrillEvidence.from_json(json.dumps(evidence_raw)))
    assert report["decision"] == "blocked"
    assert not report["recovery_authorized"]


def test_identity_and_rto_drift_fail_closed():
    plan, evidence = parsed()
    raw = evidence.__dict__ if False else documents()[1]
    raw["restore_seconds"] = 1801
    seal_evidence(raw)
    report = evaluate_drill(plan, DrillEvidence.from_json(json.dumps(raw)))
    assert report["failed_gates"] == ["rto"]
    raw = documents()[1]
    raw["recovered_lease_epoch"] = 41
    seal_evidence(raw)
    report = evaluate_drill(plan, DrillEvidence.from_json(json.dumps(raw)))
    assert report["failed_gates"] == ["lease_fencing"]


def test_rpo_and_evidence_digest_are_enforced():
    plan, _ = parsed()
    raw = documents()[1]
    raw["recovery_point_lag_seconds"] = 301
    seal_evidence(raw)
    assert evaluate_drill(plan, DrillEvidence.from_json(json.dumps(raw)))["failed_gates"] == ["rpo"]
    raw["data_consistent"] = False
    with pytest.raises(DisasterRecoveryDocumentError, match="evidence_digest"):
        DrillEvidence.from_json(json.dumps(raw))


def test_plan_rejects_approval_and_runbook_weakening():
    plan_raw, _ = documents()
    plan_raw["approvals"][1]["principal"] = plan_raw["approvals"][0]["principal"]
    with pytest.raises(DisasterRecoveryDocumentError, match="separated"):
        DisasterRecoveryPlan.from_json(json.dumps(plan_raw))
    plan_raw, _ = documents()
    plan_raw["recovery_sequence"] = plan_raw["recovery_sequence"][1:]
    with pytest.raises(DisasterRecoveryDocumentError, match="mandatory"):
        DisasterRecoveryPlan.from_json(json.dumps(plan_raw))
