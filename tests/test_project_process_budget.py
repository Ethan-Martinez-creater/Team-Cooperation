from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.project_process import (
    AdmissionRequest,
    ProjectBudgetEvaluator,
    ProjectExecutionPolicy,
    ProjectExecutionUsage,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _policy():
    return ProjectExecutionPolicy(
        policy_id="policy-1",
        project_id="project-1",
        max_agent_runs=10,
        max_total_tokens=1000,
        max_model_cost_microusd=10000,
        max_replans=3,
        max_generated_tasks=20,
        max_active_agent_runs=4,
        max_active_runs_per_team=2,
        max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=NOW + timedelta(days=1),
        version=1,
    )


def _usage():
    return ProjectExecutionUsage(
        process_id="process-1",
        project_id="project-1",
        agent_runs_started=1,
        agent_runs_completed=1,
        total_tokens=100,
        model_cost_microusd=100,
        replan_count=0,
        generated_task_count=1,
        active_agent_runs=0,
        version=1,
    )


def test_budget_allows_below_limits_and_zero_specialist_policy_for_normal_run():
    evaluator = ProjectBudgetEvaluator()
    evaluator.check(policy=_policy(), usage=_usage(), request=AdmissionRequest(0, now=NOW))
    evaluator.check(
        policy=replace(_policy(), max_specialist_depth=0, max_specialist_runs_per_task=0),
        usage=_usage(),
        request=AdmissionRequest(0, specialist_depth=0, now=NOW),
    )


@pytest.mark.parametrize(
    ("usage_field", "policy_field"),
    [
        ("agent_runs_started", "max_agent_runs"),
        ("total_tokens", "max_total_tokens"),
        ("model_cost_microusd", "max_model_cost_microusd"),
        ("replan_count", "max_replans"),
        ("generated_task_count", "max_generated_tasks"),
        ("active_agent_runs", "max_active_agent_runs"),
    ],
)
def test_each_project_usage_limit_fails_closed(usage_field, policy_field):
    policy = _policy()
    changes = {usage_field: getattr(policy, policy_field)}
    if usage_field == "active_agent_runs":
        changes["agent_runs_started"] = changes[usage_field] + 1
    usage = replace(_usage(), **changes)
    with pytest.raises(GovernanceConflictError, match="exhausted"):
        ProjectBudgetEvaluator().check(
            policy=policy, usage=usage, request=AdmissionRequest(0, now=NOW)
        )


def test_team_specialist_and_deadline_limits_fail_closed():
    evaluator = ProjectBudgetEvaluator()
    with pytest.raises(GovernanceConflictError, match="team run"):
        evaluator.check(policy=_policy(), usage=_usage(), request=AdmissionRequest(2, now=NOW))
    with pytest.raises(GovernanceConflictError, match="depth"):
        evaluator.check(
            policy=_policy(),
            usage=_usage(),
            request=AdmissionRequest(0, specialist_depth=3, now=NOW),
        )
    with pytest.raises(GovernanceConflictError, match="specialist run"):
        evaluator.check(
            policy=_policy(),
            usage=_usage(),
            request=AdmissionRequest(0, specialist_depth=1, specialist_runs_for_task=2, now=NOW),
        )
    with pytest.raises(GovernanceConflictError, match="deadline"):
        evaluator.check(
            policy=_policy(),
            usage=_usage(),
            request=AdmissionRequest(0, now=NOW + timedelta(days=1)),
        )


def test_policy_rejects_negative_limits_and_zero_version():
    with pytest.raises(ValueError):
        replace(_policy(), max_agent_runs=-1)
    with pytest.raises(ValueError):
        replace(_policy(), version=0)
