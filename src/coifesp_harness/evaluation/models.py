from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_MAX_COLLECTION_ITEMS = 10_000
_MAX_NESTING_DEPTH = 32


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a safe, non-sensitive identifier")


def _validate_version(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a strict semantic version")


def _freeze_json_value(value: Any, *, depth: int = 0) -> Any:
    """Snapshot JSON-like case input so an executor cannot mutate the suite."""

    if depth > _MAX_NESTING_DEPTH:
        raise ValueError("input_payload exceeds the nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("input_payload cannot contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("input_payload mapping exceeds the item limit")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("input_payload mapping keys must be strings")
            result[key] = _freeze_json_value(item, depth=depth + 1)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("input_payload sequence exceeds the item limit")
        return tuple(_freeze_json_value(item, depth=depth + 1) for item in value)
    raise ValueError("input_payload must contain only JSON-compatible values")


def _rate(value: Decimal | str | float | int, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal rate") from exc
    if not result.is_finite() or result < 0 or result > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return result


class PolicyExpectation(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL = "approval"


class RedTeamCategory(str, Enum):
    PROMPT_INJECTION = "prompt_injection"
    DATA_EXFILTRATION = "data_exfiltration"
    SECRET_LEAKAGE = "secret_leakage"
    CROSS_TENANT_DISCLOSURE = "cross_tenant_disclosure"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    APPROVAL_BYPASS = "approval_bypass"
    UNSAFE_TOOL_USE = "unsafe_tool_use"
    RESOURCE_EXHAUSTION = "resource_exhaustion"


class MatchOperator(str, Enum):
    CONTAINS = "contains"
    EXCLUDES = "excludes"
    EQUALS = "equals"


@dataclass(frozen=True, slots=True)
class OutputRule:
    """A bounded literal assertion whose expected value is never reportable."""

    rule_id: str
    operator: MatchOperator
    expected: str = field(repr=False)
    case_sensitive: bool = True

    def __post_init__(self) -> None:
        _validate_identifier(self.rule_id, "rule_id")
        if not isinstance(self.operator, MatchOperator):
            raise ValueError("operator must be a MatchOperator")
        if not isinstance(self.expected, str) or not self.expected:
            raise ValueError("expected must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SensitiveValue:
    """A canary or secret that must never appear in an executor's output."""

    value_id: str
    value: str = field(repr=False)
    case_sensitive: bool = True

    def __post_init__(self) -> None:
        _validate_identifier(self.value_id, "value_id")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("sensitive value must be a non-empty string")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    version: str
    expected_policy: PolicyExpectation
    input_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    output_rules: tuple[OutputRule, ...] = ()
    sensitive_values: tuple[SensitiveValue, ...] = field(default=(), repr=False)
    red_team_categories: frozenset[RedTeamCategory] = frozenset()

    def __post_init__(self) -> None:
        _validate_identifier(self.case_id, "case_id")
        _validate_version(self.version, "case version")
        if not isinstance(self.expected_policy, PolicyExpectation):
            raise ValueError("expected_policy must be a PolicyExpectation")
        rules = tuple(self.output_rules)
        secrets = tuple(self.sensitive_values)
        categories = frozenset(self.red_team_categories)
        if any(not isinstance(rule, OutputRule) for rule in rules):
            raise ValueError("output_rules must contain OutputRule values")
        if any(not isinstance(value, SensitiveValue) for value in secrets):
            raise ValueError("sensitive_values must contain SensitiveValue values")
        if any(not isinstance(category, RedTeamCategory) for category in categories):
            raise ValueError("red_team_categories contains an unknown category")
        rule_ids = [rule.rule_id for rule in rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("output rule identifiers must be unique within a case")
        value_ids = [value.value_id for value in secrets]
        if len(value_ids) != len(set(value_ids)):
            raise ValueError("sensitive value identifiers must be unique within a case")
        if len(secrets) > 128:
            raise ValueError("a case cannot contain more than 128 sensitive values")
        if not isinstance(self.input_payload, Mapping):
            raise ValueError("input_payload must be a mapping")
        object.__setattr__(self, "input_payload", _freeze_json_value(self.input_payload))
        object.__setattr__(self, "output_rules", rules)
        object.__setattr__(self, "sensitive_values", secrets)
        object.__setattr__(self, "red_team_categories", categories)

    @property
    def key(self) -> str:
        return f"{self.case_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class GateThresholds:
    minimum_pass_rate: Decimal | str | float = Decimal("1")
    minimum_policy_pass_rate: Decimal | str | float = Decimal("1")
    max_failures: int | None = None
    max_secret_exposures: int = 0
    max_executor_errors: int = 0
    category_minimum_pass_rates: Mapping[
        RedTeamCategory, Decimal | str | float
    ] = field(default_factory=dict)
    required_red_team_categories: frozenset[RedTeamCategory] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_pass_rate",
            _rate(self.minimum_pass_rate, "minimum_pass_rate"),
        )
        object.__setattr__(
            self,
            "minimum_policy_pass_rate",
            _rate(self.minimum_policy_pass_rate, "minimum_policy_pass_rate"),
        )
        for field_name in ("max_secret_exposures", "max_executor_errors"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.max_failures is not None and (
            not isinstance(self.max_failures, int)
            or isinstance(self.max_failures, bool)
            or self.max_failures < 0
        ):
            raise ValueError("max_failures must be a non-negative integer or None")
        rates: dict[RedTeamCategory, Decimal] = {}
        for category, value in self.category_minimum_pass_rates.items():
            if not isinstance(category, RedTeamCategory):
                raise ValueError("category_minimum_pass_rates contains an unknown category")
            rates[category] = _rate(value, f"category rate for {category.value}")
        required = frozenset(self.required_red_team_categories)
        if any(not isinstance(category, RedTeamCategory) for category in required):
            raise ValueError("required_red_team_categories contains an unknown category")
        object.__setattr__(self, "category_minimum_pass_rates", MappingProxyType(rates))
        object.__setattr__(self, "required_red_team_categories", required)


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    suite_id: str
    version: str
    cases: tuple[EvaluationCase, ...]
    thresholds: GateThresholds = GateThresholds()
    evaluation_instant: datetime | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.suite_id, "suite_id")
        _validate_version(self.version, "suite version")
        cases = tuple(self.cases)
        if not cases:
            raise ValueError("an evaluation suite must contain at least one case")
        if any(not isinstance(case, EvaluationCase) for case in cases):
            raise ValueError("cases must contain EvaluationCase values")
        keys = [case.key for case in cases]
        if len(keys) != len(set(keys)):
            raise ValueError("case identity and version must be unique within a suite")
        object.__setattr__(self, "cases", cases)
        if self.evaluation_instant is not None:
            if not isinstance(self.evaluation_instant, datetime) or self.evaluation_instant.tzinfo is None:
                raise ValueError("evaluation_instant must be timezone-aware")
            object.__setattr__(self, "evaluation_instant", self.evaluation_instant.astimezone(UTC))

    @property
    def key(self) -> str:
        return f"{self.suite_id}@{self.version}"
