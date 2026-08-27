from __future__ import annotations

from dataclasses import dataclass

from ..security.redaction import SecretRedactor
from .models import MatchOperator, OutputRule, SensitiveValue


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    operator: MatchOperator
    passed: bool
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class OutputMatchResult:
    rule_results: tuple[RuleResult, ...]
    sensitive_exposure_count: int
    output_too_large: bool

    @property
    def passed(self) -> bool:
        return (
            not self.output_too_large
            and self.sensitive_exposure_count == 0
            and all(result.passed for result in self.rule_results)
        )


class SafeOutputMatcher:
    """Matches output in memory and returns only non-sensitive result codes.

    It deliberately supports literal operations only. This avoids regular-expression
    denial-of-service and prevents a failed assertion from echoing its secret value.
    """

    def __init__(self, *, max_output_chars: int = 1_000_000) -> None:
        if not isinstance(max_output_chars, int) or max_output_chars <= 0:
            raise ValueError("max_output_chars must be a positive integer")
        self.max_output_chars = max_output_chars
        self._secret_redactor = SecretRedactor()

    def match(
        self,
        output: str,
        *,
        rules: tuple[OutputRule, ...],
        sensitive_values: tuple[SensitiveValue, ...],
    ) -> OutputMatchResult:
        if not isinstance(output, str):
            raise TypeError("executor output must be text")

        too_large = len(output) > self.max_output_chars
        if too_large:
            return OutputMatchResult(
                tuple(
                    RuleResult(rule.rule_id, rule.operator, False, "output_too_large")
                    for rule in rules
                ),
                sensitive_exposure_count=0,
                output_too_large=True,
            )

        exposed = 0
        for sensitive in sensitive_values:
            haystack, needle = self._comparable(
                output, sensitive.value, sensitive.case_sensitive
            )
            if needle in haystack:
                exposed += 1
        # Standard credential shapes are unsafe even when a case author omitted a canary.
        exposed += len(self._secret_redactor.redact(output).findings)

        results: list[RuleResult] = []
        for rule in rules:
            haystack, needle = self._comparable(output, rule.expected, rule.case_sensitive)
            if rule.operator is MatchOperator.CONTAINS:
                passed = needle in haystack
                failure = None if passed else "required_output_absent"
            elif rule.operator is MatchOperator.EXCLUDES:
                passed = needle not in haystack
                failure = None if passed else "forbidden_output_present"
            else:
                passed = haystack == needle
                failure = None if passed else "output_not_equal"
            results.append(RuleResult(rule.rule_id, rule.operator, passed, failure))
        return OutputMatchResult(tuple(results), exposed, too_large)

    @staticmethod
    def _comparable(output: str, expected: str, case_sensitive: bool) -> tuple[str, str]:
        if case_sensitive:
            return output, expected
        return output.casefold(), expected.casefold()
