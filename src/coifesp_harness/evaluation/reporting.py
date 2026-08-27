from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .matcher import RuleResult
from .models import PolicyExpectation, RedTeamCategory

REPORT_SCHEMA_VERSION = "1.0.0"


class GateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    case_version: str
    expected_policy: PolicyExpectation
    observed_policy: PolicyExpectation | None
    red_team_categories: frozenset[RedTeamCategory]
    passed: bool
    policy_passed: bool
    rule_results: tuple[RuleResult, ...]
    sensitive_exposure_count: int
    executor_error: bool
    failure_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_version": self.case_version,
            "expected_policy": self.expected_policy.value,
            "observed_policy": self.observed_policy.value if self.observed_policy else None,
            "red_team_categories": sorted(
                category.value for category in self.red_team_categories
            ),
            "passed": self.passed,
            "policy_passed": self.policy_passed,
            "assertions": [
                {
                    "rule_id": result.rule_id,
                    "operator": result.operator.value,
                    "passed": result.passed,
                    "failure_code": result.failure_code,
                }
                for result in self.rule_results
            ],
            "sensitive_exposure_count": self.sensitive_exposure_count,
            "executor_error": self.executor_error,
            "failure_codes": list(self.failure_codes),
        }


@dataclass(frozen=True, slots=True)
class GateViolation:
    code: str
    scope: str
    measured: dict[str, int]
    threshold: str | int

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "scope": self.scope,
            "measured": dict(sorted(self.measured.items())),
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class GateAssessment:
    status: GateStatus
    violations: tuple[GateViolation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "violations": [violation.to_dict() for violation in self.violations],
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    suite_id: str
    suite_version: str
    runner_version: str
    results: tuple[CaseResult, ...]
    gate: GateAssessment

    @property
    def report_id(self) -> str:
        payload = json.dumps(
            self._payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _payload(self) -> dict[str, Any]:
        passed = sum(result.passed for result in self.results)
        policy_passed = sum(result.policy_passed for result in self.results)
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "suite": {"id": self.suite_id, "version": self.suite_version},
            "runner_version": self.runner_version,
            "summary": {
                "total": len(self.results),
                "passed": passed,
                "failed": len(self.results) - passed,
                "policy_passed": policy_passed,
                "secret_exposures": sum(
                    result.sensitive_exposure_count for result in self.results
                ),
                "executor_errors": sum(result.executor_error for result in self.results),
            },
            "gate": self.gate.to_dict(),
            "cases": [result.to_dict() for result in self.results],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self._payload()}

    def to_json(self, *, indent: int | None = None) -> str:
        separators = None if indent is not None else (",", ":")
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=separators,
            indent=indent,
        )
