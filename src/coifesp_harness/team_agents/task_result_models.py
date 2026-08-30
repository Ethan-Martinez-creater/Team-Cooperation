"""Strict data-only Team Agent output protocol; no storage or authority here."""

from __future__ import annotations

import json
import re

from .task_contract_models import _output_contract

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$", re.IGNORECASE)
_FIELDS = {"schema", "artifact_refs", "summary", "known_limitations"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("task output JSON has duplicate fields")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("task output JSON has a non-finite value")


def _text(value, *, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError("task output text is missing or exceeds its limit")
    value.encode("utf-8")
    return value


def _contract(value):
    # Reuse the accepted-contract validator, including required_fields and the
    # two allowed contract forms, rather than defining a second schema dialect.
    contract = _output_contract(value)
    types = contract.get("artifact_types", [])
    if any(not _MIME.fullmatch(item) for item in types):
        raise ValueError("task output contract must use concrete MIME types")
    if len({item.lower() for item in types}) != len(types):
        raise ValueError("task output contract MIME types are duplicated")
    return contract


def _refs(value, contract):
    if type(value) is not list or len(value) > min(32, contract.get("max_count", 32)):
        raise ValueError("task output artifact_refs must be a bounded list")
    if not value and contract.get("required", True):
        raise ValueError("task output requires artifacts")
    if any(type(item) is not str or not _ID.fullmatch(item) for item in value):
        raise ValueError("task output artifact_refs must be project resource IDs")
    if len(value) != len(set(value)):
        raise ValueError("task output artifact_refs are duplicated")
    return list(value)


def parse_task_result(content: str, *, output_contract: dict) -> dict:
    """Read exactly one JSON result; no Markdown/prose/path heuristics."""
    if type(content) is not str or not content or len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("task output must contain at most 1MB of JSON text")
    payload = json.loads(content, object_pairs_hook=_object, parse_constant=_constant)
    if (
        type(payload) is not dict
        or set(payload) - _FIELDS
        or not {"schema", "artifact_refs", "summary"} <= set(payload)
    ):
        raise ValueError("task output fields are invalid")
    if payload["schema"] != "coifesp.task-output.v1":
        raise ValueError("task output schema is unsupported")
    contract = _contract(output_contract)
    if not set(contract.get("required_fields", [])) <= set(payload):
        raise ValueError("task output is missing contracted fields")
    limitations = payload.get("known_limitations", [])
    if type(limitations) is not list or len(limitations) > 100:
        raise ValueError("task output known_limitations must be a bounded text list")
    normalized = [_text(item, limit=2000) for item in limitations]
    if len(normalized) != len(set(normalized)):
        raise ValueError("task output limitations are duplicated")
    return {
        "schema": "coifesp.task-output.v1",
        "artifact_refs": _refs(payload["artifact_refs"], contract),
        "summary": _text(payload["summary"], limit=20_000),
        "known_limitations": normalized,
    }


def validate_task_artifact_types(
    result: dict, *, media_types: dict[str, str], output_contract: dict
) -> None:
    """Require exactly the refs validated from real manifests, never a subset."""
    if type(result) is not dict or type(media_types) is not dict:
        raise ValueError("task output artifact metadata must be an object")
    contract = _contract(output_contract)
    refs = _refs(result.get("artifact_refs"), contract)
    if set(media_types) != set(refs):
        raise ValueError("task output artifact metadata does not match its references")
    allowed = {item.lower() for item in contract.get("artifact_types", [])}
    for value in media_types.values():
        if (
            type(value) is not str
            or not _MIME.fullmatch(value)
            or (allowed and value.lower() not in allowed)
        ):
            raise ValueError("task artifact MIME type violates the output contract")
