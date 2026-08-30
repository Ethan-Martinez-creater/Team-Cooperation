import hashlib

import pytest

from coifesp_harness.team_agents.task_contract_models import validate_task_contract
from coifesp_harness.verification.checks import evaluate_checks
from coifesp_harness.verification.human_models import parse_human_decision


def _capability():
    return {
        "tags": ["api"],
        "protocol": "a2a",
        "input_contract_ref": "urn:contract:input:v1",
        "output_contract_ref": "urn:contract:output:v1",
        "verification_policy_ref": "urn:verify:v1",
    }


def _validate(policy):
    return validate_task_contract(
        requested_capability=_capability(),
        input_manifest={"resources": [], "work_nodes": []},
        output_contract={
            "artifact_types": ["application/json"],
            "required": True,
            "max_count": 1,
        },
        verification_policy=policy,
        autonomy_requirement="supervised",
    )


def _criterion(criterion_id, criterion_type, *, required=True, tool=None):
    value = {"criterion_id": criterion_id, "type": criterion_type, "required": required}
    if tool is not None:
        value["tool"] = tool
    return value


def _policy(*criteria):
    return {"criteria": list(criteria)}


def _artifact(content=b"artifact", *, resource_id="resource-1"):
    return {
        "resource_id": resource_id,
        "owner_team_id": "team-a",
        "artifact_id": "artifact-1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "text/plain",
        "size_bytes": len(content),
    }


class Reader:
    def __init__(self, chunks_by_digest):
        self.chunks_by_digest = chunks_by_digest

    def open_policy_authorized(self, **kwargs):
        yield from self.chunks_by_digest[kwargs["sha256"]]


def _decision(**changes):
    value = {
        "decision": "ACCEPT",
        "reason": "Reviewed against the submitted requirements",
        "idempotency_key": "human-review-1",
        "expected_version": 1,
    }
    value.update(changes)
    return value


def test_human_review_criterion_normalizes_without_a_tool_field():
    policy = _policy(_criterion("human-gate", "human_review", required=False))

    normalized = _validate(policy)

    assert normalized["verification_policy"] == {
        "criteria": [
            {"criterion_id": "human-gate", "type": "human_review", "required": False}
        ]
    }


@pytest.mark.parametrize(
    "policy",
    [
        _policy(_criterion("human-gate", "human_review", tool="not-allowed")),
        _policy(_criterion("nested", "composite")),
        _policy(_criterion("unknown", "other")),
        {"criteria": [{"criterion_id": "human-gate", "type": "human_review"}]},
    ],
    ids=["human-tool", "composite-type", "unknown-type", "missing-required"],
)
def test_human_review_policy_rejects_tool_unknown_and_recursive_criteria(policy):
    with pytest.raises(ValueError):
        _validate(policy)


def test_required_tool_agent_and_human_reviews_never_short_circuit_to_pass():
    artifact = _artifact()
    reader = Reader({artifact["sha256"]: [b"artifact"]})
    result = evaluate_checks(
        policy=_policy(
            _criterion("hash", "tool_check", tool="artifact.sha256"),
            _criterion("agent", "agent_review"),
            _criterion("human", "human_review"),
        ),
        artifacts=[artifact],
        artifact_content=reader,
    )

    assert result["status"] == "PENDING"
    assert [check["status"] for check in result["checks"]] == [
        "PASS",
        "PASS",
        "PENDING",
        "PENDING",
    ]
    assert result["checks"][2]["code"] == "agent_review_unavailable"
    assert result["checks"][3]["code"] == "human_review_unavailable"
    assert "tool" not in result["checks"][3]


def test_required_failure_has_priority_over_pending_agent_and_human_reviews():
    artifact = _artifact(b"expected")
    reader = Reader({artifact["sha256"]: [b"wrong"]})
    result = evaluate_checks(
        policy=_policy(
            _criterion("hash", "tool_check", tool="artifact.sha256"),
            _criterion("agent", "agent_review"),
            _criterion("human", "human_review"),
        ),
        artifacts=[artifact],
        artifact_content=reader,
    )

    assert result["status"] == "FAIL"
    assert result["checks"][0]["status"] == "FAIL"
    assert result["checks"][2]["status"] == "PENDING"
    assert result["checks"][3]["status"] == "PENDING"


def test_optional_human_review_is_visible_but_does_not_block():
    result = evaluate_checks(
        policy=_policy(_criterion("human", "human_review", required=False)),
        artifacts=[],
        artifact_content=None,
    )

    assert result["status"] == "PASS"
    assert result["checks"][1] == {
        "criterion_id": "human",
        "type": "human_review",
        "required": False,
        "status": "PENDING",
        "code": "human_review_unavailable",
        "evidence_refs": [],
    }


@pytest.mark.parametrize(
    ("decision", "expected_version"),
    [("ACCEPT", 1), ("REJECT", 7), ("ACCEPT", 2**64)],
    ids=["accept", "reject", "large-version"],
)
def test_valid_human_decisions_are_normalized_without_mutation(decision, expected_version):
    value = _decision(
        decision=decision,
        reason="  Keep this source text, including surrounding spaces.  ",
        expected_version=expected_version,
    )

    result = parse_human_decision(value)

    assert result == value
    assert result is not value
    assert result["reason"] == value["reason"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"decision": "ACCEPT", "reason": "ok", "idempotency_key": "key"},
        {**_decision(), "unexpected": "field"},
        {**_decision(), "decision": "accept"},
        {**_decision(), "decision": "APPROVE"},
        {**_decision(), "decision": True},
        {**_decision(), "reason": ""},
        {**_decision(), "reason": "   "},
        {**_decision(), "reason": None},
        {**_decision(), "reason": "x" * 2_001},
        {**_decision(), "reason": "\ud800"},
        {**_decision(), "idempotency_key": ""},
        {**_decision(), "idempotency_key": ".starts-with-punctuation"},
        {**_decision(), "idempotency_key": "contains/slash"},
        {**_decision(), "idempotency_key": "x" * 129},
        {**_decision(), "idempotency_key": None},
        {**_decision(), "expected_version": 0},
        {**_decision(), "expected_version": -1},
        {**_decision(), "expected_version": True},
        {**_decision(), "expected_version": 1.0},
        {**_decision(), "expected_version": None},
    ],
)
def test_human_decision_rejects_invalid_or_ambiguous_values(value):
    with pytest.raises(ValueError):
        parse_human_decision(value)


def test_idempotency_key_accepts_the_full_bounded_character_set():
    value = _decision(idempotency_key="A._:-" + "z" * 123)

    assert parse_human_decision(value)["idempotency_key"] == value["idempotency_key"]
