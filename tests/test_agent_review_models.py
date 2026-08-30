import json
import math

import pytest

from coifesp_harness.verification.review_models import parse_review_result

SCHEMA = "coifesp.verification-result.v1"


def payload(**changes):
    value = {
        "schema": SCHEMA,
        "passed": True,
        "findings": [],
        "required_changes": [],
        "evidence_refs": ["artifact-1"],
    }
    value.update(changes)
    return value


def content(value, *, ensure_ascii=True):
    return json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":"))


@pytest.mark.parametrize(
    ("allowed_refs", "value"),
    [
        pytest.param(
            ["artifact-1", "artifact-2"],
            payload(findings=["minor non-blocking note"]),
            id="successful-review-with-evidence",
        ),
        pytest.param(
            ["artifact-1", "artifact-2"],
            payload(
                passed=False,
                findings=["refresh-token rotation is missing"],
                required_changes=["Implement refresh-token rotation before release"],
                evidence_refs=["artifact-2"],
            ),
            id="failed-review-with-findings-and-changes",
        ),
        pytest.param(
            [],
            payload(evidence_refs=[]),
            id="optional-empty-evidence-manifest",
        ),
    ],
)
def test_valid_review_results_are_normalized_to_fresh_data(allowed_refs, value):
    result = parse_review_result(content(value), evidence_refs=allowed_refs)
    assert result == value

    result["findings"].append("local mutation")
    result["evidence_refs"].clear()
    reparsed = parse_review_result(content(value), evidence_refs=allowed_refs)
    assert reparsed == value


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "review complete",
        "```json\n{}\n```",
        "[]",
        "null",
        "{} {}",
        content(payload()) + " prose",
    ],
    ids=[
        "empty",
        "whitespace",
        "prose",
        "markdown-fence",
        "array",
        "null",
        "two-json-values",
        "trailing-prose",
    ],
)
def test_parser_rejects_noncanonical_json_shapes(raw):
    with pytest.raises(ValueError):
        parse_review_result(raw, evidence_refs=["artifact-1"])


@pytest.mark.parametrize(
    "value",
    [
        {"schema": SCHEMA},
        {**payload(), "extra": "not allowed"},
        {**payload(), "schema": "other"},
        {**payload(), "passed": 1},
        {**payload(), "passed": "true"},
        {**payload(), "findings": "none"},
        {**payload(), "required_changes": "none"},
        {**payload(), "evidence_refs": "artifact-1"},
        {**payload(), "findings": [" "]},
        {**payload(), "required_changes": [""]},
        {**payload(), "findings": [False]},
        {**payload(), "required_changes": [None]},
        {**payload(), "evidence_refs": [False]},
        {**payload(), "findings": ["x" * 2_001]},
        {**payload(), "required_changes": ["x" * 2_001]},
        {**payload(), "findings": ["same", "same"]},
        {**payload(), "required_changes": ["same", "same"]},
        {**payload(), "evidence_refs": ["artifact-1", "artifact-1"]},
        {**payload(), "evidence_refs": ["artifact-unknown"]},
        {**payload(), "passed": True, "required_changes": ["change"]},
        {**payload(), "passed": True, "evidence_refs": []},
        {
            **payload(),
            "passed": False,
            "findings": [],
            "required_changes": ["change"],
        },
        {
            **payload(),
            "passed": False,
            "findings": ["finding"],
            "required_changes": [],
        },
    ],
)
def test_parser_rejects_invalid_field_values(value):
    with pytest.raises(ValueError):
        parse_review_result(content(value), evidence_refs=["artifact-1"])


@pytest.mark.parametrize(
    "field",
    ["findings", "required_changes", "evidence_refs"],
)
def test_each_result_list_has_a_32_item_limit(field):
    values = [f"item-{index}" for index in range(33)]
    value = payload(**{field: values})
    allowed = values if field == "evidence_refs" else ["artifact-1"]
    with pytest.raises(ValueError):
        parse_review_result(content(value), evidence_refs=allowed)


@pytest.mark.parametrize(
    "constant",
    [math.nan, math.inf, -math.inf],
    ids=["nan", "infinity", "negative-infinity"],
)
def test_parser_rejects_nonfinite_numbers(constant):
    value = payload(findings=[constant])
    with pytest.raises(ValueError):
        parse_review_result(content(value), evidence_refs=["artifact-1"])


def test_parser_rejects_duplicate_json_keys_at_nested_depth():
    raw = (
        '{"schema":"coifesp.verification-result.v1",'
        '"passed":true,"findings":[],"required_changes":[],'
        '"evidence_refs":["artifact-1"],'
        '"extra":{"first":1,"first":2}}'
    )
    with pytest.raises(ValueError):
        parse_review_result(raw, evidence_refs=["artifact-1"])


@pytest.mark.parametrize(
    "ensure_ascii",
    [False, True],
    ids=["raw-surrogate", "escaped-surrogate"],
)
def test_parser_rejects_invalid_unicode(ensure_ascii):
    value = payload(findings=["\ud800"])
    with pytest.raises(ValueError):
        parse_review_result(
            content(value, ensure_ascii=ensure_ascii), evidence_refs=["artifact-1"]
        )


def test_parser_rejects_json_larger_than_128_kibibytes():
    with pytest.raises(ValueError):
        parse_review_result("x" * (128 * 1024 + 1), evidence_refs=[])


@pytest.mark.parametrize(
    "allowed_refs",
    [
        None,
        ("artifact-1",),
        ["artifact-1", "artifact-1"],
        [""],
        ["x" * 2_001],
    ],
    ids=["none", "tuple", "duplicate", "empty-text", "oversized-text"],
)
def test_manifest_evidence_refs_are_bounded_and_detached(allowed_refs):
    with pytest.raises(ValueError):
        parse_review_result(content(payload()), evidence_refs=allowed_refs)
