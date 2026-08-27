from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .models import (
    EvaluationCase, EvaluationSuite, GateThresholds, MatchOperator, OutputRule,
    PolicyExpectation, RedTeamCategory, SensitiveValue,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_DOCUMENT_BYTES = 4_194_304


class EvaluationDocumentError(ValueError):
    """Safe, fail-closed error for signed evaluation documents."""


def _duplicate_safe(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationDocumentError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise EvaluationDocumentError("non-finite JSON numbers are forbidden")


def _load(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise EvaluationDocumentError("evaluation document exceeds its size limit")
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_safe, parse_constant=_reject_constant)
    except EvaluationDocumentError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EvaluationDocumentError("invalid evaluation JSON") from exc
    if not isinstance(value, dict):
        raise EvaluationDocumentError("evaluation document root must be an object")
    return value


def _object(value: object, context: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationDocumentError(f"{context} fields are invalid")
    return value


def _array(value: object, context: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > 10_000:
        raise EvaluationDocumentError(f"{context} must be a bounded array")
    return value


def _string(value: object, context: str, *, maximum: int = 1_000_000) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise EvaluationDocumentError(f"{context} must be a bounded non-empty string")
    return value


def _b64url(value: object, context: str) -> bytes:
    raw = _string(value, context, maximum=512)
    if "=" in raw:
        raise EvaluationDocumentError(f"{context} must be unpadded base64url")
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise EvaluationDocumentError(f"{context} is invalid") from exc


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class TrustedEvaluationKey:
    key_id: str
    public_key: Ed25519PublicKey


class EvaluationSuiteRegistry:
    """Verifies an immutable suite before constructing executable cases."""

    def __init__(self, trusted_keys: Mapping[str, Ed25519PublicKey]) -> None:
        if not trusted_keys or any(_ID.fullmatch(key) is None for key in trusted_keys):
            raise ValueError("trusted evaluation keys are invalid")
        self._keys = dict(trusted_keys)

    @classmethod
    def from_trust_document(cls, text: str) -> "EvaluationSuiteRegistry":
        root = _object(_load(text), "trust document", {"format_version", "keys"})
        if root["format_version"] != "1.0.0":
            raise EvaluationDocumentError("trust document format_version must be 1.0.0")
        keys: dict[str, Ed25519PublicKey] = {}
        for raw in _array(root["keys"], "keys", nonempty=True):
            item = _object(raw, "trusted key", {"key_id", "algorithm", "public_key"})
            key_id = _string(item["key_id"], "key_id", maximum=128)
            if _ID.fullmatch(key_id) is None or key_id in keys or item["algorithm"] != "Ed25519":
                raise EvaluationDocumentError("trusted key identity or algorithm is invalid")
            material = _b64url(item["public_key"], "public_key")
            if len(material) != 32:
                raise EvaluationDocumentError("Ed25519 public key must contain 32 bytes")
            keys[key_id] = Ed25519PublicKey.from_public_bytes(material)
        return cls(keys)

    def load(self, text: str) -> EvaluationSuite:
        root = _object(_load(text), "signed suite", {"format_version", "suite", "signing"})
        if root["format_version"] != "1.0.0":
            raise EvaluationDocumentError("signed suite format_version must be 1.0.0")
        signing = _object(root["signing"], "signing", {"key_id", "algorithm", "suite_digest", "signature"})
        key_id = _string(signing["key_id"], "signing.key_id", maximum=128)
        key = self._keys.get(key_id)
        if key is None or signing["algorithm"] != "Ed25519":
            raise EvaluationDocumentError("suite signer is not trusted")
        canonical = _canonical(root["suite"])
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        if not isinstance(signing["suite_digest"], str) or _SHA256.fullmatch(signing["suite_digest"]) is None or signing["suite_digest"] != digest:
            raise EvaluationDocumentError("suite digest is invalid")
        signature = _b64url(signing["signature"], "signing.signature")
        try:
            key.verify(signature, canonical)
        except InvalidSignature as exc:
            raise EvaluationDocumentError("suite signature is invalid") from exc
        return _parse_suite(root["suite"])


def _parse_suite(value: object) -> EvaluationSuite:
    raw = _object(value, "suite", {"suite_id", "version", "evaluation_instant", "thresholds", "cases"})
    thresholds_raw = _object(raw["thresholds"], "thresholds", {
        "minimum_pass_rate", "minimum_policy_pass_rate", "max_failures",
        "max_secret_exposures", "max_executor_errors", "category_minimum_pass_rates",
        "required_red_team_categories",
    })
    rates_raw = thresholds_raw["category_minimum_pass_rates"]
    if not isinstance(rates_raw, dict):
        raise EvaluationDocumentError("category_minimum_pass_rates must be an object")
    try:
        thresholds = GateThresholds(
            minimum_pass_rate=thresholds_raw["minimum_pass_rate"],
            minimum_policy_pass_rate=thresholds_raw["minimum_policy_pass_rate"],
            max_failures=thresholds_raw["max_failures"],
            max_secret_exposures=thresholds_raw["max_secret_exposures"],
            max_executor_errors=thresholds_raw["max_executor_errors"],
            category_minimum_pass_rates={RedTeamCategory(key): val for key, val in rates_raw.items()},
            required_red_team_categories=frozenset(RedTeamCategory(item) for item in _array(thresholds_raw["required_red_team_categories"], "required categories")),
        )
        cases = []
        for case_raw in _array(raw["cases"], "cases", nonempty=True):
            item = _object(case_raw, "case", {"case_id", "version", "expected_policy", "input_payload", "output_rules", "sensitive_values", "red_team_categories"})
            rules = tuple(OutputRule(
                _string(rule["rule_id"], "rule_id", maximum=128), MatchOperator(rule["operator"]),
                _string(rule["expected"], "expected"), bool(rule["case_sensitive"]),
            ) for rule in (_object(entry, "output rule", {"rule_id", "operator", "expected", "case_sensitive"}) for entry in _array(item["output_rules"], "output_rules")))
            sensitive = tuple(SensitiveValue(
                _string(secret["value_id"], "value_id", maximum=128), _string(secret["value"], "sensitive value"), bool(secret["case_sensitive"]),
            ) for secret in (_object(entry, "sensitive value", {"value_id", "value", "case_sensitive"}) for entry in _array(item["sensitive_values"], "sensitive_values")))
            if any(type(entry["case_sensitive"]) is not bool for entry in item["output_rules"] + item["sensitive_values"]):
                raise EvaluationDocumentError("case_sensitive must be boolean")
            cases.append(EvaluationCase(
                case_id=item["case_id"], version=item["version"], expected_policy=PolicyExpectation(item["expected_policy"]),
                input_payload=item["input_payload"], output_rules=rules, sensitive_values=sensitive,
                red_team_categories=frozenset(RedTeamCategory(category) for category in _array(item["red_team_categories"], "red_team_categories")),
            ))
        instant = datetime.fromisoformat(_string(raw["evaluation_instant"], "evaluation_instant", maximum=64).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise EvaluationDocumentError("evaluation_instant must be timezone-aware")
        return EvaluationSuite(raw["suite_id"], raw["version"], tuple(cases), thresholds, instant)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EvaluationDocumentError):
            raise
        raise EvaluationDocumentError("suite content is invalid") from exc
