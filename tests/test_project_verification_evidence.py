from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_task_verification_service import evidence, policy, setup, verify

from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    TEAM_TASKS,
)
from coifesp_harness.verification.project_evidence import (
    load_project_verification_evidence,
)
from coifesp_harness.verification.repository import TASK_VERIFICATIONS
from coifesp_harness.work_graph.repository import SQLAlchemyWorkGraphRepository


def load(value, transform=lambda graph: graph):
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        graph = SQLAlchemyWorkGraphRepository(value.engine).snapshot(connection, project_id="project-a")
        return load_project_verification_evidence(connection, process=process, graph=transform(graph))


def test_current_pass_is_bound_to_real_evidence_without_writes(tmp_path):
    value = setup(tmp_path)
    assert load(value).outcome is None
    result = verify(value)
    first = load(value)
    assert first.outcome == "PASSED" and first.passed_task_ids == ("task-a",)
    assert first.verification_ids == (result["verification_id"],)
    assert load(value) == first
    assert evidence(value)[0]["version"] == result["version"]


def test_manual_verified_status_without_evidence_is_not_pass(tmp_path):
    value = setup(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(status="verified"))
    assert load(value).pending_task_ids == ("task-a",)


def test_fail_is_actionable_even_when_resource_was_withdrawn(tmp_path):
    value = setup(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
    assert verify(value)["status"] == "FAIL"
    assert load(value).outcome == "FAILED"
    assert load(value).failed_task_ids == ("task-a",)


def test_human_review_pending_cannot_enter_integration(tmp_path):
    value = setup(tmp_path, verification_policy=policy(
        {"criterion_id": "accept", "type": "human_review", "required": True}))
    assert verify(value)["status"] == "PENDING"
    assert load(value).outcome is None


@pytest.mark.parametrize("change", ["contract", "task_time", "resource", "digest", "receipt", "binding"])
def test_historical_pass_does_not_approve_changed_subject(tmp_path, change):
    value = setup(tmp_path)
    verify(value)
    with value.engine.begin() as connection:
        if change == "contract":
            connection.execute(TEAM_TASKS.update().values(source_contract_version=2, accepted_contract_version=2))
        elif change == "task_time":
            task = connection.execute(select(TEAM_TASKS)).mappings().one()
            connection.execute(TEAM_TASKS.update().values(updated_at=task["updated_at"] + timedelta(seconds=1)))
        elif change == "resource":
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
        elif change == "digest":
            connection.execute(TASK_VERIFICATIONS.update().values(subject_digest="f" * 64))
        elif change == "binding":
            connection.execute(PROJECT_AGENT_RUNS.update().values(team_id="team-a", executed_as_principal_id="team-agent:team-a"))
        else:
            row = connection.execute(select(PROJECT_AGENT_RUNS)).mappings().one()
            receipt = {**row["task_result_json"], "artifact_refs": []}
            connection.execute(PROJECT_AGENT_RUNS.update().values(task_result_json=receipt))
    assert load(value).outcome is None
    assert evidence(value)[0]["status"] == "PASS"  # historical evidence is immutable


def test_new_execution_attempt_invalidates_previous_pass(tmp_path):
    value = setup(tmp_path)
    verify(value)
    with value.engine.begin() as connection:
        row = dict(connection.execute(select(PROJECT_AGENT_RUNS)).mappings().one())
        row.update(run_id="run-new", execution_attempt=2, orchestration_decision_id="decision-new",
                   capacity_reservation_id="capacity-new", project_budget_reservation_id="budget-new",
                   task_result_status=None, task_result_json=None, task_result_at=None)
        connection.execute(PROJECT_AGENT_RUNS.insert().values(**row))
    assert load(value).outcome is None


def test_empty_or_wrong_project_graph_never_passes(tmp_path):
    value = setup(tmp_path)
    verify(value)
    assert load(value, lambda graph: replace(graph, nodes=())).outcome is None
    with pytest.raises(ValueError, match="another project"):
        load(value, lambda graph: replace(graph, project_id="other"))
    with pytest.raises(ValueError, match="duplicate"):
        load(value, lambda graph: replace(graph, nodes=graph.nodes + graph.nodes))
