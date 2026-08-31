"""Deterministic, side-effect-free project completion evaluation.

The main-thread evidence loader is responsible for authorizing and
version-binding the facts before this evaluator is called.  This module only
checks the resulting boolean facts; it does not establish the authenticity of
SQL rows, their authorization, or their version binding.
"""

from collections.abc import Mapping
from dataclasses import dataclass

REQUIRED_CRITERIA = (
    "goal_confirmed",
    "all_required_requirements_satisfied",
    "all_required_tasks_terminal",
    "all_required_milestones_completed",
    "no_open_blocking_risk",
    "no_unresolved_blocking_dependency",
    "all_required_artifacts_exist",
    "artifact_integrity_valid",
    "all_required_verifications_pass",
    "integration_passed",
    "required_delivery_manifest_exists",
    "required_delivery_accepted",
    "required_human_approvals_obtained",
    "no_open_human_controls",
)

_REQUIRED_CRITERIA_SET = frozenset(REQUIRED_CRITERIA)


@dataclass(frozen=True)
class CompletionEvaluation:
    """Immutable result of checking every required completion criterion."""

    passed: bool
    checks: tuple[dict, ...]


def default_completion_criteria() -> dict[str, bool]:
    """Return a fresh policy that enables every required criterion.

    Required criteria are intentionally not configurable in this evaluator:
    callers must provide all of them as literal ``True`` values.
    """

    return {criterion_id: True for criterion_id in REQUIRED_CRITERIA}


class ProjectCompletionEvaluator:
    """Evaluate an authorized completion-fact snapshot deterministically.

    The caller's main-thread loader must authorize and version-bind the
    ``facts`` snapshot (for example, against immutable SQL evidence) before
    invoking this pure function.  The evaluator itself performs no database or
    LLM work and cannot prove that a supplied fact came from that loader.
    """

    @staticmethod
    def evaluate(
        *,
        facts: Mapping[str, object],
        criteria: Mapping[str, object],
    ) -> CompletionEvaluation:
        """Check all required criteria in their canonical order.

        ``criteria`` is a fixed, fail-closed policy: its keys must be exactly
        :data:`REQUIRED_CRITERIA` and every value must be the ``bool`` object
        ``True``.  ``facts`` may omit criteria, but unknown fact keys are
        rejected to catch misspellings.  Only a literal ``True`` fact passes;
        an explicit ``False`` is a failed condition and every other value is a
        missing or invalid fact.
        """

        _validate_criteria(criteria)
        _validate_facts(facts)

        checks = []
        for criterion_id in REQUIRED_CRITERIA:
            if criterion_id not in facts:
                status, code = "FAIL", "fact_missing_or_invalid"
            else:
                value = facts[criterion_id]
                if type(value) is bool and value is True:
                    status, code = "PASS", "satisfied"
                elif type(value) is bool and value is False:
                    status, code = "FAIL", "condition_not_met"
                else:
                    status, code = "FAIL", "fact_missing_or_invalid"
            checks.append(
                {
                    "criterion_id": criterion_id,
                    "status": status,
                    "code": code,
                }
            )

        return CompletionEvaluation(
            passed=all(check["status"] == "PASS" for check in checks),
            checks=tuple(checks),
        )


def _validate_criteria(criteria: Mapping[str, object]) -> None:
    if not isinstance(criteria, Mapping):
        # The public contract reports every malformed criteria policy as a
        # ValueError, including a value that is not a mapping.
        raise ValueError("criteria must be a mapping")  # noqa: TRY004

    if set(criteria) != _REQUIRED_CRITERIA_SET:
        raise ValueError("criteria must contain exactly the required criteria")

    for criterion_id in REQUIRED_CRITERIA:
        if type(criteria[criterion_id]) is not bool or criteria[criterion_id] is not True:
            raise ValueError("all completion criteria must be literal True")


def _validate_facts(facts: Mapping[str, object]) -> None:
    if not isinstance(facts, Mapping):
        # Keep malformed fact snapshots in the evaluator's ValueError API.
        raise ValueError("facts must be a mapping")  # noqa: TRY004

    unknown = set(facts) - _REQUIRED_CRITERIA_SET
    if unknown:
        raise ValueError("facts contains unknown criteria")


__all__ = [
    "REQUIRED_CRITERIA",
    "CompletionEvaluation",
    "ProjectCompletionEvaluator",
    "default_completion_criteria",
]
