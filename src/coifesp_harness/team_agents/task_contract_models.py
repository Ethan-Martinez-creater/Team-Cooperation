"""Pure validation and normalization for structured TeamTask contracts.

The project service owns persistence and authorization.  This module only
checks the JSON contract boundary and returns a detached JSON-compatible
snapshot for callers to persist or pass to a loader.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from ..security import Classification

_CAPABILITY_REQUIRED_KEYS = frozenset(
    {
        "tags",
        "protocol",
        "input_contract_ref",
        "output_contract_ref",
        "verification_policy_ref",
    }
)
_CAPABILITY_OPTIONAL_KEYS = frozenset(
    {"input_classification", "compartments", "residency", "slots"}
)
_MANIFEST_KEYS = frozenset({"resources", "work_nodes"})
_RESOURCE_KEYS = frozenset({"resource_id", "required", "mode"})
_OUTPUT_ARTIFACT_KEYS = frozenset({"artifact_types", "required", "max_count"})
_OUTPUT_SCHEMA_KEYS = frozenset({"schema", "required_fields"})
_OUTPUT_KEYS = _OUTPUT_ARTIFACT_KEYS | _OUTPUT_SCHEMA_KEYS
_CRITERION_KEYS = frozenset({"criterion_id", "type", "required", "tool"})
_VERIFICATION_KEYS = frozenset({"criteria"})

_RESOURCE_MODES = frozenset({"team_private", "project_readonly", "portable"})
_CRITERION_TYPES = frozenset({"tool_check", "agent_review"})
_REQUIRED_OUTPUT_FIELDS = frozenset({"artifact_refs", "summary"})
_KNOWN_OUTPUT_FIELDS = frozenset(
    {"artifact_refs", "summary", "known_limitations"}
)
_MIME_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def validate_task_contract(
    *,
    requested_capability: dict,
    input_manifest: dict,
    output_contract: dict,
    verification_policy: dict,
    autonomy_requirement: str,
) -> dict:
    """Validate and detach the structured TeamTask contract.

    All malformed values raise :class:`ValueError`, including wrong types.
    The returned object contains only plain JSON-compatible dictionaries,
    lists, strings, booleans, and integers; in particular, Classification
    values are normalized to their integer wire representation.
    """

    result = {
        "requested_capability": _requested_capability(requested_capability),
        "input_manifest": _input_manifest(input_manifest),
        "output_contract": _output_contract(output_contract),
        "verification_policy": _verification_policy(verification_policy),
        "autonomy_requirement": _autonomy_requirement(autonomy_requirement),
    }
    # Every nested value was rebuilt above.  Keep the final copy explicit so
    # this function's ownership guarantee remains true if a validator grows a
    # richer nested value in the future.
    return deepcopy(result)


def _requested_capability(value: object) -> dict[str, Any]:
    raw = _object(value, "requested_capability")
    _reject_unknown(raw, _CAPABILITY_REQUIRED_KEYS | _CAPABILITY_OPTIONAL_KEYS, "requested_capability")
    _require_keys(raw, _CAPABILITY_REQUIRED_KEYS, "requested_capability")

    return {
        "tags": _string_list(raw["tags"], "requested_capability.tags", nonempty=True),
        "protocol": _nonempty_string(raw["protocol"], "requested_capability.protocol"),
        "input_contract_ref": _nonempty_string(
            raw["input_contract_ref"], "requested_capability.input_contract_ref"
        ),
        "output_contract_ref": _nonempty_string(
            raw["output_contract_ref"], "requested_capability.output_contract_ref"
        ),
        "verification_policy_ref": _nonempty_string(
            raw["verification_policy_ref"], "requested_capability.verification_policy_ref"
        ),
        "input_classification": _classification(
            raw.get("input_classification", int(Classification.INTERNAL)),
            "requested_capability.input_classification",
        ),
        "compartments": _string_list(
            raw.get("compartments", []), "requested_capability.compartments"
        ),
        "residency": _string_list(raw.get("residency", []), "requested_capability.residency"),
        "slots": _positive_integer(raw.get("slots", 1), "requested_capability.slots"),
    }


def _input_manifest(value: object) -> dict[str, Any]:
    raw = _object(value, "input_manifest")
    _reject_unknown(raw, _MANIFEST_KEYS, "input_manifest")

    resources_value = raw.get("resources", [])
    if type(resources_value) is not list:
        raise ValueError("input_manifest.resources must be a list")
    resources: list[dict[str, Any]] = []
    resource_ids: set[str] = set()
    for index, item in enumerate(resources_value):
        resource = _object(item, f"input_manifest.resources[{index}]")
        _reject_unknown(resource, _RESOURCE_KEYS, f"input_manifest.resources[{index}]")
        _require_keys(resource, _RESOURCE_KEYS, f"input_manifest.resources[{index}]")
        resource_id = _nonempty_string(
            resource["resource_id"], f"input_manifest.resources[{index}].resource_id"
        )
        if resource_id in resource_ids:
            raise ValueError("input_manifest.resources contains duplicate resource_id")
        resource_ids.add(resource_id)
        resources.append(
            {
                "resource_id": resource_id,
                "required": _boolean(
                    resource["required"], f"input_manifest.resources[{index}].required"
                ),
                "mode": _choice(
                    resource["mode"], _RESOURCE_MODES, f"input_manifest.resources[{index}].mode"
                ),
            }
        )

    work_nodes = _string_list(raw.get("work_nodes", []), "input_manifest.work_nodes")
    return {"resources": resources, "work_nodes": work_nodes}


def _output_contract(value: object) -> dict[str, Any]:
    raw = _object(value, "output_contract")
    _reject_unknown(raw, _OUTPUT_KEYS, "output_contract")

    has_artifact_field = bool(_OUTPUT_ARTIFACT_KEYS.intersection(raw))
    has_schema_field = bool(_OUTPUT_SCHEMA_KEYS.intersection(raw))
    artifact_complete = _OUTPUT_ARTIFACT_KEYS.issubset(raw)
    schema_complete = _OUTPUT_SCHEMA_KEYS.issubset(raw)
    if (has_artifact_field and not artifact_complete) or (has_schema_field and not schema_complete):
        raise ValueError("output_contract contains an incomplete output form")
    if not artifact_complete and not schema_complete:
        raise ValueError("output_contract must contain a complete output form")

    normalized: dict[str, Any] = {}
    if artifact_complete:
        normalized["artifact_types"] = _mime_list(raw["artifact_types"])
        normalized["required"] = _boolean(raw["required"], "output_contract.required")
        normalized["max_count"] = _positive_integer(raw["max_count"], "output_contract.max_count")

    if schema_complete:
        schema = _nonempty_string(raw["schema"], "output_contract.schema")
        if schema != "coifesp.task-output.v1":
            raise ValueError("output_contract.schema is unsupported")
        required_fields = _string_list(
            raw["required_fields"], "output_contract.required_fields", nonempty=True
        )
        if any(field not in _KNOWN_OUTPUT_FIELDS for field in required_fields):
            raise ValueError("output_contract.required_fields contains an unsupported field")
        if not _REQUIRED_OUTPUT_FIELDS.issubset(required_fields):
            raise ValueError("output_contract.required_fields must include artifact_refs and summary")
        normalized["schema"] = schema
        normalized["required_fields"] = required_fields

    return normalized


def _verification_policy(value: object) -> dict[str, Any]:
    raw = _object(value, "verification_policy")
    _reject_unknown(raw, _VERIFICATION_KEYS, "verification_policy")
    _require_keys(raw, _VERIFICATION_KEYS, "verification_policy")
    criteria_value = raw["criteria"]
    if type(criteria_value) is not list or not criteria_value:
        raise ValueError("verification_policy.criteria must be a nonempty list")

    criteria: list[dict[str, Any]] = []
    criterion_ids: set[str] = set()
    for index, item in enumerate(criteria_value):
        criterion = _object(item, f"verification_policy.criteria[{index}]")
        _reject_unknown(criterion, _CRITERION_KEYS, f"verification_policy.criteria[{index}]")
        required_keys = frozenset({"criterion_id", "type", "required"})
        _require_keys(criterion, required_keys, f"verification_policy.criteria[{index}]")
        criterion_id = _nonempty_string(
            criterion["criterion_id"], f"verification_policy.criteria[{index}].criterion_id"
        )
        if criterion_id in criterion_ids:
            raise ValueError("verification_policy.criteria contains duplicate criterion_id")
        if criterion_id == "__artifact_integrity__":
            raise ValueError("verification_policy criterion_id is reserved for artifact integrity")
        criterion_ids.add(criterion_id)
        criterion_type = _choice(
            criterion["type"], _CRITERION_TYPES, f"verification_policy.criteria[{index}].type"
        )
        required = _boolean(
            criterion["required"], f"verification_policy.criteria[{index}].required"
        )

        normalized_criterion = {
            "criterion_id": criterion_id,
            "type": criterion_type,
            "required": required,
        }
        if criterion_type == "tool_check":
            if "tool" not in criterion:
                raise ValueError("tool_check criteria require tool")
            normalized_criterion["tool"] = _nonempty_string(
                criterion["tool"], f"verification_policy.criteria[{index}].tool"
            )
        elif "tool" in criterion:
            raise ValueError("agent_review criteria do not accept tool")
        criteria.append(normalized_criterion)

    return {"criteria": criteria}


def _autonomy_requirement(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 32:
        raise ValueError("autonomy_requirement must be a nonempty string of at most 32 characters")
    return value


def _object(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    if any(key not in allowed for key in value):
        raise ValueError(f"{name} contains unknown fields")


def _require_keys(value: dict[str, Any], required: frozenset[str], name: str) -> None:
    if any(key not in value for key in required):
        raise ValueError(f"{name} is missing required fields")


def _nonempty_string(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _string_list(value: object, name: str, *, nonempty: bool = False) -> list[str]:
    if type(value) is not list or (nonempty and not value):
        raise ValueError(f"{name} must be a {'nonempty ' if nonempty else ''}list")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_name = f"{name}[{index}]"
        normalized = _nonempty_string(item, item_name)
        if normalized in seen:
            raise ValueError(f"{name} contains duplicate values")
        seen.add(normalized)
        result.append(normalized)
    return result


def _mime_list(value: object) -> list[str]:
    if type(value) is not list or not value:
        raise ValueError("output_contract.artifact_types must be a nonempty list")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        media_type = _nonempty_string(item, f"output_contract.artifact_types[{index}]")
        if "/" not in media_type:
            raise ValueError("output_contract.artifact_types must contain MIME types")
        major, minor = media_type.split("/", 1)
        if not _MIME_TOKEN.fullmatch(major) or not _MIME_TOKEN.fullmatch(minor):
            raise ValueError("output_contract.artifact_types must contain MIME types")
        if media_type in seen:
            raise ValueError("output_contract.artifact_types contains duplicate values")
        seen.add(media_type)
        result.append(media_type)
    return result


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _classification(value: object, name: str) -> int:
    if isinstance(value, Classification):
        return int(value)
    if type(value) is not int:
        raise ValueError(f"{name} must be a Classification or integer value")
    try:
        return int(Classification(value))
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid Classification value") from exc


def _choice(value: object, choices: frozenset[str], name: str) -> str:
    if type(value) is not str or value not in choices:
        raise ValueError(f"{name} has an unsupported value")
    return value


__all__ = ["validate_task_contract"]
