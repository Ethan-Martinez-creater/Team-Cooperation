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
