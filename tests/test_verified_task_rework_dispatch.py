import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_task_verification_service import setup as verification_setup
from test_task_verification_service import verify
from test_team_agent_dispatcher import dispatch, record

from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    TEAM_TASKS,
)
from coifesp_harness.project_process.budget import ProjectBudgetExhausted
from coifesp_harness.project_process.repository import PROJECT_EXECUTION_POLICIES
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.task_contracts import PersistentTaskDispatchFactLoader


def _failed_value(tmp_path):
    value = verification_setup(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
    assert verify(value)["status"] == "FAIL"
    accounting = TeamTaskRunAccounting(
        repository=value.repository,
        run_repository=value.runs,
        capability_repository=value.capabilities,
    )
    assert accounting.settle(run_id=value.dispatched.run_id) is True
    value.dispatcher.fact_loader = PersistentTaskDispatchFactLoader(engine=value.engine)
    return value


def _binding_rows(value):
    with value.engine.connect() as connection:
        return connection.execute(
            select(PROJECT_AGENT_RUNS).order_by(PROJECT_AGENT_RUNS.c.execution_attempt)
        ).mappings().all()


def test_current_fail_rework_dispatches_new_attempt_without_contract_change(tmp_path):
    value = _failed_value(tmp_path)
    with value.engine.connect() as connection:
        before = connection.execute(select(TEAM_TASKS)).mappings().one()
    record(value, "decision-rework")

    result = dispatch(value, decision_id="decision-rework")
    assert result.execution_attempt == 2
    rows = _binding_rows(value)
    assert len(rows) == 2
    assert [row["execution_attempt"] for row in rows] == [1, 2]
    assert [row["task_contract_version"] for row in rows] == [1, 1]
    with value.engine.connect() as connection:
        task = connection.execute(select(TEAM_TASKS)).mappings().one()
        usage = value.repository.usage(connection, "process-a")
    assert task["status"] == "in_progress"
    assert task["source_contract_version"] == before["source_contract_version"]
    assert task["accepted_contract_version"] == before["accepted_contract_version"]
    assert usage.agent_runs_started == 2 and usage.active_agent_runs == 1

    checkpoint = value.runs.load_checkpoint(tenant_id="team-b", run_id=result.run_id)
    items = [
        json.loads(item.content)
        for item in value.dispatcher.run_service.checkpoint_codec.decode(checkpoint)["context_items"]
    ]
    feedback = next(item for item in items if item.get("schema") == "coifesp.task-rework-feedback.v1")
    assert feedback["status"] == "FAIL"
    assert feedback["findings"] == [{
        "criterion_id": "__artifact_integrity__",
        "type": "tool_check",
        "code": "submission_artifacts_changed",
    }]
    assert "private team note" not in json.dumps(feedback)


def test_rework_decision_retry_is_idempotent(tmp_path):
    value = _failed_value(tmp_path)
    record(value, "decision-rework")
    first = dispatch(value, decision_id="decision-rework")
    repeated = dispatch(value, decision_id="decision-rework")
    assert repeated.run_id == first.run_id and repeated.duplicate is True
    assert len(_binding_rows(value)) == 2


@pytest.mark.parametrize("mutation", ["manual_status", "old_fail", "contract"])
def test_only_current_fail_evidence_can_authorize_changes_requested_dispatch(tmp_path, mutation):
    if mutation == "manual_status":
        value = verification_setup(tmp_path)
        assert verify(value)["status"] == "PASS"
        accounting = TeamTaskRunAccounting(
            repository=value.repository,
            run_repository=value.runs,
            capability_repository=value.capabilities,
        )
        assert accounting.settle(run_id=value.dispatched.run_id) is True
        with value.engine.begin() as connection:
            connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
    else:
        value = _failed_value(tmp_path)
        with value.engine.begin() as connection:
            if mutation == "old_fail":
                task = connection.execute(select(TEAM_TASKS)).mappings().one()
                connection.execute(TEAM_TASKS.update().values(
                    updated_at=task["updated_at"] + timedelta(seconds=1),
                ))
            else:
                connection.execute(TEAM_TASKS.update().values(
                    source_contract_version=2,
                    accepted_contract_version=2,
                ))
    value.dispatcher.fact_loader = PersistentTaskDispatchFactLoader(engine=value.engine)
    record(value, f"decision-{mutation}")
    with pytest.raises(GovernanceConflictError, match="current verification FAIL evidence"):
        dispatch(value, decision_id=f"decision-{mutation}")
    assert len(_binding_rows(value)) == 1
    with value.engine.connect() as connection:
        assert connection.execute(select(TEAM_TASKS.c.status)).scalar_one() == "changes_requested"


def test_active_previous_run_blocks_evidence_bound_rework(tmp_path):
    value = _failed_value(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(AGENT_RUNS.update().values(
            status="running",
            lease_owner="worker-rework",
            lease_token="token-rework",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            completed_at=None,
            updated_at=datetime.now(UTC),
        ))
    record(value, "decision-active")
    with pytest.raises(GovernanceConflictError, match="active execution"):
        dispatch(value, decision_id="decision-active")
    assert len(_binding_rows(value)) == 1


def test_rework_fence_failure_rolls_back_new_attempt_and_task_transition(tmp_path):
    value = _failed_value(tmp_path)
    record(value, "decision-fence")
    calls = 0

    def fence(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GovernanceConflictError("lease lost")

    with pytest.raises(GovernanceConflictError, match="lease lost"):
        dispatch(value, decision_id="decision-fence", fence=fence)
    assert len(_binding_rows(value)) == 1
    with value.engine.connect() as connection:
        assert connection.execute(select(TEAM_TASKS.c.status)).scalar_one() == "changes_requested"


def test_rework_budget_exhaustion_leaves_task_and_binding_unchanged(tmp_path):
    value = _failed_value(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_EXECUTION_POLICIES.update().where(
            PROJECT_EXECUTION_POLICIES.c.policy_id == "policy-a",
            PROJECT_EXECUTION_POLICIES.c.version == 1,
        ).values(max_total_tokens=0))
    record(value, "decision-budget")
    with pytest.raises(ProjectBudgetExhausted):
        dispatch(value, decision_id="decision-budget")
    assert len(_binding_rows(value)) == 1
    with value.engine.connect() as connection:
        assert connection.execute(select(TEAM_TASKS.c.status)).scalar_one() == "changes_requested"
