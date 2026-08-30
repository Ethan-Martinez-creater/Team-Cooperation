import json

import pytest

from coifesp_harness.team_agents.task_result_models import (
    parse_task_result,
    validate_task_artifact_types,
)

CONTRACT = {"artifact_types": ["text/plain"], "required": True, "max_count": 2}


def payload(**values):
    return {
        "schema": "coifesp.task-output.v1",
        "artifact_refs": ["resource-1"],
        "summary": "done",
        **values,
    }


def parse(value, contract=None):
    return parse_task_result(json.dumps(value), output_contract=contract or CONTRACT)


def test_valid_result_defaults_and_concrete_artifact_types():
    result = parse(payload())
    assert result["known_limitations"] == []
    validate_task_artifact_types(
        result, media_types={"resource-1": "Text/Plain"}, output_contract=CONTRACT
    )


def test_schema_contract_requires_explicit_known_limitations():
    contract = {
        "schema": "coifesp.task-output.v1",
        "required_fields": ["artifact_refs", "summary", "known_limitations"],
    }
    with pytest.raises(ValueError):
        parse(payload(), contract)
    assert parse(payload(known_limitations=[]), contract)["known_limitations"] == []


def test_only_explicit_optional_artifacts_allow_empty_refs():
    assert parse(payload(artifact_refs=[]), {**CONTRACT, "required": False})["artifact_refs"] == []
    with pytest.raises(ValueError):
        parse(payload(artifact_refs=[]))


@pytest.mark.parametrize(
    "content",
    [
        "done",
        "```json\n{}\n```",
        "[]",
        "null",
        "{} {}",
        '{"schema":"coifesp.task-output.v1","schema":"bad"}',
        '{"schema":"coifesp.task-output.v1","artifact_refs":[],"summary":NaN}',
        pytest.param("x" * 1_000_001, id="oversized-json"),
    ],
)
def test_protocol_rejects_noncanonical_json(content):
    with pytest.raises(ValueError):
        parse_task_result(content, output_contract=CONTRACT)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "other"},
        {"extra": "hidden"},
        {"summary": " "},
        {"summary": False},
        {"summary": "x" * 20_001},
        {"artifact_refs": "resource-1"},
        {"artifact_refs": [True]},
        {"artifact_refs": ["resource-1", "resource-1"]},
        {"artifact_refs": ["C:/secret.txt"]},
        {"artifact_refs": ["https://example.test/a"]},
        {"artifact_refs": ["id-1", "id-2", "id-3"]},
        {"known_limitations": "none"},
        {"known_limitations": ["one", "one"]},
        {"known_limitations": [False]},
        {"known_limitations": ["x" * 2001]},
    ],
)
def test_protocol_rejects_invalid_fields(changes):
    with pytest.raises(ValueError):
        parse(payload(**changes))


@pytest.mark.parametrize(
    "media_types",
    [
        {},
        {"other": "text/plain"},
        {"resource-1": "image/png"},
        {"resource-1": "text/plain", "extra": "text/plain"},
        {"resource-1": "text/plain;charset=utf-8"},
        {"resource-1": "*/*"},
        {"resource-1": None},
    ],
)
def test_artifact_mapping_must_be_exact_and_of_contracted_types(media_types):
    with pytest.raises(ValueError):
        validate_task_artifact_types(
            parse(payload()), media_types=media_types, output_contract=CONTRACT
        )


@pytest.mark.parametrize(
    "contract", [{}, {**CONTRACT, "max_count": True}, {**CONTRACT, "artifact_types": ["text/*"]}]
)
def test_bad_persisted_contract_does_not_bypass_validation(contract):
    with pytest.raises(ValueError):
        parse_task_result(json.dumps(payload()), output_contract=contract)


def test_parse_returns_detached_data_and_never_mutates_contract():
    original = payload(known_limitations=["constraint"])
    contract = {**CONTRACT, "artifact_types": list(CONTRACT["artifact_types"])}
    result = parse(original, contract)
    result["artifact_refs"].append("another")
    result["known_limitations"].clear()
    assert original["artifact_refs"] == ["resource-1"]
    assert original["known_limitations"] == ["constraint"]
    assert contract == CONTRACT
