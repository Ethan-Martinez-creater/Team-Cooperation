import asyncio

import pytest
from sqlalchemy import select
from test_task_verification_service import setup
from test_team_task_result_projection import attach_scheduler

from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.project_process import (
    ProjectProcessScheduler,
    SQLAlchemyProjectProcessWakeupRepository,
)
from coifesp_harness.project_process.runtime import build_project_orchestrator_worker
from coifesp_harness.project_process.worker_loop import ProjectOrchestratorLoop


def test_factory_reuses_existing_database_services_without_launching_runs(tmp_path):
    value = setup(tmp_path, completed=False)
    attach_scheduler(value)
    captures = []

    class Loader:
        def __init__(self, **kwargs):
            captures.append(kwargs)

    worker = build_project_orchestrator_worker(repository=value.repository,
        scheduler=ProjectProcessScheduler(SQLAlchemyProjectProcessWakeupRepository(value.engine)),
        agent_run_service=value.dispatcher.run_service, capability_repository=value.capabilities,
        artifact_content=value.content, snapshot_loader_factory=Loader, worker_id="project-worker")
    assert isinstance(worker, ProjectOrchestratorLoop)
    assert captures[0]["repository"] is value.repository
    dispatcher = worker.runner.effect.dispatcher
    assert dispatcher.run_service is value.dispatcher.run_service
    assert dispatcher.fact_loader is captures[0]["fact_loader"]
    assert dispatcher.capabilities is captures[0]["capability_adapter"]
    assert dispatcher.work_graph is captures[0]["work_graph_repository"]
    assert isinstance(worker.runner.snapshot_loader, Loader)
    with value.engine.connect() as connection:
        assert len(connection.execute(select(AGENT_RUNS)).all()) == 1
        assert value.repository.usage(connection, "process-a").agent_runs_started == 1


def test_mismatched_runtime_engine_is_rejected(tmp_path):
    value = setup(tmp_path, completed=False)

    class WrongResolver:
        engine = object()

    with pytest.raises(ValueError, match="shared database"):
        build_project_orchestrator_worker(repository=value.repository, scheduler=None,
            agent_run_service=value.dispatcher.run_service, capability_repository=value.capabilities,
            artifact_content=value.content, snapshot_loader_factory=lambda **_: None,
            runtime_resolver=WrongResolver())


def test_application_lifespan_consumes_persisted_verification_with_production_loader(tmp_path):
    from test_control_plane import StubVerifier, settings
    from test_verification_orchestration_effect import stack

    from coifesp_harness.control_plane import create_app
    from coifesp_harness.project_process.persistent_snapshot import (
        PersistentProjectOrchestrationSnapshotLoader,
    )
    from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting

    value = stack(tmp_path, fail=False)
    TeamTaskRunAccounting(repository=value.repository, run_repository=value.runs,
                         capability_repository=value.capabilities).replay_pending()
    worker = build_project_orchestrator_worker(repository=value.repository,
        scheduler=value.runner.scheduler, agent_run_service=value.dispatcher.run_service,
        capability_repository=value.capabilities, artifact_content=value.content,
        snapshot_loader_factory=PersistentProjectOrchestrationSnapshotLoader,
        worker_id="production-verification-worker", idle_poll_seconds=.05)
    results = []

    async def scenario():
        done = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        actual = worker.runner.process_once

        def observed(**kwargs):
            result = actual(**kwargs)
            results.append(result)
            event_loop.call_soon_threadsafe(done.set)
            return result

        worker.runner.process_once = observed
        app = create_app(settings=settings(), verifier=StubVerifier({}), readiness_probe=lambda: True)
        app.state.project_orchestrator_worker = worker
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(done.wait(), 10)

    asyncio.run(scenario())
    assert results[0].status.value == "APPLIED", results[0]
    assert results[0].action.value == "enter_integration"
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        assert (process.phase.value, process.status.value) == ("INTEGRATION", "READY")
        events = value.repository.events(connection, "process-a")
        assert len([event for event in events
                    if event.event_type == "project.verification.completed"]) == 1
