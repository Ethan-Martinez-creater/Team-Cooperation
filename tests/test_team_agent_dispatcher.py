import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_project_orchestrator_capability_adapter import (
    _declare,
    _publish,
    _publisher,
)
from test_project_orchestrator_capability_adapter import (
    _stack as capability_stack,
)

from coifesp_harness.agent_runs import (
    AgentCheckpointKeyring,
    AgentRunCheckpointCodec,
    AgentRunService,
    DurableAgentWorker,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.capabilities.repository import CAPACITY_RESERVATIONS
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import ProjectWorkspaceService, TeamCollaborationService
from coifesp_harness.product.repository import PROJECT_AGENT_RUNS, TEAM_TASKS
from coifesp_harness.project_process import (
    ProjectCapabilityRequirement,
    ProjectProcessCommandService,
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
    TeamTaskExecutionContract,
)
from coifesp_harness.project_process.budget import ProjectBudgetExhausted
from coifesp_harness.project_process.repository import (
    PROJECT_EXECUTION_RESERVATIONS,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESS_EVENTS,
    PROJECT_PROCESSES,
)
from coifesp_harness.security import Classification, Principal
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.dispatcher import (
    TaskDispatchFacts,
    TeamAgentDispatcher,
)
from coifesp_harness.team_agents.identity import TeamAgentPrincipalResolver
from coifesp_harness.team_agents.profiles import TeamAgentCapabilityResolver
from coifesp_harness.work_graph import (
    ProjectWorkGraphService,
    SQLAlchemyWorkGraphRepository,
)


def stack(*, accepted=True, slots=2, token_budget=200000):
    engine, _, directory, adapter, providers = capability_stack()
    publisher = _publisher(providers["team-b"])
    capability = _publish(
        directory, publisher, required_compartments=(),
        max_input_classification=Classification.INTERNAL,
    ).capability
    _declare(directory, publisher, capability, slots=slots)
    collaboration = TeamCollaborationService(engine)
    collaboration.create_task(
        task_id="task-a", project_id="project-a", actor_id="lead-a",
        target_team_id="team-b", title="Review", description="Review input document",
        acceptance_criteria="Produce the contracted review output",
    )
    if accepted:
        collaboration.respond_task(
            task_id="task-a", project_id="project-a", actor_id="lead-b", accept=True,
        )
    agent = ProjectWorkspaceService(engine).ensure_team_project_agent(
        project_id="project-a", team_id="team-b",
    )
    graph_repository = SQLAlchemyWorkGraphRepository(engine)
    graph_repository.create_schema()
    graph_service = ProjectWorkGraphService(graph_repository)
    graph_service.register_existing_subject(
        node_id="node:task:task-a", project_id="project-a", node_type="task", subject_id="task-a",
    )
    repository = SQLAlchemyProjectProcessRepository(engine)
    repository.create_schema()
    processes = ProjectProcessService(repository)
    processes.create_policy(
        policy_id="policy-a", project_id="project-a", max_agent_runs=10,
        max_total_tokens=token_budget, max_model_cost_microusd=20000000,
        max_replans=3, max_generated_tasks=20, max_active_agent_runs=4,
        max_active_runs_per_team=2, max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=datetime.now(UTC) + timedelta(days=1), version=1,
    )
    processes.start_process(
        process_id="process-a", project_id="project-a",
        execution_policy_id="policy-a", started_by="lead-a",
    )
    # This fixture starts at the already-approved execution boundary. Earlier
    # phases and their Human Gate transitions have independent coverage.
    with engine.begin() as connection:
        connection.execute(PROJECT_INPUT_REQUESTS.update().values(
            status="CANCELLED", answered_at=datetime.now(UTC), answered_by="lead-a",
            resolution_idempotency_key="fixture:initial-input",
            resolution_event_id="fixture:initial-input-closed", resolution_sha256="a" * 64,
        ))
        connection.execute(PROJECT_PROCESSES.update().values(
            phase="EXECUTION", status="READY", wait_reason="NONE",
        ))
    runs = SQLAlchemyAgentRunRepository(
        engine=engine, keyring=AgentCheckpointKeyring(master_key=b"k" * 32, key_id="dispatch-test"),
    )
    runs.create_schema()
    facts = TaskDispatchFacts(
        TeamTaskExecutionContract(
            "contract-a", "task-a", "project-a", "team-b", ("review",),
            capability.input_contract, capability.output_contract, "verify:review:v1",
        ),
        ProjectCapabilityRequirement(
            "project-a", "team-a", "team-b", ("review",), "a2a-1.0",
            Classification.INTERNAL, (), (), 1,
        ),
        True,
    )
    dispatcher = TeamAgentDispatcher(
        repository=repository, work_graph_repository=graph_repository,
        capability_adapter=adapter, runtime_resolver=TeamAgentCapabilityResolver(engine=engine),
        run_service=AgentRunService(runs), fact_loader=lambda **_: facts,
    )
    value = SimpleNamespace(
        engine=engine, repository=repository, graph=graph_service, dispatcher=dispatcher,
        runs=runs, facts=facts, agent=agent, capabilities=adapter.repository,
    )
    record(value)
    return value


def record(value, decision_id="decision-a"):
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
    graph = value.graph.snapshot(project_id="project-a")
    return ProjectProcessCommandService(value.repository).record_decision(
        decision_id=decision_id, process_id=process.process_id, reason="READY_WORK",
        based_on_process_version=process.version,
        based_on_event_sequence=process.last_event_sequence,
        graph_snapshot_digest=graph.digest, current_graph_snapshot_digest=graph.digest,
        decision_json={"action": "dispatch_work", "reason": "READY_WORK", "work_id": "task-a"},
    )


def dispatch(value, *, fence=lambda _: None, decision_id="decision-a"):
    return value.dispatcher.dispatch(
        process_id="process-a", decision_id=decision_id, task_id="task-a", mutation_fence=fence,
    )


def assert_no_dispatch(value):
    with value.engine.connect() as connection:
        for table in (AGENT_RUNS, PROJECT_AGENT_RUNS, CAPACITY_RESERVATIONS, PROJECT_EXECUTION_RESERVATIONS):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0
        assert value.repository.usage(connection, "process-a").agent_runs_started == 0


def test_dispatch_commits_one_machine_run_and_retry_reuses_original_binding():
    value = stack()
    first = dispatch(value)
    repeated = dispatch(value)
    assert repeated.run_id == first.run_id
    assert first.duplicate is False and repeated.duplicate is True
    with value.engine.connect() as connection:
        binding = connection.execute(select(PROJECT_AGENT_RUNS)).mappings().one()
        assert binding["created_by"] is None and binding["mode"] is None
        assert binding["initiated_by_principal_id"] == "service:project-orchestrator"
        assert binding["executed_as_principal_id"] == "team-agent:team-b"
        assert binding["team_agent_id"] == value.agent.agent_id
        assert binding["execution_attempt"] == 1
        assert connection.execute(select(TEAM_TASKS.c.status)).scalar_one() == "in_progress"
        assert connection.execute(select(func.count()).select_from(AGENT_RUNS)).scalar_one() == 1
        assert value.repository.usage(connection, "process-a").agent_runs_started == 1
    checkpoint = AgentRunCheckpointCodec().decode(value.runs.load_checkpoint(
        tenant_id="team-b", run_id=first.run_id,
    ))
    assert checkpoint["tool_authorization"] is not None
    assert checkpoint["tool_authorization"].tools == ()
    assert len(checkpoint["context_items"]) == 2


def test_proposed_tasks_never_dispatch():
    value = stack(accepted=False)
    with pytest.raises(GovernanceConflictError, match="only an accepted"):
        dispatch(value)
    assert_no_dispatch(value)


def test_dependency_is_recomputed_from_real_work_graph_before_dispatch():
    value = stack()
    TeamCollaborationService(value.engine).create_task(
        task_id="task-dependency", project_id="project-a", actor_id="lead-a",
        target_team_id="team-b", title="Prerequisite", description="Prepare input",
        acceptance_criteria="Input verified",
    )
    value.graph.register_existing_subject(
        node_id="node:task:dependency", project_id="project-a", node_type="task",
        subject_id="task-dependency",
    )
    value.graph.add_relation(
        relation_id="relation:depends", project_id="project-a",
        source_node_id="node:task:task-a", target_node_id="node:task:dependency",
        relation_type="depends_on", created_by_type="human", created_by_id="lead-a",
    )
    record(value, "decision-dependent")
    with pytest.raises(GovernanceConflictError, match="readiness conditions"):
        dispatch(value, decision_id="decision-dependent")
    assert_no_dispatch(value)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().where(
            TEAM_TASKS.c.task_id == "task-dependency"
        ).values(status="verified"))
    record(value, "decision-ready")
    assert dispatch(value, decision_id="decision-ready").execution_attempt == 1


def test_reaccepted_task_cannot_hide_a_still_active_durable_run():
    value = stack()
    dispatch(value)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(status="accepted"))
    record(value, "decision-again")
    with pytest.raises(GovernanceConflictError, match="active execution"):
        dispatch(value, decision_id="decision-again")


def test_open_human_input_blocks_dispatch_even_if_process_state_is_stale():
    value = stack()
    with value.engine.begin() as connection:
        connection.execute(PROJECT_INPUT_REQUESTS.update().values(
            status="OPEN", answered_at=None, answered_by=None,
            resolution_idempotency_key=None, resolution_event_id=None, resolution_sha256=None,
        ))
    with pytest.raises(GovernanceConflictError, match="open human gate or input"):
        dispatch(value)
    assert_no_dispatch(value)


def test_missing_contract_acceptance_and_wrong_requirement_fail_closed():
    value = stack()
    value.dispatcher.fact_loader = lambda **_: replace(value.facts, contract_accepted=False)
    with pytest.raises(GovernanceConflictError, match="contract is not accepted"):
        dispatch(value)
    value.dispatcher.fact_loader = lambda **_: replace(
        value.facts, requirement=replace(value.facts.requirement, target_team_id="team-c"),
    )
    with pytest.raises(GovernanceConflictError, match="requirement does not match"):
        dispatch(value)
    assert_no_dispatch(value)


def test_capacity_and_project_budget_are_real_admission_limits():
    value = stack(slots=0)
    with pytest.raises(GovernanceConflictError, match="no matching"):
        dispatch(value)
    assert_no_dispatch(value)
    value = stack(token_budget=10)
    with pytest.raises(ProjectBudgetExhausted):
        dispatch(value)
    assert_no_dispatch(value)


def test_stale_work_graph_does_not_create_any_run_or_reservation():
    value = stack()
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(title="Changed task"))
    with pytest.raises(GovernanceConflictError, match="graph snapshot is stale"):
        dispatch(value)
    assert_no_dispatch(value)


def test_run_create_failure_rolls_back_capacity_budget_binding_and_task(monkeypatch):
    value = stack()

    def unavailable(*args, **kwargs):
        raise RuntimeError("run store unavailable")

    monkeypatch.setattr(AgentRunService, "create", unavailable)
    with pytest.raises(RuntimeError, match="run store unavailable"):
        dispatch(value)
    assert_no_dispatch(value)
    with value.engine.connect() as connection:
        assert connection.execute(select(TEAM_TASKS.c.status)).scalar_one() == "accepted"


@pytest.mark.parametrize("fail_at", [1, 2])
def test_expired_worker_fence_rolls_back_every_effect(fail_at):
    value = stack()
    calls = 0

    def fence(_):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise GovernanceConflictError("lease lost")

    with pytest.raises(GovernanceConflictError, match="lease lost"):
        dispatch(value, fence=fence)
    assert_no_dispatch(value)


def test_different_decision_cannot_start_second_execution_while_first_is_active():
    value = stack()
    dispatch(value)
    record(value, "decision-b")
    with pytest.raises(GovernanceConflictError, match="only an accepted"):
        dispatch(value, decision_id="decision-b")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_accounting_replays_once_and_releases_capacity(status):
    value = stack()
    result = dispatch(value)
    accounting = TeamTaskRunAccounting(
        repository=value.repository, run_repository=value.runs,
        capability_repository=value.capabilities,
    )
    assert accounting.replay_pending() == 0  # queued is not terminal
    with value.engine.begin() as connection:
        connection.execute(AGENT_RUNS.update().values(
            status=status, completed_at=datetime.now(UTC), total_tokens=7,
            model_cost_microusd=3,
        ))
    assert accounting.replay_pending() == 1
    assert accounting.replay_pending() == 0
    assert accounting.settle(run_id=result.run_id) is True  # duplicate callback
    with value.engine.connect() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.active_agent_runs == 0
        assert usage.agent_runs_completed == 1 and usage.total_tokens == 7
        assert connection.execute(select(CAPACITY_RESERVATIONS.c.status)).scalar_one() == "released"
        events = connection.execute(select(PROJECT_PROCESS_EVENTS).where(
            PROJECT_PROCESS_EVENTS.c.subject_id == result.run_id
        )).mappings().all()
        assert len(events) == 1 and events[0]["event_type"] == f"agent_run.{status}"
        assert events[0]["executed_as"] == "team-agent:team-b"


def test_terminal_accounting_is_atomic_when_capacity_release_fails(monkeypatch):
    value = stack()
    result = dispatch(value)
    with value.engine.begin() as connection:
        connection.execute(AGENT_RUNS.update().values(status="failed", completed_at=datetime.now(UTC)))
    accounting = TeamTaskRunAccounting(
        repository=value.repository, run_repository=value.runs, capability_repository=value.capabilities,
    )
    original = type(value.capabilities).release_reservation

    def unavailable(*args, **kwargs):
        raise RuntimeError("release interrupted")

    monkeypatch.setattr(type(value.capabilities), "release_reservation", unavailable)
    with pytest.raises(RuntimeError, match="release interrupted"):
        accounting.settle(run_id=result.run_id)
    with value.engine.connect() as connection:
        assert value.repository.usage(connection, "process-a").active_agent_runs == 1
        assert connection.execute(select(PROJECT_EXECUTION_RESERVATIONS.c.status)).scalar_one() == "RESERVED"
    monkeypatch.setattr(type(value.capabilities), "release_reservation", original)
    assert accounting.replay_pending() == 1


def test_dispatched_task_executes_through_real_worker_and_accounts_automatically():
    from test_agent_run_worker import Provider
    from test_agent_run_worker import stack as worker_stack

    value = stack()
    result = dispatch(value)
    accounting = TeamTaskRunAccounting(
        repository=value.repository, run_repository=value.runs,
        capability_repository=value.capabilities,
    )
    value.dispatcher.run_service.terminal_callback = accounting.on_run_terminal
    _, _, loop = worker_stack(provider=Provider())

    class NoHumanResolver:
        async def resolve(self, **kwargs):
            raise AssertionError("a Team Agent must not resolve as a human")

    worker = DurableAgentWorker(
        service=value.dispatcher.run_service, loop=loop,
        principal_resolver=TeamAgentPrincipalResolver(
            engine=value.engine, human_resolver=NoHumanResolver(),
        ),
    )
    outcome = asyncio.run(worker.process_once(worker=Principal(
        "worker-test", "team-b", roles=frozenset({"agent_worker"}), is_service=True,
    )))
    assert outcome.status.value == "completed"
    assert value.runs.get(tenant_id="team-b", run_id=result.run_id).status.value == "completed"
    assert accounting.replay_pending() == 0
    with value.engine.connect() as connection:
        assert value.repository.usage(connection, "process-a").active_agent_runs == 0
        assert connection.execute(select(CAPACITY_RESERVATIONS.c.status)).scalar_one() == "released"


def _runner(value):
    from coifesp_harness.project_process import (
        ProjectProcessScheduler,
        SQLAlchemyProjectProcessWakeupRepository,
    )
    from coifesp_harness.project_process.readiness import (
        ProjectReadinessEvaluator,
        ProjectReadinessSnapshot,
        WorkItemSnapshot,
    )
    from coifesp_harness.project_process.runner import (
        ProjectOrchestrationSnapshot,
        ProjectOrchestratorRunner,
    )

    wakeups = SQLAlchemyProjectProcessWakeupRepository(value.engine)
    wakeups.create_schema()
    scheduler = ProjectProcessScheduler(wakeups, retry_delay=lambda **_: 0)
    scheduler.enqueue(
        process_id="process-a", project_id="project-a", source_event_id="fixture:approved",
        source_event_type="project.plan.approved", payload={}, retry_budget=3,
    )

    def snapshot(process):
        graph = value.graph.snapshot(project_id=process.project_id)
        readiness = ProjectReadinessEvaluator().evaluate(ProjectReadinessSnapshot(tasks=(
            WorkItemSnapshot("task-a", "accepted", "team-b", contract_required=False),
        )))
        return ProjectOrchestrationSnapshot(graph.digest, readiness)

    return ProjectOrchestratorRunner(
        repository=value.repository, process_service=ProjectProcessService(value.repository),
        command_service=ProjectProcessCommandService(value.repository), scheduler=scheduler,
        snapshot_loader=snapshot, effect=value.dispatcher,
    )


def test_runner_commits_dispatch_event_and_run_together_before_wakeup_ack():
    value = stack()
    runner = _runner(value)
    calls = 0

    def crash_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after dispatch commit")

    runner.after_effect = crash_once
    first = runner.process_once(worker_id="project-worker")
    assert first.status.value == "RETRY"
    with value.engine.connect() as connection:
        assert value.repository.process(connection, "process-a").status.value == "RUNNING"
        assert value.repository.event(connection, f"event:{first.decision_id}") is not None
        assert connection.execute(select(func.count()).select_from(AGENT_RUNS)).scalar_one() == 1
    second = runner.process_once(worker_id="project-worker")
    assert second.status.value == "APPLIED"
    with value.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(AGENT_RUNS)).scalar_one() == 1


def test_dispatch_event_publication_failure_rolls_back_run_and_reservations():
    value = stack()
    runner = _runner(value)

    def fail_publish(connection, event):
        raise RuntimeError("outbox unavailable")

    value.repository.set_event_listener(fail_publish)
    result = runner.process_once(worker_id="project-worker")
    assert result.status.value == "RETRY"
    assert result.error == "outbox unavailable"
    assert_no_dispatch(value)
    with value.engine.connect() as connection:
        assert value.repository.event(connection, f"event:{result.decision_id}") is None
        assert value.repository.process(connection, "process-a").status.value == "READY"
