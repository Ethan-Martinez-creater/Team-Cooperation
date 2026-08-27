import json

import pytest

from coifesp_harness.release import (
    CompatibilityMatrix,
    ReleaseDocumentError,
    StableVersion,
)


def matrix_document() -> dict[str, object]:
    v1 = {
        "db_schema": "1.0.0",
        "control_plane": "1.0.0",
        "worker": "1.0.0",
        "checkpoint": "1.0.0",
        "envelope": "1.0.0",
        "audit": "1.0.0",
    }
    v2 = dict(v1)
    v2["worker"] = "1.1.0"
    modes = {name: "unchanged" for name in v1}
    modes["worker"] = "bidirectional"
    return {
        "format_version": "1.0.0",
        "default_policy": "deny",
        "excluded_frozen_protocols": ["mcp", "a2a"],
        "releases": [
            {"release_version": "1.0.0", "interfaces": v1},
            {"release_version": "1.1.0", "interfaces": v2},
        ],
        "transitions": [
            {
                "from_release": "1.0.0",
                "to_release": "1.1.0",
                "strategy": "canary",
                "rollback_safe": True,
                "interfaces": modes,
            }
        ],
    }


def test_matrix_allows_only_explicit_internal_transition_and_has_stable_digest():
    raw = matrix_document()
    matrix = CompatibilityMatrix.from_json(json.dumps(raw))
    allowed = matrix.assess(
        StableVersion.parse("1.0.0", "source"), StableVersion.parse("1.1.0", "target"), "canary"
    )
    assert allowed.allowed and allowed.rollback_safe
    assert (
        matrix.canonical_digest()
        == CompatibilityMatrix.from_json(json.dumps(raw, indent=2)).canonical_digest()
    )


def test_unknown_release_and_unlisted_strategy_are_denied():
    matrix = CompatibilityMatrix.from_json(json.dumps(matrix_document()))
    assert not matrix.assess(
        StableVersion.parse("0.9.0", "source"), StableVersion.parse("1.1.0", "target"), "canary"
    ).allowed
    assert not matrix.assess(
        StableVersion.parse("1.0.0", "source"), StableVersion.parse("1.1.0", "target"), "blue-green"
    ).allowed


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc.update(default_policy="allow"), "default_policy"),
        (lambda doc: doc.update(excluded_frozen_protocols=["mcp"]), "mcp and a2a"),
        (lambda doc: doc.update(excluded_frozen_protocols=["mcp", {}]), "mcp and a2a"),
        (lambda doc: doc["releases"][0]["interfaces"].pop("audit"), "missing fields"),
        (lambda doc: doc["releases"][0]["interfaces"].update(mcp="1.0.0"), "unknown fields"),
        (lambda doc: doc["transitions"][0]["interfaces"].update(worker="unchanged"), "contradicts"),
    ],
)
def test_matrix_is_strict_and_fail_closed(mutation, message):
    raw = matrix_document()
    mutation(raw)
    with pytest.raises(ReleaseDocumentError, match=message):
        CompatibilityMatrix.from_json(json.dumps(raw))


def test_duplicate_keys_and_prereleases_are_rejected():
    with pytest.raises(ReleaseDocumentError, match="duplicate JSON key"):
        CompatibilityMatrix.from_json('{"format_version":"1.0.0","format_version":"1.0.0"}')
    raw = matrix_document()
    raw["releases"][0]["release_version"] = "1.0.0-rc.1"
    with pytest.raises(ReleaseDocumentError, match="stable strict semantic version"):
        CompatibilityMatrix.from_json(json.dumps(raw))
