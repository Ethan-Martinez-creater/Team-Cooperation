"""Strict, side-effect-free parser for independent agent review results."""

from __future__ import annotations

import json
from typing import Any

_SCHEMA = "coifesp.verification-result.v1"
_FIELDS = frozenset(
    {"schema", "passed", "findings", "required_changes", "evidence_refs"}
)
_MAX_JSON_BYTES = 128 * 1024
_MAX_LIST_ITEMS = 32
_MAX_TEXT_CHARS = 2_000


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object while rejecting duplicate keys at every JSON depth."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("review result JSON has duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("review result JSON has a non-finite number")


def _text(value: object, *, field: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"review result {field} must contain bounded non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"review result {field} contains invalid Unicode") from exc
    return value


def _text_list(value: object, *, field: str) -> list[str]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"review result {field} must be a bounded text list")
    normalized = [_text(item, field=f"{field}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"review result {field} contains duplicate text")
    return normalized


def _ref_list(value: object, *, field: str) -> list[str]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"review result {field} must be a bounded reference list")
    normalized = [_text(item, field=f"{field}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"review result {field} contains duplicate references")
    return normalized


def parse_review_result(content: str, *, evidence_refs: list[str]) -> dict[str, Any]:
    """Parse one exact JSON review result against a supplied evidence manifest.

    This function validates detached data only.  It does not perform I/O,
    mutate review state, or call an external model or service.
    """

    if type(content) is not str or not content:
        raise ValueError("review result must contain JSON text")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("review result contains invalid Unicode") from exc
    if len(encoded) > _MAX_JSON_BYTES:
        raise ValueError("review result JSON exceeds its size limit")

    allowed_refs = _ref_list(evidence_refs, field="allowed evidence_refs")
    try:
        payload = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("review result must be exactly one valid JSON value") from exc

    if type(payload) is not dict or set(payload) != _FIELDS:
        raise ValueError("review result must contain exactly the mandatory fields")
    if payload["schema"] != _SCHEMA:
        raise ValueError("review result schema is unsupported")
    if type(payload["passed"]) is not bool:
        raise ValueError("review result passed must be a boolean")

    findings = _text_list(payload["findings"], field="findings")
    required_changes = _text_list(payload["required_changes"], field="required_changes")
    result_refs = _ref_list(payload["evidence_refs"], field="evidence_refs")
    if not set(result_refs) <= set(allowed_refs):
        raise ValueError("review result cites evidence outside the supplied manifest")

    passed = payload["passed"]
    if passed:
        if required_changes:
            raise ValueError("successful review cannot require changes")
        if allowed_refs and not result_refs:
            raise ValueError("successful review must cite supplied evidence")
    elif not findings or not required_changes:
        raise ValueError("failed review requires findings and required changes")

    return {
        "schema": _SCHEMA,
        "passed": passed,
        "findings": list(findings),
        "required_changes": list(required_changes),
        "evidence_refs": list(result_refs),
    }


__all__ = ["parse_review_result"]
