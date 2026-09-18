import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_team_agent_dispatcher import dispatch, record
from test_team_task_result_projection import finish, prepare

from coifesp_harness.agent_runs import AgentRunCheckpointCodec
from coifesp_harness.product.repository import PROJECT_AGENT_RUNS, TEAM_TASKS
from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.task_output_rework import load_task_output_rework


def invalid_output(tmp_path):
    value = prepare(tmp_path)
    finish(value, "Task complete, but not protocol JSON")
    TeamTaskRunAccounting(
        repository=value.repository,
        run_repository=value.runs,
        capability_repository=value.capabilities,
    ).settle(run_id=value.dispatched.run_id)
    assert value.projection.project(run_id=value.dispatched.run_id)
    return value


def evidence(value):
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        graph = value.dispatcher.work_graph.snapshot(
            connection, project_id="project-a"
        )
        return load_task_output_rework(
            connection,
            process=process,
            graph=graph,
            task_id="task-a",
        )


def test_invalid_output_dispatches_bounded_second_attempt_with_protocol_feedback(tmp_path):
    value = invalid_output(tmp_path)
    first = evidence(value)
    assert first.error_code == "invalid_task_output"
    assert first.execution_attempt == 1

    record(value, decision_id="decision-output-rework")
    retried = dispatch(value, decision_id="decision-output-rework")
    assert retried.execution_attempt == 2
    raw = value.runs.load_checkpoint(tenant_id="team-b", run_id=retried.run_id)
    checkpoint = AgentRunCheckpointCodec().decode(raw)
    content = json.dumps(
        [item.content for item in checkpoint["context_items"]], ensure_ascii=False
    )
    assert "coifesp.task-output-rework.v1" in content
    assert "coifesp.task-output.v1 JSON object" in content
    assert "Task complete, but not protocol JSON" not in content
    assert evidence(value) is None


@pytest.mark.parametrize("mutation", ["task_time", "contract", "receipt", "event"])
def test_stale_or_tampered_task_output_receipt_cannot_authorize_rework(tmp_path, mutation):
    value = invalid_output(tmp_path)
    with value.engine.begin() as connection:
        if mutation == "task_time":
            task = connection.execute(select(TEAM_TASKS)).mappings().one()
            connection.execute(
                TEAM_TASKS.update().values(
                    updated_at=task["updated_at"] + timedelta(seconds=1)
                )
            )
        elif mutation == "contract":
            connection.execute(
                TEAM_TASKS.update().values(
                    source_contract_version=2,
                    accepted_contract_version=2,
                )
            )
        elif mutation == "receipt":
            binding = connection.execute(select(PROJECT_AGENT_RUNS)).mappings().one()
            receipt = dict(binding["task_result_json"])
            receipt["run_id"] = "another-run"
            connection.execute(
                PROJECT_AGENT_RUNS.update().values(task_result_json=receipt)
            )
        else:
            event = connection.execute(
                select(PROJECT_PROCESS_EVENTS).where(
                    PROJECT_PROCESS_EVENTS.c.event_type
                    == "team_task.changes_requested"
                )
            ).mappings().one()
            payload = dict(event["payload_json"])
            payload["error_code"] = "another_error"
            connection.execute(
                PROJECT_PROCESS_EVENTS.update()
                .where(PROJECT_PROCESS_EVENTS.c.event_id == event["event_id"])
                .values(payload_json=payload)
            )
    assert evidence(value) is None
