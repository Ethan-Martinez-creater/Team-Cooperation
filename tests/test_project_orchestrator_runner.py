from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.project_process import (
    ProjectProcessCommandService,
    ProjectProcessScheduler,
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
    SQLAlchemyProjectProcessWakeupRepository,
)
from coifesp_harness.project_process.commands import ProjectOrchestrationDecisionStatus
from coifesp_harness.project_process.models import (
    ProjectProcessPhase,
    ProjectProcessStatus,
)
from coifesp_harness.project_process.orchestrator import DeterministicAction
from coifesp_harness.project_process.readiness import (
    ProcessReadinessSnapshot,
    ProjectReadinessEvaluator,
    ProjectReadinessSnapshot,
    WorkItemSnapshot,
)
from coifesp_harness.project_process.runner import (
    ProjectOrchestrationSnapshot,
    ProjectOrchestratorRunner,
    ProjectOrchestratorWorkerStatus,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _stack(*, after_effect=None, after_finished=None, with_effect=True):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    accounts.register_team(team_id="team-a", team_handle="team-a", team_name="Team A")
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="Runner project",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    repository = SQLAlchemyProjectProcessRepository(engine)
    repository.create_schema()
    service = ProjectProcessService(repository, clock=lambda: NOW)
    service.create_policy(
        policy_id="policy-a",
        project_id="project-a",
        max_agent_runs=10,
        max_total_tokens=10000,
        max_model_cost_microusd=100000,
        max_replans=3,
        max_generated_tasks=20,
        max_active_agent_runs=4,
        max_active_runs_per_team=2,
        max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=NOW + timedelta(days=30),
        version=1,
    )
    service.start_process(
        process_id="process-a",
        project_id="project-a",
        execution_policy_id="policy-a",
        started_by="lead-a",
    )
    for event_id, event_type, selector, version in (
        ("event-goal", "project.goal.confirmed", "goal.confirmed", 1),
        ("event-analysis-start", "project.analysis.started", "analysis.started", 2),
        ("event-analysis-done", "project.analysis.completed", "analysis.completed", 3),
        ("event-plan", "project.plan.approved", "plan.approved", 4),
    ):
        service.apply_transition(
            process_id="process-a",
            event_id=event_id,
            event_type=event_type,
            transition_key=selector,
            expected_version=version,
            subject_type="project",
            subject_id="project-a",
            initiated_by="lead-a",
            executed_as="service:project-orchestrator",
            correlation_id="corr-setup",
            payload={"selector": selector},
        )

    wakeups = SQLAlchemyProjectProcessWakeupRepository(engine)
    wakeups.create_schema()
    scheduler = ProjectProcessScheduler(
        wakeups,
        clock=lambda: NOW,
        retry_delay=lambda **_: 0,
    )
    repository.set_event_listener(
        lambda connection, event: scheduler.enqueue_in_transaction(
            connection,
            process_id=event.process_id,
            project_id=event.project_id,
            source_event_id=event.event_id,
            source_event_type=event.event_type,
            payload={"sequence": event.sequence},
            available_at=event.occurred_at,
        )
    )
    scheduler.enqueue(
        process_id="process-a",
        project_id="project-a",
        source_event_id="event-plan",
        source_event_type="project.plan.approved",
        payload={"sequence": 5},
        retry_budget=3,
    )
    state = {"task_status": "accepted", "effects": []}

    def snapshot_loader(process):
        evaluation = ProjectReadinessEvaluator().evaluate(
            ProjectReadinessSnapshot(
                tasks=(
                    WorkItemSnapshot(
                        work_id="task-a",
                        team_id="team-a",
                        status=state["task_status"],
                        contract_required=False,
                    ),
                ),
                process=ProcessReadinessSnapshot(
                    phase=process.phase.value,
                    status=process.status.value,
                    wait_reason=process.wait_reason.value,
                    dispatch_allowed=True,
                ),
            )
        )
        return ProjectOrchestrationSnapshot(
            graph_digest="sha256:" + "a" * 64,
            readiness=evaluation,
        )

    def effect(**kwargs):
        state["effects"].append((kwargs["decision_id"], kwargs["decision"].work_id))
        state["task_status"] = "submitted"

    runner = ProjectOrchestratorRunner(
        repository=repository,
        process_service=service,
        command_service=ProjectProcessCommandService(repository, clock=lambda: NOW),
        scheduler=scheduler,
        snapshot_loader=snapshot_loader,
        effect=effect if with_effect else None,
        after_effect=after_effect,
        after_decision_finished=after_finished,
    )
    return repository, service, scheduler, runner, state


def _process(repository):
    with repository.transaction() as connection:
        return repository.process(connection, "process-a")


def _events(repository):
    with repository.transaction() as connection:
        return repository.events(connection, "process-a")


def test_fake_synchronous_worker_advances_manual_work_to_verification():
    repository, _, _, runner, state = _stack()

    dispatched = runner.process_once(worker_id="orchestrator-worker-a")
    assert dispatched.status is ProjectOrchestratorWorkerStatus.APPLIED
    assert dispatched.action is DeterministicAction.DISPATCH_WORK
    assert state["effects"] == [(dispatched.decision_id, "task-a")]
    assert _process(repository).status is ProjectProcessStatus.RUNNING

    verified = runner.process_once(worker_id="orchestrator-worker-a")
    assert verified.status is ProjectOrchestratorWorkerStatus.APPLIED
    assert verified.action is DeterministicAction.ENTER_VERIFICATION
    process = _process(repository)
    assert process.phase is ProjectProcessPhase.VERIFICATION
    assert process.status is ProjectProcessStatus.READY


def test_crash_after_effect_replays_same_decision_without_duplicate_effect_or_event():
    failures = {"remaining": 1}

    def crash_once():
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise RuntimeError("crash after effect")

    repository, _, _, runner, state = _stack(after_effect=crash_once)
    first = runner.process_once(worker_id="orchestrator-worker-a")
    assert first.status is ProjectOrchestratorWorkerStatus.RETRY
    assert _process(repository).status is ProjectProcessStatus.RUNNING

    recovered = runner.process_once(worker_id="orchestrator-worker-a")
    assert recovered.status is ProjectOrchestratorWorkerStatus.APPLIED
    assert recovered.decision_id == first.decision_id
    assert len(state["effects"]) == 1
    assert len([event for event in _events(repository) if event.event_id == f"event:{first.decision_id}"]) == 1


def test_crash_after_decision_finish_only_retries_wakeup_completion():
    failures = {"remaining": 1}

    def crash_once():
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise RuntimeError("crash after decision finish")

    repository, _, _, runner, state = _stack(after_finished=crash_once)
    first = runner.process_once(worker_id="orchestrator-worker-a")
    assert first.status is ProjectOrchestratorWorkerStatus.RETRY
    with repository.transaction() as connection:
        decision = repository.decision(connection, first.decision_id)
    assert decision.status is ProjectOrchestrationDecisionStatus.APPLIED

    recovered = runner.process_once(worker_id="orchestrator-worker-a")
    assert recovered.status is ProjectOrchestratorWorkerStatus.APPLIED
    assert recovered.decision_id == first.decision_id
    assert len(state["effects"]) == 1


def test_missing_effect_adapter_retries_then_resumes_pending_decision():
    repository, service, scheduler, blocked, state = _stack(with_effect=False)
    first = blocked.process_once(worker_id="orchestrator-worker-a")
    assert first.status is ProjectOrchestratorWorkerStatus.RETRY
    assert _process(repository).status is ProjectProcessStatus.READY

    def effect(**kwargs):
        state["effects"].append(kwargs["decision_id"])
        state["task_status"] = "submitted"

    resumed = ProjectOrchestratorRunner(
        repository=repository,
        process_service=service,
        command_service=ProjectProcessCommandService(repository, clock=lambda: NOW),
        scheduler=scheduler,
        snapshot_loader=blocked.snapshot_loader,
        effect=effect,
    )
    second = resumed.process_once(worker_id="orchestrator-worker-a")
    assert second.status is ProjectOrchestratorWorkerStatus.APPLIED
    assert second.decision_id == first.decision_id
    assert state["effects"] == [first.decision_id]


def test_concurrent_event_during_snapshot_marks_decision_stale_and_emits_fact():
    repository, service, scheduler, runner, _ = _stack()
    original_loader = runner.snapshot_loader
    injected = {"done": False}

    def concurrent_loader(process):
        snapshot = original_loader(process)
        if not injected["done"]:
            injected["done"] = True
            service.append_fact(
                process_id=process.process_id,
                event_id="event-concurrent-risk",
                event_type="risk.created",
                expected_version=process.version,
                expected_event_sequence=process.last_event_sequence,
                subject_type="risk",
                subject_id="risk-a",
                initiated_by="lead-a",
                executed_as="lead-a",
                correlation_id="corr-risk",
                payload={"severity": "high"},
            )
        return snapshot

    runner.snapshot_loader = concurrent_loader
    outcome = runner.process_once(worker_id="orchestrator-worker-a")
    assert outcome.status is ProjectOrchestratorWorkerStatus.STALE
    with repository.transaction() as connection:
        decision = repository.decision(connection, outcome.decision_id)
        commands = repository.commands_for_decision(connection, outcome.decision_id)
    assert decision.status is ProjectOrchestrationDecisionStatus.STALE
    assert commands == ()
    assert any(
        event.event_type == "project.orchestrator.decision_stale"
        and event.subject_id == outcome.decision_id
        for event in _events(repository)
    )
    assert scheduler.claim(owner="another-worker") is not None
