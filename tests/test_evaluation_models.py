from decimal import Decimal

import pytest

from coifesp_harness.evaluation import (
    EvaluationCase,
    EvaluationSuite,
    GateThresholds,
    MatchOperator,
    OutputRule,
    PolicyExpectation,
    RedTeamCategory,
    SensitiveValue,
)


def test_suite_and_cases_require_versioned_unique_identities() -> None:
    case = EvaluationCase(
        case_id="policy.local-read",
        version="1.2.0",
        expected_policy=PolicyExpectation.ALLOW,
    )
    suite = EvaluationSuite("security-regression", "2.0.0", (case,))

    assert suite.key == "security-regression@2.0.0"
    assert case.key == "policy.local-read@1.2.0"

    with pytest.raises(ValueError, match="semantic version"):
        EvaluationCase("case", "latest", PolicyExpectation.DENY)
    with pytest.raises(ValueError, match="unique"):
        EvaluationSuite("suite", "1.0.0", (case, case))


def test_case_snapshots_json_input_and_hides_sensitive_fields_from_repr() -> None:
    payload = {"messages": [{"text": "private request"}]}
    case = EvaluationCase(
        case_id="redteam.exfiltration",
        version="1.0.0",
        expected_policy=PolicyExpectation.DENY,
        input_payload=payload,
        output_rules=(OutputRule("no-export", MatchOperator.EXCLUDES, "canary"),),
        sensitive_values=(SensitiveValue("tenant-canary", "canary-secret-value"),),
    )
    payload["messages"][0]["text"] = "mutated"

    assert case.input_payload["messages"][0]["text"] == "private request"
    with pytest.raises(TypeError):
        case.input_payload["extra"] = "not allowed"
    rendered = repr(case)
    assert "private request" not in rendered
    assert "canary-secret-value" not in rendered


def test_thresholds_are_exact_validated_rates_and_immutable_mappings() -> None:
    thresholds = GateThresholds(
        minimum_pass_rate="0.95",
        category_minimum_pass_rates={RedTeamCategory.PROMPT_INJECTION: "0.80"},
        required_red_team_categories=frozenset({RedTeamCategory.PROMPT_INJECTION}),
    )

    assert thresholds.minimum_pass_rate == Decimal("0.95")
    assert (
        thresholds.category_minimum_pass_rates[RedTeamCategory.PROMPT_INJECTION]
        == Decimal("0.80")
    )
    with pytest.raises(TypeError):
        thresholds.category_minimum_pass_rates[RedTeamCategory.SECRET_LEAKAGE] = Decimal(1)
    with pytest.raises(ValueError, match="between 0 and 1"):
        GateThresholds(minimum_pass_rate="1.01")


def test_invalid_payload_and_duplicate_non_reportable_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="JSON-compatible"):
        EvaluationCase(
            "case",
            "1.0.0",
            PolicyExpectation.ALLOW,
            input_payload={"opaque": object()},
        )
    with pytest.raises(ValueError, match="unique"):
        EvaluationCase(
            "case",
            "1.0.0",
            PolicyExpectation.ALLOW,
            output_rules=(
                OutputRule("same", MatchOperator.CONTAINS, "a"),
                OutputRule("same", MatchOperator.EXCLUDES, "b"),
            ),
        )
