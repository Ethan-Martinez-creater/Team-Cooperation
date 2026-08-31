from copy import deepcopy

import pytest

from coifesp_harness.delivery.completion import (
    REQUIRED_CRITERIA,
    CompletionEvaluation,
    ProjectCompletionEvaluator,
    default_completion_criteria,
)


def _all_true_facts() -> dict[str, bool]:
    return {criterion_id: True for criterion_id in REQUIRED_CRITERIA}


def test_default_criteria_enables_every_required_criterion() -> None:
    criteria = default_completion_criteria()

    assert tuple(criteria) == REQUIRED_CRITERIA
    assert criteria == {criterion_id: True for criterion_id in REQUIRED_CRITERIA}


def test_all_true_facts_pass_in_fixed_order() -> None:
    evaluation = ProjectCompletionEvaluator.evaluate(
        facts=_all_true_facts(),
        criteria=default_completion_criteria(),
    )

    assert isinstance(evaluation, CompletionEvaluation)
    assert evaluation.passed is True
    assert tuple(check["criterion_id"] for check in evaluation.checks) == REQUIRED_CRITERIA
    assert all(
        check["status"] == "PASS" and check["code"] == "satisfied"
        for check in evaluation.checks
    )


@pytest.mark.parametrize("criterion_id", REQUIRED_CRITERIA)
def test_each_false_fact_fails_closed(criterion_id: str) -> None:
    facts = _all_true_facts()
    facts[criterion_id] = False

    evaluation = ProjectCompletionEvaluator.evaluate(
        facts=facts,
        criteria=default_completion_criteria(),
    )

    failed = [check for check in evaluation.checks if check["status"] == "FAIL"]
    assert evaluation.passed is False
    assert failed == [
        {
            "criterion_id": criterion_id,
            "status": "FAIL",
            "code": "condition_not_met",
        }
    ]


def test_unaccepted_delivery_fails() -> None:
    facts = _all_true_facts()
    facts["required_delivery_accepted"] = False

    evaluation = ProjectCompletionEvaluator.evaluate(
        facts=facts,
        criteria=default_completion_criteria(),
    )

    check = evaluation.checks[REQUIRED_CRITERIA.index("required_delivery_accepted")]
    assert evaluation.passed is False
    assert check == {
        "criterion_id": "required_delivery_accepted",
        "status": "FAIL",
        "code": "condition_not_met",
    }


@pytest.mark.parametrize("value", [None, 1, 0, "true", object()])
def test_non_boolean_fact_is_invalid(value: object) -> None:
    facts = _all_true_facts()
    facts["goal_confirmed"] = value

    evaluation = ProjectCompletionEvaluator.evaluate(
        facts=facts,
        criteria=default_completion_criteria(),
    )

    assert evaluation.passed is False
    assert evaluation.checks[0]["status"] == "FAIL"
    assert evaluation.checks[0]["code"] == "fact_missing_or_invalid"


def test_missing_or_empty_facts_never_pass() -> None:
    evaluation = ProjectCompletionEvaluator.evaluate(
        facts={},
        criteria=default_completion_criteria(),
    )

    assert evaluation.passed is False
    assert len(evaluation.checks) == len(REQUIRED_CRITERIA)
    assert all(
        check["status"] == "FAIL" and check["code"] == "fact_missing_or_invalid"
        for check in evaluation.checks
    )


def test_unknown_fact_key_is_rejected() -> None:
    facts = _all_true_facts()
    facts["goal_confrimed"] = True

    with pytest.raises(ValueError):
        ProjectCompletionEvaluator.evaluate(
            facts=facts,
            criteria=default_completion_criteria(),
        )


@pytest.mark.parametrize(
    "criteria_factory",
    [
        dict,
        lambda: {**default_completion_criteria(), "unexpected": True},
        lambda: {**default_completion_criteria(), "goal_confirmed": False},
        lambda: {**default_completion_criteria(), "goal_confirmed": 1},
        lambda: {**default_completion_criteria(), "goal_confirmed": "true"},
    ],
)
def test_criteria_must_be_exact_and_all_true(criteria_factory) -> None:
    with pytest.raises(ValueError):
        ProjectCompletionEvaluator.evaluate(
            facts=_all_true_facts(),
            criteria=criteria_factory(),
        )


@pytest.mark.parametrize("argument", [None, [], "criteria"])
def test_non_mapping_criteria_is_rejected(argument: object) -> None:
    with pytest.raises(ValueError):
        ProjectCompletionEvaluator.evaluate(facts=_all_true_facts(), criteria=argument)


def test_inputs_are_not_modified_and_result_is_frozen() -> None:
    facts = _all_true_facts()
    criteria = default_completion_criteria()
    facts_before = deepcopy(facts)
    criteria_before = deepcopy(criteria)

    evaluation = ProjectCompletionEvaluator.evaluate(facts=facts, criteria=criteria)

    assert facts == facts_before
    assert criteria == criteria_before
    with pytest.raises(AttributeError):
        evaluation.passed = False
