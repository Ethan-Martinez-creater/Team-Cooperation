import pytest
from sqlalchemy import select
from test_task_verification_service import setup, verify

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product.repository import PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.project_process import (
    ProjectProcessCommandService,
    ProjectProcessScheduler,
    ProjectProcessService,
    SQLAlchemyProjectProcessWakeupRepository,
)
from coifesp_harness.project_process.orchestrator import DeterministicAction
from coifesp_harness.project_process.readiness import (
    ProjectReadinessEvaluator,
    ProjectReadinessSnapshot,
)
from coifesp_harness.project_process.runner import (
    ProjectOrchestrationSnapshot,
    ProjectOrchestratorRunner,
)
from coifesp_harness.project_process.verification_effect import (
    VerificationOrchestrationEffect,
)
from coifesp_harness.verification.project_evidence import (
    load_project_verification_evidence,
)
from coifesp_harness.work_graph.repository import SQLAlchemyWorkGraphRepository


def stack(tmp_path, *, after_effect=None, fail=True):
    value = setup(tmp_path)
    if fail:
        with value.engine.begin() as connection:
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
    verify(value)
    service = ProjectProcessService(value.repository)
    for key, kind in [("work.dispatched", "project.work.dispatched"),
                      ("all_required_work_submitted", "project.work.required_submitted")]:
        with value.repository.transaction() as connection:
            process = value.repository.process(connection, "process-a")
        service.apply_transition(process_id=process.process_id, event_id="setup:" + key,
            event_type=kind, transition_key=key, expected_version=process.version,
            subject_type="project", subject_id=process.project_id, initiated_by="lead-a",
            executed_as="service:project-orchestrator", correlation_id="verification-test", payload={})
    wakeups = SQLAlchemyProjectProcessWakeupRepository(value.engine)
    wakeups.create_schema()
    scheduler = ProjectProcessScheduler(wakeups, retry_delay=lambda **_: 0)
    scheduler.enqueue(process_id="process-a", project_id="project-a",
        source_event_id="setup:all_required_work_submitted", source_event_type="project.work.required_submitted",
        payload={})
    graph_repository = SQLAlchemyWorkGraphRepository(value.engine)

    def loader(process):
        with value.repository.transaction() as connection:
            graph = graph_repository.snapshot(connection, project_id=process.project_id)
            evidence = load_project_verification_evidence(connection, process=process, graph=graph)
        return ProjectOrchestrationSnapshot(graph_digest=graph.digest,
            readiness=ProjectReadinessEvaluator().evaluate(ProjectReadinessSnapshot(tasks=())),
            verification_outcome=evidence.outcome)

    value.effect = VerificationOrchestrationEffect(repository=value.repository,
        work_graph_repository=graph_repository, dispatcher=value.dispatcher)
    value.runner = ProjectOrchestratorRunner(repository=value.repository, process_service=service,
        command_service=ProjectProcessCommandService(value.repository), scheduler=scheduler,
        snapshot_loader=loader, effect=value.effect, after_effect=after_effect)
    return value


def test_runner_reopens_failed_verification_without_rewriting_contract_or_budget(tmp_path):
    value = stack(tmp_path)
    with value.repository.transaction() as connection:
        usage = value.repository.usage(connection, "process-a")
        task = dict(connection.execute(select(TEAM_TASKS)).mappings().one())
    result = value.runner.process_once(worker_id="project-worker")
    assert result.status.value == "APPLIED" and result.action is DeterministicAction.REOPEN_WORK
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        assert (process.phase.value, process.status.value) == ("EXECUTION", "READY")
        assert value.repository.usage(connection, "process-a") == usage
        assert dict(connection.execute(select(TEAM_TASKS)).mappings().one()) == task
        events = [event for event in value.repository.events(connection, "process-a")
                  if event.event_type == "project.verification.completed"]
        assert len(events) == 1
        assert events[0].payload["outcome"] == "FAIL"


def test_current_pass_enters_integration_not_delivery_or_completion(tmp_path):
    value = stack(tmp_path, fail=False)
    result = value.runner.process_once(worker_id="project-worker")
    assert result.status.value == "APPLIED" and result.action is DeterministicAction.ENTER_INTEGRATION
    with value.repository.transaction() as connection:
        assert value.repository.process(connection, "process-a").phase.value == "INTEGRATION"
        event = value.repository.event(connection, "event:" + result.decision_id)
        assert event.payload["outcome"] == "PASS"


def test_pass_revoked_between_snapshot_and_effect_cannot_advance(tmp_path):
    value = stack(tmp_path, fail=False)
    original = value.effect.apply_with_event

    def revoke(**kwargs):
        with value.engine.begin() as connection:
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
        return original(**kwargs)

    value.effect.apply_with_event = revoke
    assert value.runner.process_once(worker_id="project-worker").status.value == "RETRY"
    with value.repository.transaction() as connection:
        assert value.repository.process(connection, "process-a").phase.value == "VERIFICATION"


def test_commit_before_ack_replay_does_not_repeat_transition(tmp_path):
    attempts = []

    def crash_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("crash before wakeup ack")

    value = stack(tmp_path, after_effect=crash_once)
    first = value.runner.process_once(worker_id="project-worker")
    assert first.status.value == "RETRY"
    second = value.runner.process_once(worker_id="project-worker")
    assert second.status.value == "APPLIED"
    with value.repository.transaction() as connection:
        assert len([event for event in value.repository.events(connection, "process-a")
                    if event.event_type == "project.verification.completed"]) == 1


@pytest.mark.parametrize("fault", ["publisher", "fence", "missing_evidence", "changed_graph"])
def test_reopening_failure_rolls_back_or_refuses_stale_evidence(tmp_path, fault):
    value = stack(tmp_path)
    original = value.effect.apply_with_event

    def fault_effect(**kwargs):
        if fault == "publisher":
            kwargs["publish_dispatch"] = lambda connection: None
        elif fault == "fence":
            def deny(connection):
                raise GovernanceConflictError("worker fence lost")
            kwargs["mutation_fence"] = deny
        else:
            with value.engine.begin() as connection:
                # Mutate after snapshot/decision capture, before effect admission.
                if fault == "missing_evidence":
                    from coifesp_harness.verification.repository import (
                        TASK_VERIFICATIONS,
                    )
                    connection.execute(TASK_VERIFICATIONS.update().values(subject_digest="f" * 64))
                else:
                    connection.execute(TEAM_TASKS.update().values(title="Scope changed"))
        return original(**kwargs)

    value.effect.apply_with_event = fault_effect
    assert value.runner.process_once(worker_id="project-worker").status.value == "RETRY"
    with value.repository.transaction() as connection:
        assert value.repository.process(connection, "process-a").phase.value == "VERIFICATION"
        assert not [event for event in value.repository.events(connection, "process-a")
                    if event.event_type == "project.verification.completed"]


def test_failed_submission_reopens_reruns_and_passes_with_same_contract(tmp_path):
    from types import SimpleNamespace

    from test_team_agent_dispatcher import dispatch, record
    from test_team_task_result_projection import finish, state

    from coifesp_harness.verification.worker_runtime import configure_worker_reviews

    value = stack(tmp_path)
    recovery = configure_worker_reviews(settings=SimpleNamespace(artifact_store_root=str(tmp_path),
        artifact_max_upload_bytes=100000, sandbox_profiles_json=None), engine=value.engine,
        service=value.dispatcher.run_service, jobs=None, audit=value.capabilities.audit_log)
    recovery.reconcile(tenant_id="team-b")
    assert value.runner.process_once(worker_id="project-worker").status.value == "APPLIED"
    first_run = value.dispatched.run_id
    # Fixture repair represents restoring the explicitly shared deliverable;
    # it does not grant the Agent a new sharing permission or change its contract.
    with value.engine.begin() as connection:
        connection.execute(PROJECT_RESOURCES.update().values(propagation="project_readonly"))
    record(value, "decision-second-attempt")
    value.dispatched = dispatch(value, decision_id="decision-second-attempt")
    assert value.dispatched.execution_attempt == 2 and value.dispatched.run_id != first_run
    finish(value, callback=value.dispatcher.run_service.terminal_callback)
    task, _, _ = state(value)
    assert task["status"] == "verified"
    assert task["source_contract_version"] == task["accepted_contract_version"] == 1
    with value.repository.transaction() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.agent_runs_started == usage.agent_runs_completed == 2
        assert usage.active_agent_runs == usage.replan_count == 0
        assert usage.total_tokens == 20
        assert value.repository.process(connection, "process-a").status.value != "COMPLETED"
