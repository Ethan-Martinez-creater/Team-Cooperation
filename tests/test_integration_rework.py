import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_integration_orchestration import worker
from test_integration_service import execute, prepare
from test_team_task_result_projection import finish

from coifesp_harness.agent_runs import AgentRunCheckpointCodec
from coifesp_harness.delivery.repository import INTEGRATION_RUNS
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product.repository import PROJECT_AGENT_RUNS, TEAM_TASKS
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.integration_rework import load_integration_rework
from coifesp_harness.team_agents.task_contracts import PersistentTaskDispatchFactLoader
from coifesp_harness.verification.repository import TASK_VERIFICATIONS


def failed(tmp_path):
    value = prepare(tmp_path)
    result = execute(value, policy={"schema": "coifesp.integration-policy.v1", "mode": "artifact_composition",
        "max_artifacts": 1, "max_total_bytes": 1})
    assert result["status"] == "FAIL"
    return value


def evidence(value):
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        graph = value.integration.work_graph.snapshot(connection, project_id="project-a")
        return load_integration_rework(connection, process=process, graph=graph, task_id="task-a")


def test_integration_fail_production_snapshot_dispatches_new_attempt_and_safe_feedback(tmp_path):
    value = failed(tmp_path)
    assert evidence(value).codes == ("bundle_limit_exceeded",)
    with value.engine.connect() as connection:
        old_verification = dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one())
        row = connection.execute(select(INTEGRATION_RUNS)).mappings().one()
    # Untrusted free text in a check is never copied into a target team's context.
    with value.engine.begin() as connection:
        checks = [{**check, "private_note": "other-team-secret"} for check in row["checks_json"]]
        connection.execute(INTEGRATION_RUNS.update().values(checks_json=checks))
    runner = worker(value)
    result = runner.process_once(worker_id="integration-production")
    assert result.status.value == "APPLIED", result
    assert result.action.value == "dispatch_work"
    with value.engine.connect() as connection:
        bindings = connection.execute(select(PROJECT_AGENT_RUNS).order_by(
            PROJECT_AGENT_RUNS.c.execution_attempt)).mappings().all()
        assert [item["execution_attempt"] for item in bindings] == [1, 2]
        assert bindings[1]["task_contract_version"] == bindings[0]["task_contract_version"]
        assert bindings[1]["initiated_by_principal_id"] == "service:project-orchestrator"
        assert bindings[1]["executed_as_principal_id"] == "team-agent:team-b"
        assert dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one()) == old_verification
    raw = value.runs.load_checkpoint(tenant_id="team-b", run_id=bindings[1]["run_id"])
    checkpoint = AgentRunCheckpointCodec().decode(raw)
    content = json.dumps([item.content for item in checkpoint["context_items"]])
    assert "coifesp.task-integration-rework.v1" in content
    assert "Reduce output file count" in content
    assert "other-team-secret" not in content
    assert evidence(value) is None


@pytest.mark.parametrize("change", ["task_time", "contract", "source_run", "impacted", "identity", "checks"])
def test_stale_or_unbound_integration_failure_cannot_authorize_rework(tmp_path, change):
    value = failed(tmp_path)
    with value.engine.begin() as connection:
        if change == "task_time":
            task = connection.execute(select(TEAM_TASKS)).mappings().one()
            connection.execute(TEAM_TASKS.update().values(updated_at=task["updated_at"] + timedelta(seconds=1)))
        elif change == "contract":
            connection.execute(TEAM_TASKS.update().values(source_contract_version=2, accepted_contract_version=2))
        elif change == "source_run":
            connection.execute(PROJECT_AGENT_RUNS.update().values(task_result_status="invalid_output"))
        elif change == "impacted":
            connection.execute(INTEGRATION_RUNS.update().values(impacted_work_ids_json=[]))
        elif change == "identity":
            connection.execute(INTEGRATION_RUNS.update().values(executed_as="team-agent:team-b"))
        else:
            connection.execute(INTEGRATION_RUNS.update().values(checks_json=[]))
    assert evidence(value) is None


def test_direct_contract_loader_does_not_accept_arbitrary_changes_requested(tmp_path):
    from coifesp_harness.product.service import TeamCollaborationService

    value = prepare(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        task = TeamCollaborationService._task(connection.execute(select(TEAM_TASKS)).mappings().one())
        with pytest.raises(GovernanceConflictError):
            PersistentTaskDispatchFactLoader(engine=value.engine, artifact_content=value.content)(
                connection=connection, process=process, task=task)


def test_failed_composition_reexecutes_verifies_and_integrates_new_attempt(tmp_path):
    value = prepare(tmp_path)
    read = value.content.open_policy_authorized
    value.content.open_policy_authorized = lambda **_: iter([b"broken"])
    assert execute(value)["status"] == "FAIL"
    # Explicit recovery of the injected storage fault, not an Agent permission
    # to rewrite immutable bytes or broaden another team's disclosure.
    value.content.open_policy_authorized = read
    runner = worker(value)
    assert runner.process_once(worker_id="integration-production").action.value == "dispatch_work"
    with value.engine.connect() as connection:
        run_id = connection.execute(select(PROJECT_AGENT_RUNS.c.run_id).order_by(
            PROJECT_AGENT_RUNS.c.execution_attempt.desc()).limit(1)).scalar_one()
    value.dispatched = SimpleNamespace(run_id=run_id)
    finish(value)
    accounting = TeamTaskRunAccounting(repository=value.repository, run_repository=value.runs,
        capability_repository=value.capabilities)
    accounting.settle(run_id=run_id)
    value.projection.project(run_id=run_id)
    value.verifier.verify_run(run_id=run_id)
    for index, expected in enumerate(["enter_verification", "enter_integration", "assemble_integration"]):
        runner.scheduler.enqueue(process_id="process-a", project_id="project-a",
            source_event_id=f"retry-progress:{index}", source_event_type="team_task.verified", payload={})
        result = runner.process_once(worker_id="integration-production")
        assert result.status.value == "APPLIED", result
        assert result.action.value == expected
    with value.repository.transaction() as connection:
        rows = connection.execute(select(INTEGRATION_RUNS).order_by(INTEGRATION_RUNS.c.version)).mappings().all()
        assert [row["status"] for row in rows] == ["FAIL", "PASS"]
        assert rows[1]["verification_refs_json"][0]["source_run_id"] == run_id
        assert value.repository.process(connection, "process-a").phase.value == "DELIVERY"
        usage = value.repository.usage(connection, "process-a")
        assert usage.agent_runs_started == 2 and usage.active_agent_runs == 0
