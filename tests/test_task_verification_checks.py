import hashlib

import pytest

from coifesp_harness.errors import IntegrityError, PolicyDenied, ResourceNotFound
from coifesp_harness.verification.checks import evaluate_checks


def _policy(*criteria):
    return {"criteria": list(criteria)}


def _criterion(criterion_id="hash", *, tool="artifact.sha256", required=True):
    return {
        "criterion_id": criterion_id,
        "type": "tool_check",
        "tool": tool,
        "required": required,
    }


def _artifact(content=b"artifact", *, resource_id="resource-1", media_type="text/plain"):
    return {
        "resource_id": resource_id,
        "owner_team_id": "team-a",
        "artifact_id": "artifact-1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": media_type,
        "size_bytes": len(content),
    }


class Reader:
    def __init__(self, chunks_by_digest):
        self.chunks_by_digest = chunks_by_digest
        self.calls = []

    def open_policy_authorized(self, **kwargs):
        self.calls.append(kwargs)
        yield from self.chunks_by_digest[kwargs["sha256"]]


@pytest.mark.parametrize("payload", [b'{"answer": 42}', b'[1, 2, 3]'])
def test_valid_hash_size_and_chunked_json(payload):
    artifact = _artifact(payload, media_type="application/vnd.example+json")
    reader = Reader({artifact["sha256"]: [payload[:2], payload[2:5], payload[5:]]})

    result = evaluate_checks(
        policy=_policy(_criterion(), _criterion("json", tool="artifact.json")),
        artifacts=[artifact],
        artifact_content=reader,
    )

    assert result["status"] == "PASS"
    assert [check["status"] for check in result["checks"]] == ["PASS", "PASS", "PASS"]
    assert result["checks"][0]["evidence_refs"] == ["resource-1"]
    assert reader.calls == [
        {
            "owner_tenant_id": "team-a",
            "sha256": artifact["sha256"],
            "expected_size": len(payload),
        }
    ]


def test_corrupt_chunks_fail_without_raw_content():
    artifact = _artifact(b"expected")
    reader = Reader({artifact["sha256"]: [b"wrong", b" secret material "]})

    result = evaluate_checks(
        policy=_policy(_criterion()), artifacts=[artifact], artifact_content=reader
    )

    assert result["status"] == "FAIL"
    assert result["checks"][0]["status"] == "FAIL"
    assert "secret material" not in repr(result)


@pytest.mark.parametrize("payload", [b'{"duplicate": 1, "duplicate": 2}', b'{"value": NaN}'])
def test_bad_json_duplicate_fields_and_nan_fail(payload):
    artifact = _artifact(payload, media_type="application/json")
    reader = Reader({artifact["sha256"]: [payload]})

    result = evaluate_checks(
        policy=_policy(_criterion("json", tool="artifact.json")),
        artifacts=[artifact],
        artifact_content=reader,
    )

    assert result["status"] == "FAIL"
    assert result["checks"][1]["status"] == "FAIL"


def test_oversize_json_is_pending_with_bounded_check_code():
    payload = b"x" * 1_000_001
    artifact = _artifact(payload, media_type="application/json")
    reader = Reader({artifact["sha256"]: [payload[:700_000], payload[700_000:]]})

    result = evaluate_checks(
        policy=_policy(_criterion("json", tool="artifact.json")),
        artifacts=[artifact],
        artifact_content=reader,
    )

    assert result["status"] == "PENDING"
    assert result["checks"][1]["status"] == "PENDING"
    assert result["checks"][1]["code"] == "json_check_size_limit"


def test_reserved_criterion_id_is_rejected():
    with pytest.raises(ValueError):
        evaluate_checks(
            policy=_policy(_criterion("__artifact_integrity__")),
            artifacts=[],
            artifact_content=None,
        )


def test_unknown_tool_and_required_agent_review_stay_pending():
    result = evaluate_checks(
        policy=_policy(
            _criterion("unknown", tool="vendor.check"),
            {"criterion_id": "review", "type": "agent_review", "required": True},
        ),
        artifacts=[],
        artifact_content=None,
    )

    assert result["status"] == "PENDING"
    assert result["checks"][1]["code"] == "tool_unavailable"
    assert result["checks"][2]["code"] == "agent_review_unavailable"


def test_optional_failure_is_visible_but_does_not_block():
    payload = b"not JSON"
    artifact = _artifact(payload, media_type="application/json")
    reader = Reader({artifact["sha256"]: [payload]})

    result = evaluate_checks(
        policy=_policy(_criterion("hash"), _criterion("json", tool="artifact.json", required=False)),
        artifacts=[artifact],
        artifact_content=reader,
    )

    assert result["status"] == "PASS"
    assert result["checks"][2]["status"] == "FAIL"
    assert result["checks"][2]["required"] is False


@pytest.mark.parametrize("error", [IntegrityError("storage secret"), ResourceNotFound("path secret"), PolicyDenied("tenant secret")])
def test_expected_reader_errors_fail_without_leaking_messages(error):
    artifact = _artifact()

    class FailingReader:
        def open_policy_authorized(self, **_):
            raise error

    result = evaluate_checks(
        policy=_policy(_criterion()), artifacts=[artifact], artifact_content=FailingReader()
    )

    assert result["status"] == "FAIL"
    assert "secret" not in repr(result)


@pytest.mark.parametrize("error", [RuntimeError("offline"), OSError("temporary")])
def test_transient_reader_errors_propagate(error):
    artifact = _artifact()

    class FailingReader:
        def open_policy_authorized(self, **_):
            raise error

    with pytest.raises(type(error)):
        evaluate_checks(
            policy=_policy(_criterion()), artifacts=[artifact], artifact_content=FailingReader()
        )


def test_empty_snapshot_is_allowed_for_hash_check_without_reader():
    result = evaluate_checks(
        policy=_policy(_criterion()), artifacts=[], artifact_content=None
    )

    assert result["status"] == "PASS"
    assert result["checks"][0]["status"] == "PASS"
    assert result["checks"][1]["status"] == "PASS"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: item.update(size_bytes=True),
        lambda item: item.update(sha256="A" * 64),
        lambda item: item.update(media_type="application/*"),
        lambda item: item.update(extra="not allowed"),
    ],
)
def test_malformed_artifact_snapshots_raise_value_error(mutator):
    artifact = _artifact()
    mutator(artifact)
    with pytest.raises(ValueError):
        evaluate_checks(
            policy=_policy(_criterion()), artifacts=[artifact], artifact_content=None
        )


def test_duplicate_resource_ids_raise_value_error():
    first = _artifact(resource_id="same")
    second = _artifact(resource_id="same")
    with pytest.raises(ValueError):
        evaluate_checks(
            policy=_policy(_criterion()), artifacts=[first, second], artifact_content=None
        )
