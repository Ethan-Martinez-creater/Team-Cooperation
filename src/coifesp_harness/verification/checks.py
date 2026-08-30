"""Bounded, deterministic checks for pinned TeamTask artifact evidence.

The verification service owns persistence and authorization.  This module is
deliberately a small, side-effect-free evaluator: it accepts only a detached
artifact snapshot and an authorized content reader, and it never treats
caller-provided JSON as evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..errors import IntegrityError, PolicyDenied, ResourceNotFound
from ..team_agents.task_contract_models import _verification_policy

_INTEGRITY_CRITERION_ID = "__artifact_integrity__"
_ARTIFACT_KEYS = frozenset(
    {"resource_id", "owner_team_id", "artifact_id", "sha256", "media_type", "size_bytes"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# A concrete MIME type has one non-empty type and subtype token.  Parameters
# and wildcard tokens are intentionally excluded: snapshots are manifest
# metadata, not HTTP Content-Type header values.
_MIME = re.compile(
    r"^[!#$%&'+.^_`|~0-9A-Za-z-]+/[!#$%&'+.^_`|~0-9A-Za-z-]+$"
)
_JSON_MAX_BYTES = 1_000_000

_BASELINE_PASS = "artifact_integrity_verified"
_BASELINE_FAIL = "artifact_integrity_failed"
_JSON_PASS = "artifact_json_valid"
_JSON_FAIL = "artifact_json_invalid"
_JSON_MEDIA_FAIL = "artifact_json_requires_json_mime"
_JSON_EMPTY_FAIL = "artifact_json_requires_artifacts"


def evaluate_checks(*, policy: dict, artifacts: list[dict], artifact_content) -> dict:
    """Evaluate deterministic verification criteria against pinned artifacts.

    ``artifact_content`` is expected to expose ``open_policy_authorized``.
    Content is streamed once per artifact; hashes are never materialized, and
    JSON is buffered only when a policy requests ``artifact.json`` (up to one
    megabyte per artifact).

    Malformed policy/snapshot inputs raise :class:`ValueError`.  Expected
    content-integrity and authorization failures become a failed baseline;
    transient or unknown reader failures intentionally propagate so callers
    can retry without persisting a false verdict.
    """

    normalized_policy = _verification_policy(policy)
    criteria = normalized_policy["criteria"]
    if any(item["criterion_id"] == _INTEGRITY_CRITERION_ID for item in criteria):
        # Keep this guard here as well as in the contract validator.  A caller
        # may invoke this low-level evaluator with a policy created elsewhere.
        raise ValueError("verification_policy criterion_id is reserved for artifact integrity")

    normalized_artifacts = _validate_artifacts(artifacts)
    evidence_refs = [item["resource_id"] for item in normalized_artifacts]
    needs_json = any(
        item["type"] == "tool_check" and item["tool"] == "artifact.json"
        for item in criteria
    )

    baseline_status, baseline_code, json_outcome = _verify_artifacts(
        normalized_artifacts,
        artifact_content,
        collect_json=needs_json,
    )
    baseline = {
        "criterion_id": _INTEGRITY_CRITERION_ID,
        "type": "tool_check",
        "required": True,
        "status": baseline_status,
        "code": baseline_code,
        "evidence_refs": list(evidence_refs),
    }

    checks = [baseline]
    for criterion in criteria:
        checks.append(
            _evaluate_criterion(
                criterion,
                baseline_status=baseline_status,
                baseline_code=baseline_code,
                json_outcome=json_outcome,
                evidence_refs=evidence_refs,
            )
        )

    return {"status": _aggregate(checks), "checks": checks}


def _validate_artifacts(value: object) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("artifacts must be a list")

    result: list[dict[str, Any]] = []
    resource_ids: set[str] = set()
    for index, item in enumerate(value):
        if type(item) is not dict or set(item) != _ARTIFACT_KEYS:
            raise ValueError(f"artifacts[{index}] must contain the exact artifact snapshot fields")

        resource_id = _identifier(item["resource_id"], f"artifacts[{index}].resource_id")
        if resource_id in resource_ids:
            raise ValueError("artifacts contains duplicate resource_id")
        resource_ids.add(resource_id)

        owner_team_id = _identifier(item["owner_team_id"], f"artifacts[{index}].owner_team_id")
        artifact_id = _identifier(item["artifact_id"], f"artifacts[{index}].artifact_id")
        sha256 = item["sha256"]
        if type(sha256) is not str or _SHA256.fullmatch(sha256) is None:
            raise ValueError(f"artifacts[{index}].sha256 must be lowercase hexadecimal SHA-256")

        media_type = item["media_type"]
        if (
            type(media_type) is not str
            or not media_type
            or _MIME.fullmatch(media_type) is None
            or "*" in media_type
        ):
            raise ValueError(f"artifacts[{index}].media_type must be a concrete MIME type")

        size_bytes = item["size_bytes"]
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError(f"artifacts[{index}].size_bytes must be a non-negative integer")

        result.append(
            {
                "resource_id": resource_id,
                "owner_team_id": owner_team_id,
                "artifact_id": artifact_id,
                "sha256": sha256,
                "media_type": media_type,
                "size_bytes": size_bytes,
            }
        )
    return result


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _verify_artifacts(
    artifacts: list[dict[str, Any]], artifact_content, *, collect_json: bool
) -> tuple[str, str, tuple[str, str] | None]:
    """Stream all snapshots and return baseline plus the JSON-tool result.

    The JSON outcome tuple is ``(status, code)`` and is computed while the
    content is already in flight.  Keeping only one bounded bytearray at a
    time avoids making a large artifact set multiply the memory bound.
    """

    # An empty optional artifact set is valid for the hash check.  There is no
    # content to read, so a missing reader is not a reason to hold that result
    # pending.  ``artifact.json`` handles its required non-empty set below.
    if not artifacts:
        if collect_json:
            return "PASS", _BASELINE_PASS, ("FAIL", _JSON_EMPTY_FAIL)
        return "PASS", _BASELINE_PASS, None

    reader = getattr(artifact_content, "open_policy_authorized", None)
    if not callable(reader):
        return "PENDING", "reader_unavailable", ("PENDING", "reader_unavailable") if collect_json else None

    baseline_failed = False
    json_results: list[tuple[str, str]] = []
    for artifact in artifacts:
        json_buffer: bytearray | None = None
        json_oversized = False
        if collect_json and _is_json_media_type(artifact["media_type"]):
            # A declared size beyond the bound can never be parsed, while the
            # integrity pass still consumes the stream and verifies its hash.
            if artifact["size_bytes"] <= _JSON_MAX_BYTES:
                json_buffer = bytearray()
            else:
                json_oversized = True

        digest = hashlib.sha256()
        size = 0
        artifact_failed = False
        try:
            chunks = reader(
                owner_tenant_id=artifact["owner_team_id"],
                sha256=artifact["sha256"],
                expected_size=artifact["size_bytes"],
            )
            for chunk in chunks:
                if type(chunk) is not bytes or not chunk:
                    artifact_failed = True
                    # The reader contract promises non-empty bytes.  Do not
                    # hash malformed chunks or expose their representation.
                    continue
                size += len(chunk)
                digest.update(chunk)

                if json_buffer is not None:
                    remaining = _JSON_MAX_BYTES - len(json_buffer)
                    if remaining < len(chunk):
                        json_oversized = True
                        json_buffer = None
                    else:
                        json_buffer.extend(chunk)
        except (IntegrityError, ResourceNotFound, PolicyDenied):
            # These errors are expected evidence failures.  Their messages may
            # carry sensitive storage details and are intentionally discarded.
            artifact_failed = True

        if size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
            artifact_failed = True
        if artifact_failed:
            baseline_failed = True

        if collect_json:
            if not _is_json_media_type(artifact["media_type"]):
                json_results.append(("FAIL", _JSON_MEDIA_FAIL))
            elif artifact_failed:
                # A semantic result is not evidence for a corrupted or
                # unavailable artifact, even if some bytes happened to parse.
                json_results.append(("FAIL", _BASELINE_FAIL))
            elif json_oversized:
                json_results.append(("PENDING", "json_check_size_limit"))
            else:
                assert json_buffer is not None
                json_results.append(_parse_json_bytes(bytes(json_buffer)))

    if baseline_failed:
        baseline_status, baseline_code = "FAIL", _BASELINE_FAIL
    else:
        baseline_status, baseline_code = "PASS", _BASELINE_PASS

    json_outcome = _aggregate_outcomes(json_results) if collect_json else None
    return baseline_status, baseline_code, json_outcome


def _is_json_media_type(media_type: str) -> bool:
    major, subtype = media_type.split("/", 1)
    return major.lower() == "application" and (
        subtype.lower() == "json" or subtype.lower().endswith("+json")
    )


def _parse_json_bytes(value: bytes) -> tuple[str, str]:
    try:
        text = value.decode("utf-8", errors="strict")
        json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return "FAIL", _JSON_FAIL
    return "PASS", _JSON_PASS


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _evaluate_criterion(
    criterion: dict[str, Any],
    *,
    baseline_status: str,
    baseline_code: str,
    json_outcome: tuple[str, str] | None,
    evidence_refs: list[str],
) -> dict[str, Any]:
    if criterion["type"] == "agent_review":
        status, code = "PENDING", "agent_review_unavailable"
    else:
        tool = criterion["tool"]
        if tool == "artifact.sha256":
            status, code = baseline_status, baseline_code
        elif tool == "artifact.json":
            if baseline_status == "PENDING":
                status, code = "PENDING", "reader_unavailable"
            elif baseline_status == "FAIL":
                status, code = "FAIL", _BASELINE_FAIL
            elif json_outcome is None:
                # Defensive fallback; the evaluator always computes this for
                # a requested artifact.json criterion.
                status, code = "PENDING", "reader_unavailable"
            else:
                status, code = json_outcome
        else:
            status, code = "PENDING", "tool_unavailable"

    check = {
        "criterion_id": criterion["criterion_id"],
        "type": criterion["type"],
        "required": criterion["required"],
        "status": status,
        "code": code,
        "evidence_refs": list(evidence_refs),
    }
    if criterion["type"] == "tool_check":
        check["tool"] = criterion["tool"]
    return check


def _aggregate_outcomes(outcomes: list[tuple[str, str]]) -> tuple[str, str]:
    if any(status == "FAIL" for status, _ in outcomes):
        # A known failure is stronger than a size-bound pending result.  The
        # first failure code is deterministic and never includes raw content.
        return "FAIL", next(code for status, code in outcomes if status == "FAIL")
    if any(status == "PENDING" for status, _ in outcomes):
        return "PENDING", next(code for status, code in outcomes if status == "PENDING")
    return "PASS", _JSON_PASS


def _aggregate(checks: list[dict[str, Any]]) -> str:
    required = [check for check in checks if check["required"]]
    if any(check["status"] == "FAIL" for check in required):
        return "FAIL"
    if any(check["status"] == "PENDING" for check in required):
        return "PENDING"
    return "PASS"


__all__ = ["evaluate_checks"]
