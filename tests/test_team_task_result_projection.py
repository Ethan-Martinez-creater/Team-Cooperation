import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from test_agent_run_worker import Provider
from test_agent_run_worker import stack as worker_stack
from test_persistent_team_task_contracts import accept, propose, resource
from test_team_agent_dispatcher import dispatch, record, stack

from coifesp_harness.agent_runs import DurableAgentWorker
from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.artifacts.repository import ARTIFACT_MANIFESTS
from coifesp_harness.control_plane.app import create_app
from coifesp_harness.control_plane.product_routes import build_product_router
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    TEAM_TASKS,
)
from coifesp_harness.project_process import (
    ProjectProcessScheduler,
    SQLAlchemyProjectProcessWakeupRepository,
)
from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
from coifesp_harness.project_process.scheduler import PROJECT_PROCESS_WAKEUPS
from coifesp_harness.runtime import LLMResponse
from coifesp_harness.security import Principal
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.identity import TeamAgentPrincipalResolver
from coifesp_harness.team_agents.task_contracts import (
    PersistentTaskDispatchFactLoader,
    TeamTaskContractService,
)
from coifesp_harness.team_agents.task_projection import TeamTaskResultProjection


def prepare(tmp_path, **contract):
    value = stack(accepted=False)
    value.content = resource(value, tmp_path, owner="team-b")
    propose(value, **contract)
    accept(value)
    value.dispatcher.fact_loader = PersistentTaskDispatchFactLoader(engine=value.engine)
    record(value, "decision-result")
    value.dispatched = dispatch(value, decision_id="decision-result")
    value.projection = TeamTaskResultProjection(
        repository=value.repository,
        run_repository=value.runs,
        artifact_content=value.content,
    )
    return value


def output(**overrides):
    return json.dumps(
        {
            "schema": "coifesp.task-output.v1",
            "artifact_refs": ["resource-input"],
            "summary": "private team note",
            "known_limitations": ["team-only detail"],
            **overrides,
        }
    )


def finish(value, text=None, callback=None):
    _, _, loop = worker_stack(
        provider=Provider(
            response=LLMResponse(
                text=output() if text is None else text,
                input_tokens=7,
                output_tokens=3,
            )
        )
    )

    class NoHumanResolver:
        async def resolve(self, **_):
            raise AssertionError("task machine identity must never resolve as human")

    value.dispatcher.run_service.terminal_callback = callback
    worker = DurableAgentWorker(
        service=value.dispatcher.run_service,
        loop=loop,
        principal_resolver=TeamAgentPrincipalResolver(
            engine=value.engine, human_resolver=NoHumanResolver()
        ),
    )
    outcome = asyncio.run(
        worker.process_once(
            worker=Principal(
                "worker-result",
                "team-b",
                roles=frozenset({"agent_worker"}),
                is_service=True,
            )
        )
    )
    assert outcome.status.value == "completed"


def state(value):
    with value.engine.connect() as connection:
        task = (
            connection.execute(select(TEAM_TASKS).where(TEAM_TASKS.c.task_id == "task-a"))
            .mappings()
            .one()
        )
        binding = (
            connection.execute(
                select(PROJECT_AGENT_RUNS).where(
                    PROJECT_AGENT_RUNS.c.run_id == value.dispatched.run_id,
                )
            )
            .mappings()
            .one()
        )
        events = (
            connection.execute(
                select(PROJECT_PROCESS_EVENTS).where(
                    PROJECT_PROCESS_EVENTS.c.event_type.in_(
                        ("team_task.submitted", "team_task.changes_requested")
                    ),
                )
            )
            .mappings()
            .all()
        )
        return task, binding, events


def attach_scheduler(value):
    repository = SQLAlchemyProjectProcessWakeupRepository(value.engine)
    repository.create_schema()
    scheduler = ProjectProcessScheduler(repository)

    def enqueue(connection, event):
        scheduler.enqueue_in_transaction(
            connection,
            process_id=event.process_id,
            project_id=event.project_id,
            source_event_id=event.event_id,
            source_event_type=event.event_type,
            payload={"event_id": event.event_id},
            available_at=event.occurred_at,
        )

    value.repository.set_event_listener(enqueue)


def test_real_worker_submits_artifacts_accounts_and_wakes_once(tmp_path):
    value = prepare(tmp_path)
    attach_scheduler(value)
    accounting = TeamTaskRunAccounting(
        repository=value.repository,
        run_repository=value.runs,
        capability_repository=value.capabilities,
    )

    def callback(run):
        accounting.on_run_terminal(run)
        value.projection.on_run_terminal(run)

    finish(value, callback=callback)
    task, binding, events = state(value)
    assert task["status"] == "submitted" and task["completed_at"] is None
    assert json.loads(task["artifact_resource_ids"]) == ["resource-input"]
    assert binding["task_contract_version"] == 1
    assert binding["task_result_status"] == "submitted"
    assert binding["task_result_json"]["summary"] == "private team note"
    assert len(events) == 1 and events[0]["event_type"] == "team_task.submitted"
    assert "private team note" not in str(events)
    assert "team-only detail" not in str(events)
    value.projection.project(run_id=value.dispatched.run_id)
    assert value.projection.replay_pending() == 0
    assert len(state(value)[2]) == 1
    with value.engine.connect() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.active_agent_runs == 0 and usage.agent_runs_completed == 1
        assert value.repository.process(connection, "process-a").status.value != "COMPLETED"
        count = connection.execute(
            select(func.count())
            .select_from(PROJECT_PROCESS_WAKEUPS)
            .where(
                PROJECT_PROCESS_WAKEUPS.c.source_event_type == "team_task.submitted",
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.parametrize(
    "text",
    [
        "Task complete!",
        "{}",
        output(artifact_refs=["missing-resource"]),
        output(artifact_refs=[]),
    ],
)
def test_invalid_output_is_durable_rework_not_verification(tmp_path, text):
    value = prepare(tmp_path)
    finish(value, text)
    assert value.projection.replay_pending() == 1
    task, binding, events = state(value)
    assert task["status"] == "changes_requested"
    assert task["completed_at"] is None
    assert binding["task_result_status"] == "invalid_output"
    assert binding["task_result_json"]["summary"] == ""
    assert events[0]["event_type"] == "team_task.changes_requested"
    assert value.projection.replay_pending() == 0


@pytest.mark.parametrize("mutation", ["private", "other_owner", "wrong_media", "manifest_hash"])
def test_unshared_or_mismatched_artifact_never_submits(tmp_path, mutation):
    value = prepare(tmp_path)
    finish(value)
    with value.engine.begin() as connection:
        if mutation == "private":
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
        elif mutation == "other_owner":
            connection.execute(PROJECT_RESOURCES.update().values(owner_team_id="team-a"))
        elif mutation == "wrong_media":
            connection.execute(PROJECT_RESOURCES.update().values(media_type="application/json"))
        else:
            connection.execute(ARTIFACT_MANIFESTS.update().values(sha256="f" * 64))
    assert value.projection.project(run_id=value.dispatched.run_id)
    task, binding, _ = state(value)
    assert task["status"] == "changes_requested"
    assert binding["task_result_status"] == "invalid_output"
    assert json.loads(task["artifact_resource_ids"]) == []


def test_declared_output_media_type_is_enforced(tmp_path):
    value = prepare(
        tmp_path,
        output_contract={
            "artifact_types": ["application/pdf"],
            "required": True,
            "max_count": 1,
        },
    )
    finish(value)
    value.projection.project(run_id=value.dispatched.run_id)
    assert state(value)[1]["task_result_status"] == "invalid_output"


def test_empty_result_only_when_output_contract_explicitly_optional(tmp_path):
    value = prepare(
        tmp_path,
        output_contract={
            "artifact_types": ["text/plain"],
            "required": False,
            "max_count": 1,
        },
    )
    finish(value, output(artifact_refs=[]))
    value.projection.project(run_id=value.dispatched.run_id)
    assert state(value)[0]["status"] == "submitted"


def test_projection_failure_rolls_back_receipt_task_event_and_wakeup_then_replays(
    tmp_path,
):
    value = prepare(tmp_path)
    finish(value)
    attach_scheduler(value)
    original = value.repository.event_listener

    def fail_after_enqueue(connection, event):
        original(connection, event)
        raise RuntimeError("crash after task event before receipt")

    value.repository.set_event_listener(fail_after_enqueue)
    with pytest.raises(RuntimeError, match="crash"):
        value.projection.project(run_id=value.dispatched.run_id)
    task, binding, events = state(value)
    assert task["status"] == "in_progress" and not events
    assert binding["task_result_status"] is None
    with value.engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(PROJECT_PROCESS_WAKEUPS)
            ).scalar_one()
            == 0
        )
    value.repository.set_event_listener(original)
    assert value.projection.replay_pending() == 1
    assert state(value)[0]["status"] == "submitted"
    assert value.projection.replay_pending() == 0


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_failure_releases_task_for_rework_with_receipt(tmp_path, status):
    value = prepare(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(
            AGENT_RUNS.update().values(status=status, completed_at=datetime.now(UTC))
        )
    assert value.projection.replay_pending() == 1
    task, binding, events = state(value)
    assert task["status"] == "changes_requested"
    assert binding["task_result_status"] == status
    assert len(events) == 1


def test_human_submission_cannot_be_overwritten(tmp_path):
    value = prepare(tmp_path)
    finish(value)
    with value.engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.update().values(
                status="submitted", artifact_resource_ids='["human-artifact"]'
            )
        )
    value.projection.project(run_id=value.dispatched.run_id)
    task, binding, events = state(value)
    assert json.loads(task["artifact_resource_ids"]) == ["human-artifact"]
    assert binding["task_result_status"] == "invalid_output" and not events


def test_reaccepted_new_contract_cannot_be_overwritten_by_old_run(tmp_path):
    value = prepare(tmp_path)
    finish(value)
    with value.engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.update().values(
                source_contract_version=2,
                accepted_contract_version=2,
            )
        )
    assert value.projection.project(run_id=value.dispatched.run_id)
    task, binding, events = state(value)
    assert task["status"] == "in_progress" and task["source_contract_version"] == 2
    assert binding["task_result_json"]["error_code"] == "stale_or_unbound_task_contract"
    assert not events


@pytest.mark.parametrize("content", [b"corrupted", b"", b"x" * 100])
def test_actual_artifact_bytes_must_match_manifest(tmp_path, content):
    value = prepare(tmp_path)
    finish(value)

    class CorruptStore:
        def open_policy_authorized(self, **_):
            yield content

    value.projection.artifact_content = CorruptStore()
    assert value.projection.project(run_id=value.dispatched.run_id)
    task, binding, events = state(value)
    assert task["status"] == "changes_requested"
    assert binding["task_result_status"] == "invalid_output"
    assert len(events) == 1 and not events[0]["payload_json"].get("artifact_resource_ids")


def test_receipts_visible_only_to_executing_team(tmp_path):
    value = prepare(tmp_path)
    finish(value)
    value.projection.project(run_id=value.dispatched.run_id)
    service = TeamTaskContractService(value.engine)
    assert (
        service.results(project_id="project-a", task_id="task-a", actor_id="lead-b")[0]["summary"]
        == "private team note"
    )
    with pytest.raises(PolicyDenied):
        service.results(project_id="project-a", task_id="task-a", actor_id="lead-a")


def test_runtime_store_unavailable_defers_receipt_and_later_replay_succeeds(tmp_path):
    value = prepare(tmp_path)
    finish(value)
    value.projection.artifact_content = None
    with pytest.raises(RuntimeError, match="reader"):
        value.projection.project(run_id=value.dispatched.run_id)
    assert state(value)[1]["task_result_status"] is None
    value.projection.artifact_content = value.content
    assert value.projection.replay_pending() == 1
    assert state(value)[0]["status"] == "submitted"


def test_callback_cannot_override_durable_run_identity(tmp_path):
    value = prepare(tmp_path)
    finish(value)
    with value.engine.begin() as connection:
        connection.execute(AGENT_RUNS.update().values(owner_principal_id="team-agent:team-a"))
    with pytest.raises(GovernanceConflictError, match="identity"):
        value.projection.project(run_id=value.dispatched.run_id)
    assert state(value)[1]["task_result_status"] is None


def test_http_execution_results_uses_authenticated_executing_team(tmp_path):
    value = prepare(tmp_path)
    finish(value)
    value.projection.project(run_id=value.dispatched.run_id)
    app = FastAPI()

    async def auth(request: Request):
        return SimpleNamespace(
            principal=SimpleNamespace(
                principal_id=request.headers["x-test-actor"],
            )
        )

    app.include_router(
        build_product_router(
            authenticator=auth,
            accounts=None,
            directory=None,
            collaboration=TeamCollaborationService(value.engine),
        )
    )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            path = "/v1/projects/project-a/tasks/task-a/execution-results"
            response = await client.get(path, headers={"x-test-actor": "lead-b"})
            assert response.status_code == 200
            assert response.json()[0]["run_id"] == value.dispatched.run_id
            with pytest.raises(PolicyDenied):
                await client.get(path, headers={"x-test-actor": "lead-a"})

    asyncio.run(scenario())


def test_application_lifespan_replays_unprojected_task_result(tmp_path):
    from test_bootstrap import StubVerifier, production_settings

    value = prepare(tmp_path)
    finish(value)
    assert state(value)[1]["task_result_status"] is None
    app = create_app(settings=production_settings(), verifier=StubVerifier())
    app.state.agent_run_service = value.dispatcher.run_service
    app.state.team_task_result_projection = value.projection

    async def startup():
        async with app.router.lifespan_context(app):
            assert state(value)[0]["status"] == "submitted"

    asyncio.run(startup())
    assert len(state(value)[2]) == 1
