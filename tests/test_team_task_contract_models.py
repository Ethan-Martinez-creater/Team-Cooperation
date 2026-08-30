import json

import pytest

from coifesp_harness.security import Classification
from coifesp_harness.team_agents.task_contract_models import validate_task_contract


def _capability(**overrides):
    value = {
        "tags": ["api", "review"],
        "protocol": "a2a",
        "input_contract_ref": "urn:contract:input:v1",
        "output_contract_ref": "urn:contract:output:v1",
        "verification_policy_ref": "urn:verify:v1",
    }
    value.update(overrides)
    return value


def _manifest(**overrides):
    value = {
        "resources": [
            {"resource_id": "resource-api-spec", "required": True, "mode": "project_readonly"}
        ],
        "work_nodes": ["requirement-auth-1", "decision-api-v2"],
    }
    value.update(overrides)
    return value


def _artifact_output(**overrides):
    value = {
        "artifact_types": ["application/openapi+yaml"],
        "required": True,
        "max_count": 2,
    }
    value.update(overrides)
    return value


def _schema_output(**overrides):
    value = {
        "schema": "coifesp.task-output.v1",
        "required_fields": ["artifact_refs", "summary", "known_limitations"],
    }
    value.update(overrides)
    return value


def _verification(**overrides):
    value = {
        "criteria": [
            {
                "criterion_id": "schema-valid",
                "type": "tool_check",
                "required": True,
                "tool": "openapi.validate",
            },
            {"criterion_id": "security-review", "type": "agent_review", "required": True},
        ]
    }
    value.update(overrides)
    return value


def _validate(*, output=None, **kwargs):
    return validate_task_contract(
        requested_capability=kwargs.pop("requested_capability", _capability()),
        input_manifest=kwargs.pop("input_manifest", _manifest()),
        output_contract=output if output is not None else _artifact_output(),
        verification_policy=kwargs.pop("verification_policy", _verification()),
        autonomy_requirement=kwargs.pop("autonomy_requirement", "supervised"),
        **kwargs,
    )


def test_valid_artifact_contract_has_defaults_and_json_wire_classification():
    result = _validate()

    assert set(result) == {
        "requested_capability",
        "input_manifest",
        "output_contract",
        "verification_policy",
        "autonomy_requirement",
    }
    capability = result["requested_capability"]
    assert capability["input_classification"] == 1
    assert type(capability["input_classification"]) is int
    assert capability["compartments"] == []
    assert capability["residency"] == []
    assert capability["slots"] == 1
    assert result["input_manifest"] == _manifest()
    assert json.loads(json.dumps(result)) == result


def test_valid_schema_contract_roundtrips_real_classification_and_preserves_shape():
    result = _validate(
        output=_schema_output(),
        requested_capability=_capability(
            input_classification=Classification.CONFIDENTIAL,
            compartments=["program-1"],
            residency=["cn-east"],
            slots=3,
        ),
        input_manifest={"resources": [], "work_nodes": []},
        autonomy_requirement="assisted",
    )

    capability = result["requested_capability"]
    assert capability["input_classification"] == int(Classification.CONFIDENTIAL)
    assert Classification(capability["input_classification"]) is Classification.CONFIDENTIAL
    assert result["output_contract"] == _schema_output()


def test_valid_merged_output_contract_is_supported():
    result = _validate(output={**_artifact_output(), **_schema_output()})
    assert result["output_contract"] == {**_artifact_output(), **_schema_output()}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tags", []),
        ("tags", ["api", "api"]),
        ("tags", "api"),
        ("protocol", ""),
        ("input_contract_ref", " "),
        ("output_contract_ref", None),
        ("verification_policy_ref", 1),
        ("input_classification", True),
        ("input_classification", 99),
        ("compartments", ["program-1", "program-1"]),
        ("residency", [""]),
        ("slots", False),
        ("slots", 0),
        ("slots", 1.5),
    ],
)
def test_requested_capability_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        _validate(requested_capability=_capability(**{field: value}))


def test_requested_capability_rejects_unknown_and_missing_keys():
    with pytest.raises(ValueError):
        _validate(requested_capability=_capability(unexpected=True))
    missing = _capability()
    del missing["protocol"]
    with pytest.raises(ValueError):
        _validate(requested_capability=missing)


@pytest.mark.parametrize(
    "manifest",
    [
        {"resources": [{"resource_id": "r", "required": True, "mode": "project_readonly", "x": 1}]},
        {"resources": [{"resource_id": "", "required": True, "mode": "project_readonly"}]},
        {"resources": [{"resource_id": "r", "required": 1, "mode": "project_readonly"}]},
        {"resources": [{"resource_id": "r", "required": True, "mode": "unknown"}]},
        {"resources": [{"resource_id": "r", "required": True, "mode": "project_readonly"},
                        {"resource_id": "r", "required": False, "mode": "portable"}]},
        {"work_nodes": ["node-1", "node-1"]},
        {"work_nodes": [""]},
        {"work_nodes": "node-1"},
        {"unexpected": []},
    ],
)
def test_input_manifest_rejects_invalid_values(manifest):
    with pytest.raises(ValueError):
        _validate(input_manifest=manifest)


def test_input_manifest_defaults_both_lists():
    assert _validate(input_manifest={})["input_manifest"] == {"resources": [], "work_nodes": []}


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"artifact_types": ["application/json"], "required": True},
        {"artifact_types": [], "required": True, "max_count": 1},
        {"artifact_types": ["application/json", "application/json"], "required": True, "max_count": 1},
        {"artifact_types": ["not-a-mime"], "required": True, "max_count": 1},
        {"artifact_types": ["application/json"], "required": 1, "max_count": 1},
        {"artifact_types": ["application/json"], "required": True, "max_count": 0},
        {"schema": "coifesp.task-output.v1"},
        {"schema": "other", "required_fields": ["artifact_refs", "summary"]},
        {"schema": "coifesp.task-output.v1", "required_fields": ["summary"]},
        {"schema": "coifesp.task-output.v1", "required_fields": ["artifact_refs", "summary", "summary"]},
        {"schema": "coifesp.task-output.v1", "required_fields": ["artifact_refs", "summary", "unknown"]},
        {"schema": "coifesp.task-output.v1", "required_fields": "artifact_refs"},
        {"artifact_types": ["application/json"], "required": True, "max_count": 1, "extra": 1},
    ],
)
def test_output_contract_rejects_invalid_values(output):
    with pytest.raises(ValueError):
        _validate(output=output)


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"criteria": []},
        {"criteria": [{"criterion_id": "c", "type": "tool_check", "required": True}]},
        {"criteria": [{"criterion_id": "c", "type": "tool_check", "required": True, "tool": " "}]},
        {"criteria": [{"criterion_id": "c", "type": "agent_review", "required": True, "tool": "review"}]},
        {"criteria": [{"criterion_id": "c", "type": "other", "required": True}]},
        {"criteria": [{"criterion_id": "", "type": "agent_review", "required": True}]},
        {"criteria": [{"criterion_id": "c", "type": "agent_review", "required": 1}]},
        {"criteria": [
            {"criterion_id": "c", "type": "agent_review", "required": True},
            {"criterion_id": "c", "type": "tool_check", "required": True, "tool": "x"},
        ]},
        {"criteria": [{"criterion_id": "c", "type": "agent_review", "required": True, "extra": 1}]},
    ],
)
def test_verification_policy_rejects_invalid_values(policy):
    with pytest.raises(ValueError):
        _validate(verification_policy=policy)


@pytest.mark.parametrize("value", ["", " ", "x" * 33, None, 1, True])
def test_autonomy_requirement_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        _validate(autonomy_requirement=value)


def test_autonomy_requirement_is_opaque_bounded_string_when_no_frozen_enum_exists():
    assert _validate(autonomy_requirement="bounded")["autonomy_requirement"] == "bounded"


def test_result_is_mutation_isolated_from_all_input_nesting():
    capability = _capability(tags=["api"], compartments=["program-1"])
    manifest = _manifest()
    output = _artifact_output()
    policy = _verification()
    result = validate_task_contract(
        requested_capability=capability,
        input_manifest=manifest,
        output_contract=output,
        verification_policy=policy,
        autonomy_requirement="supervised",
    )

    result["requested_capability"]["tags"].append("mutated")
    result["input_manifest"]["resources"][0]["resource_id"] = "mutated"
    result["output_contract"]["artifact_types"].append("application/json")
    result["verification_policy"]["criteria"][0]["tool"] = "mutated"
    capability["tags"].append("caller-mutated")
    manifest["resources"][0]["mode"] = "portable"
    output["artifact_types"].append("application/json")
    policy["criteria"][0]["tool"] = "caller-mutated"

    assert result["requested_capability"]["tags"] == ["api", "mutated"]
    assert capability["tags"] == ["api", "caller-mutated"]
    assert manifest["resources"][0]["mode"] == "portable"
    assert result["input_manifest"]["resources"][0]["mode"] == "project_readonly"
    assert result["output_contract"]["artifact_types"] == [
        "application/openapi+yaml",
        "application/json",
    ]
    assert output["artifact_types"] == ["application/openapi+yaml", "application/json"]
    assert result["verification_policy"]["criteria"][0]["tool"] == "mutated"
    assert policy["criteria"][0]["tool"] == "caller-mutated"
