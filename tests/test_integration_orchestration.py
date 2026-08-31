import pytest
from sqlalchemy import func, select
from test_integration_service import prepare

from coifesp_harness.delivery.repository import INTEGRATION_RUNS
from coifesp_harness.project_process.orchestrator import DeterministicAction
from coifesp_harness.project_process.persistent_snapshot import (
    PersistentProjectOrchestrationSnapshotLoader,
)
from coifesp_harness.project_process.runtime import build_project_orchestrator_worker
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting


def worker(value):
    TeamTaskRunAccounting(repository=value.repository, run_repository=value.runs,
        capability_repository=value.capabilities).replay_pending()
    scheduler = value.runner.scheduler
    scheduler.enqueue(process_id="process-a", project_id="project-a", source_event_id="wake-integration",
        source_event_type="project.verification.completed", payload={})
    return build_project_orchestrator_worker(repository=value.repository, scheduler=scheduler,
        agent_run_service=value.dispatcher.run_service, capability_repository=value.capabilities,
        artifact_content=value.content, snapshot_loader_factory=PersistentProjectOrchestrationSnapshotLoader,
        worker_id="integration-production").runner


def test_production_snapshot_and_runner_assemble_real_delivery_bundle(tmp_path):
    value = prepare(tmp_path)
    runner = worker(value)
    result = runner.process_once(worker_id="integration-production")
    assert result.status.value == "APPLIED", result
    assert result.action is DeterministicAction.ASSEMBLE_INTEGRATION
    with value.repository.transaction() as connection:
        assert value.repository.process(connection, "process-a").phase.value == "DELIVERY"
        integration = connection.execute(select(INTEGRATION_RUNS)).mappings().one()
        event = value.repository.event(connection, f"event:{result.decision_id}")
        assert event.subject_id == integration["integration_id"]
        assert event.transition_key == "integration.passed"
        assert event.initiated_by == "service:project-orchestrator"
        assert event.executed_as == "service:project-integrator"


def test_integration_commit_before_ack_recovery_does_not_republish(tmp_path):
    value = prepare(tmp_path)
    runner = worker(value)
    attempts = []

    def crash_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("process crash after commit")

    runner.after_effect = crash_once
    assert runner.process_once(worker_id="integration-production").status.value == "RETRY"
    assert runner.process_once(worker_id="integration-production").status.value == "APPLIED"
    with value.repository.transaction() as connection:
        assert connection.execute(select(func.count()).select_from(INTEGRATION_RUNS)).scalar_one() == 1
        events = value.repository.events(connection, "process-a")
        assert sum(event.event_type == "project.integration.completed" for event in events) == 1


def test_missing_atomic_integration_effect_never_generates_pass(tmp_path):
    value = prepare(tmp_path)
    runner = worker(value)
    runner.effect = lambda **_: None
    result = runner.process_once(worker_id="integration-production")
    assert result.status.value == "RETRY"
    with value.repository.transaction() as connection:
        assert value.repository.process(connection, "process-a").phase.value == "INTEGRATION"
        assert connection.execute(select(func.count()).select_from(INTEGRATION_RUNS)).scalar_one() == 0


@pytest.mark.parametrize("available", [False, True])
def test_storage_availability_is_explicit_in_deterministic_decision(tmp_path, available):
    value = prepare(tmp_path)
    runner = worker(value)
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
    snapshot = runner.snapshot_loader(process)
    decision = runner.orchestrator.decide(process=process, readiness=snapshot.readiness,
        integration_available=available)
    assert decision.action is (DeterministicAction.ASSEMBLE_INTEGRATION if available
                               else DeterministicAction.WAIT_FOR_INTEGRATION)
