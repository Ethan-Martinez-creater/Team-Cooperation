"""Pure validation and normalization for human verification decisions."""

from __future__ import annotations

import re
from typing import Any

_DECISIONS = frozenset({"ACCEPT", "REJECT"})
_FIELDS = frozenset({"decision", "reason", "idempotency_key", "expected_version"})
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_REASON_CHARS = 2_000


def _reason(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > _MAX_REASON_CHARS:
        raise ValueError("human decision reason must be nonempty text of at most 2000 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("human decision reason contains invalid Unicode") from exc
    return value


def parse_human_decision(value: dict) -> dict[str, Any]:
    """Validate one detached human decision without changing external state."""

    if type(value) is not dict or set(value) != _FIELDS:
        raise ValueError("human decision must contain exactly the mandatory fields")

    decision = value["decision"]
    if type(decision) is not str or decision not in _DECISIONS:
        raise ValueError("human decision must be ACCEPT or REJECT")

    reason = _reason(value["reason"])

    idempotency_key = value["idempotency_key"]
    if type(idempotency_key) is not str or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise ValueError("human decision idempotency_key is invalid")

    expected_version = value["expected_version"]
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("human decision expected_version must be a positive integer")

    return {
        "decision": decision,
        "reason": reason,
        "idempotency_key": idempotency_key,
        "expected_version": expected_version,
    }


__all__ = ["parse_human_decision"]
