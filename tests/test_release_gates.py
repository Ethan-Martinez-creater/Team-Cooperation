import json
from pathlib import Path

import pytest

from coifesp_harness.release import (
    CompatibilityMatrix,
    ReleaseDecision,
    ReleaseDocumentError,
    ReleasePlan,
    evaluate_release,
    parse_observations,
)

ROOT = Path(__file__).resolve().parents[1]


def load_inputs():
    matrix = CompatibilityMatrix.from_json(
        (ROOT / "deploy/release/compatibility-matrix.json").read_text(encoding="utf-8")
    )
    plan = ReleasePlan.from_json(
        (ROOT / "deploy/release/release-plan.json").read_text(encoding="utf-8")
    )
    observations = parse_observations(
        (ROOT / "deploy/release/observations-pass.json").read_text(encoding="utf-8")
    )
    return matrix, plan, observations


def test_all_stages_complete_with_machine_readable_report():
    matrix, plan, observations = load_inputs()
    report = evaluate_release(plan, matrix, observations)
    assert report.decision is ReleaseDecision.COMPLETE
    assert report.promotion_authorized
    assert [stage.status for stage in report.stages] == ["passed", "passed"]
    payload = json.loads(report.to_json())
    assert payload["report_version"] == "1.0.0"
    assert payload["compatibility_matrix_digest"] == matrix.canonical_digest()


def test_canary_pass_authorizes_only_next_stage():
    matrix, plan, observations = load_inputs()
    report = evaluate_release(plan, matrix, observations[:1])
    assert report.decision is ReleaseDecision.PROMOTE
    assert report.next_stage == "full-100"
    assert report.promotion_authorized


@pytest.mark.parametrize(
    ("field", "value", "expected_gate"),
    [
        ("errors", 100, "error_budget_burn"),
        ("p95_latency_ms", 999.0, "p95_latency"),
    ],
)
def test_slo_failures_trigger_automatic_rollback(field, value, expected_gate):
    matrix, plan, observations = load_inputs()
    payload = json.loads(
        (ROOT / "deploy/release/observations-pass.json").read_text(encoding="utf-8")
    )
    payload["observations"][0][field] = value
    failed = parse_observations(json.dumps(payload))
    report = evaluate_release(plan, matrix, failed)
    assert report.decision is ReleaseDecision.ROLLBACK
    assert not report.promotion_authorized
    assert any(g.gate == expected_gate and not g.passed for g in report.stages[0].gates)


def test_incomplete_observation_holds_and_digest_drift_rolls_back():
    matrix, plan, observations = load_inputs()
    payload = json.loads(
        (ROOT / "deploy/release/observations-pass.json").read_text(encoding="utf-8")
    )
    payload["observations"][0]["elapsed_seconds"] = 299
    report = evaluate_release(plan, matrix, parse_observations(json.dumps(payload)))
    assert report.decision is ReleaseDecision.HOLD
    payload["observations"][0]["elapsed_seconds"] = 300
    payload["observations"][0]["artifact_digest"] = "sha256:" + "9" * 64
    report = evaluate_release(plan, matrix, parse_observations(json.dumps(payload)))
    assert report.decision is ReleaseDecision.ROLLBACK


def test_observations_must_be_an_ordered_stage_prefix():
    matrix, plan, observations = load_inputs()
    report = evaluate_release(plan, matrix, tuple(reversed(observations)))
    assert report.decision is ReleaseDecision.BLOCKED
    assert report.reasons == ("observation_sequence_gap",)


def test_plan_requires_digest_bound_separation_of_duties():
    raw = json.loads((ROOT / "deploy/release/release-plan.json").read_text(encoding="utf-8"))
    raw["approvals"][1]["principal"] = raw["requested_by"]
    with pytest.raises(ReleaseDocumentError, match="cannot approve"):
        ReleasePlan.from_json(json.dumps(raw))
    raw = json.loads((ROOT / "deploy/release/release-plan.json").read_text(encoding="utf-8"))
    raw["approvals"][0]["artifact_digest"] = "sha256:" + "8" * 64
    with pytest.raises(ReleaseDocumentError, match="exact artifact"):
        ReleasePlan.from_json(json.dumps(raw))
    raw = json.loads((ROOT / "deploy/release/release-plan.json").read_text(encoding="utf-8"))
    raw["artifact"]["produced_by"] = raw["requested_by"]
    with pytest.raises(ReleaseDocumentError, match="must be distinct"):
        ReleasePlan.from_json(json.dumps(raw))


def test_matrix_digest_drift_blocks_before_rollout():
    matrix, plan, observations = load_inputs()
    raw = json.loads((ROOT / "deploy/release/release-plan.json").read_text(encoding="utf-8"))
    bad_digest = "sha256:" + "a" * 64
    raw["compatibility_matrix_digest"] = bad_digest
    for approval in raw["approvals"]:
        approval["compatibility_matrix_digest"] = bad_digest
    drifted = ReleasePlan.from_json(json.dumps(raw))
    report = evaluate_release(drifted, matrix, observations)
    assert report.decision is ReleaseDecision.BLOCKED
    assert report.reasons == ("compatibility_matrix_digest_mismatch",)
