"""Contract tests for Planner Run admission and project-budget accounting."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select, update
from test_project_planner_intents import NOW, _create, _run_service, _stack

from coifesp_harness.agent_runs import (
    AGENT_RUNS,
    AgentRunService,
    DurableRunStatus,
)
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.project_process import (
    ORCHESTRATOR_PRINCIPAL_ID,
    HumanGateService,
    ProjectExecutionBudgetService,
    ProjectPlannerIntentService,
    ProjectPlannerIntentStatus,
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.project_process.repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_RESERVATIONS,
    PROJECT_GATES,
    PROJECT_PROCESSES,
)
from coifesp_harness.security import Principal
from coifesp_harness.work_graph import ProjectGraphSnapshot

try:
    from coifesp_harness.project_process import (
        ProjectPlannerRunAccounting,
        ProjectPlannerRunLauncher,
    )
except ImportError:  # The production seam is absent on the test-first branch.
    ProjectPlannerRunAccounting = None
    ProjectPlannerRunLauncher = None


WORKER = Principal(
    "planner-worker",
    "team-a",
    roles=frozenset({"agent_worker"}),
    is_service=True,
)


def _require_planner_services() -> None:
    if ProjectPlannerRunLauncher is None or ProjectPlannerRunAccounting is None:
        pytest.xfail(
            "missing ProjectPlannerRunLauncher/ProjectPlannerRunAccounting "
            "in coifesp_harness.project_process"
        )


def _planner_stack(*, file_path=None):
    if file_path is None:
        repository, process, graph = _stack()
    else:
        engine = create_engine(
            f"sqlite+pysqlite:///{file_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
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
            description="Planner Run budget test",
            actor_id="lead-a",
            owner_assignment_name="Owner",
            owner_kind=ProjectTeamKind.PRODUCT,
        )
        repository = SQLAlchemyProjectProcessRepository(engine)
        repository.create_schema()
        process_service = ProjectProcessService(repository, clock=lambda: NOW)
        process_service.create_policy(
            policy_id="policy-a",
            project_id="project-a",
            max_agent_runs=10,
            max_total_tokens=200_000,
            max_model_cost_microusd=20_000_000,
            max_replans=3,
            max_generated_tasks=20,
            max_active_agent_runs=4,
            max_active_runs_per_team=2,
            max_specialist_depth=2,
            max_specialist_runs_per_task=2,
            deadline_at=NOW + timedelta(days=30),
            version=1,
        )
        process = process_service.start_process(
            process_id="process-a",
            project_id="project-a",
            execution_policy_id="policy-a",
            started_by="lead-a",
        )
        graph = ProjectGraphSnapshot("project-a", (), (), (), "sha256:" + "a" * 64)

    # The regular planner fixture uses intentionally small process limits.  A
    # Planner Run's bounded RunBudget is larger than those values, so raise only
    # the token/cost ceilings for this integration contract test.
    with repository.transaction() as connection:
        connection.execute(
            update(PROJECT_EXECUTION_POLICIES)
            .where(PROJECT_EXECUTION_POLICIES.c.policy_id == "policy-a")
            .values(max_total_tokens=200_000, max_model_cost_microusd=20_000_000)
        )
    process_service = ProjectProcessService(repository, clock=lambda: NOW)
    for event_id, event_type, transition_key, expected_version in (
        ("event-goal", "project.goal.confirmed", "goal.confirmed", 1),
        ("event-analysis-start", "project.analysis.started", "analysis.started", 2),
    ):
        process, _ = process_service.apply_transition(
            process_id=process.process_id,
            event_id=event_id,
            event_type=event_type,
            transition_key=transition_key,
            expected_version=expected_version,
            subject_type="project",
            subject_id="project-a",
            initiated_by="lead-a",
            executed_as=ORCHESTRATOR_PRINCIPAL_ID,
            correlation_id="planner-budget-setup",
            payload={"step": transition_key},
        )
    intents = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    runs = _run_service(repository.engine)
    human = HumanGateService(repository, clock=lambda: NOW)
    return repository, process, graph, intents, runs, human


def _launcher(repository, intents, runs, human, graph):
    _require_planner_services()
    return ProjectPlannerRunLauncher(
        intent_service=intents,
        work_graph_repository=SimpleNamespace(
            engine=repository.engine,
            snapshot=lambda _connection, project_id: graph,
        ),
        run_service=runs,
        budget_service=ProjectExecutionBudgetService(repository, clock=lambda: NOW),
        human_gate_service=human,
    )


def _accounting(repository, runs):
    _require_planner_services()
    return ProjectPlannerRunAccounting(repository=repository, run_repository=runs.repository)


def _launch_one(launcher, intents, runs, intent):
    outcome = launcher.process_once(worker_id="planner-test")
    assert outcome is not None
    bound = intents.get(intent.planner_intent_id)
    assert bound.run_id is not None
    run = runs.repository.get(tenant_id=bound.owner_team_id, run_id=bound.run_id)
    return outcome, bound, run


def _usage(repository):
    with repository.transaction() as connection:
        return repository.usage(connection, "process-a")


def _reservation(repository, run_id):
    with repository.transaction() as connection:
        row = connection.execute(
            select(PROJECT_EXECUTION_RESERVATIONS).where(
                PROJECT_EXECUTION_RESERVATIONS.c.agent_run_id == run_id
            )
        ).mappings().one_or_none()
    assert row is not None, f"no Planner reservation bound to {run_id}"
    return dict(row)


def _rows(repository, table):
    with repository.transaction() as connection:
        return [dict(row) for row in connection.execute(select(table)).mappings().all()]


def _count(repository, table):
    with repository.transaction() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _finish_actual_run(runs, run, *, target=DurableRunStatus.COMPLETED, tokens=333, cost=777):
    lease = runs.claim(worker=WORKER, lease_seconds=60)
    assert lease is not None
    assert lease.run.run_id == run.run_id
    runs.start(worker=WORKER, run_id=run.run_id, lease_token=lease.lease_token)
    checkpoint = runs.repository.load_checkpoint(tenant_id="team-a", run_id=run.run_id)
    checkpoint["usage"] = {
        "turns": 1,
        "tool_calls": 0,
        "total_tokens": tokens,
        "model_cost_microusd": cost,
    }
    return runs.checkpoint(
        worker=WORKER,
        run_id=run.run_id,
        lease_token=lease.lease_token,
        target=target,
        checkpoint=checkpoint,
        turns=1,
        tool_calls=0,
        total_tokens=tokens,
        model_cost_microusd=cost,
        failure_code="planner_run_failed" if target is DurableRunStatus.FAILED else None,
    )


def test_planner_launch_reserves_and_binds_run_in_one_transaction():
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)

    outcome, bound, run = _launch_one(launcher, intents, runs, intent)

    reservation = _reservation(repository, run.run_id)
    usage = _usage(repository)
    assert outcome.status.value == "APPLIED"
    assert bound.status is ProjectPlannerIntentStatus.RUNNING
    assert bound.run_id == run.run_id
    assert reservation["process_id"] == process.process_id
    assert reservation["team_id"] == intent.owner_team_id
    assert reservation["agent_run_id"] == run.run_id
    assert reservation["status"] == "RESERVED"
    assert (usage.agent_runs_started, usage.active_agent_runs, usage.version) == (1, 1, 2)
    assert _count(repository, AGENT_RUNS) == 1
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 1


def test_active_planner_run_is_not_launched_again():
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)

    _, bound, run = _launch_one(launcher, intents, runs, intent)
    assert launcher.process_once(worker_id="planner-test") is None
    assert intents.get(intent.planner_intent_id) == bound
    assert runs.repository.get(tenant_id="team-a", run_id=run.run_id).status.value == "queued"
    assert _count(repository, AGENT_RUNS) == 1
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 1


def test_planner_run_creation_failure_rolls_back_intent_reservation_and_run(monkeypatch):
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)

    def fail_create(_service, **_kwargs):
        raise RuntimeError("planner Run creation failed")

    monkeypatch.setattr(AgentRunService, "create", fail_create)
    with pytest.raises(RuntimeError, match="planner Run creation failed"):
        launcher.process_once(worker_id="planner-test")

    persisted = intents.get(intent.planner_intent_id)
    usage = _usage(repository)
    assert persisted.status is ProjectPlannerIntentStatus.PENDING
    assert persisted.run_id is None
    assert _count(repository, AGENT_RUNS) == 0
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 0
    assert (usage.agent_runs_started, usage.agent_runs_completed, usage.active_agent_runs) == (
        0,
        0,
        0,
    )
    assert usage.version == 1


def test_planner_completed_run_settles_actual_usage_exactly_once():
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)
    _, _, run = _launch_one(launcher, intents, runs, intent)
    accounting = _accounting(repository, runs)
    terminal = _finish_actual_run(runs, run, tokens=333, cost=777)

    assert accounting.on_run_terminal(terminal) is True
    settled = _reservation(repository, run.run_id)
    usage = _usage(repository)
    assert settled["status"] == "SETTLED"
    assert settled["terminal_total_tokens"] == 333
    assert settled["terminal_model_cost_microusd"] == 777
    assert (
        usage.agent_runs_started,
        usage.agent_runs_completed,
        usage.active_agent_runs,
        usage.total_tokens,
        usage.model_cost_microusd,
    ) == (1, 1, 0, 333, 777)
    version_after_settlement = usage.version

    assert accounting.on_run_terminal(terminal) is False
    replayed = _usage(repository)
    assert replayed == usage
    assert replayed.version == version_after_settlement


@pytest.mark.parametrize("terminal_status", [DurableRunStatus.FAILED, DurableRunStatus.CANCELLED])
def test_planner_failed_and_cancelled_runs_use_terminal_settlement_semantics(terminal_status):
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)
    _, _, run = _launch_one(launcher, intents, runs, intent)
    accounting = _accounting(repository, runs)
    if terminal_status is DurableRunStatus.CANCELLED:
        terminal = runs.repository.cancel_queued(
            tenant_id="team-a",
            run_id=run.run_id,
            owner_principal_id=ORCHESTRATOR_PRINCIPAL_ID,
        )
        expected_tokens = 0
        expected_cost = 0
    else:
        terminal = _finish_actual_run(runs, run, target=terminal_status, tokens=19, cost=23)
        expected_tokens = 19
        expected_cost = 23

    assert accounting.on_run_terminal(terminal) is True
    settled = _reservation(repository, run.run_id)
    usage = _usage(repository)
    assert settled["status"] == "SETTLED"
    assert settled["terminal_total_tokens"] == expected_tokens
    assert settled["terminal_model_cost_microusd"] == expected_cost
    assert (usage.agent_runs_completed, usage.active_agent_runs) == (1, 0)
    assert (usage.total_tokens, usage.model_cost_microusd) == (expected_tokens, expected_cost)
    assert accounting.on_run_terminal(terminal) is False
    assert _usage(repository) == usage


def test_terminal_callback_crash_is_recovered_by_startup_replay(monkeypatch):
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)
    _, _, run = _launch_one(launcher, intents, runs, intent)
    accounting = _accounting(repository, runs)
    terminal = _finish_actual_run(runs, run, tokens=41, cost=59)

    def callback_crash(_run):
        raise RuntimeError("terminal accounting callback crashed")

    monkeypatch.setattr(accounting, "on_run_terminal", callback_crash)
    with pytest.raises(RuntimeError, match="terminal accounting callback crashed"):
        accounting.on_run_terminal(terminal)
    assert _reservation(repository, run.run_id)["status"] == "RESERVED"
    assert _usage(repository).active_agent_runs == 1

    restarted = _accounting(repository, runs)
    assert restarted.replay_pending() == 1
    assert _reservation(repository, run.run_id)["status"] == "SETTLED"
    usage = _usage(repository)
    assert (usage.agent_runs_completed, usage.active_agent_runs, usage.total_tokens) == (1, 0, 41)
    assert restarted.replay_pending() == 0
    assert _usage(repository) == usage


def test_pending_intent_is_auto_started_idempotently_after_restart():
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)

    launcher.process_once(worker_id="planner-test")
    restarted_intents = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    restarted_runs = _run_service(repository.engine)
    restarted = _launcher(repository, restarted_intents, restarted_runs, human, graph)
    restarted.process_once(worker_id="planner-test-restarted")

    persisted = restarted_intents.get(intent.planner_intent_id)
    usage = _usage(repository)
    assert persisted.status is ProjectPlannerIntentStatus.RUNNING
    assert persisted.run_id is not None
    assert _count(repository, AGENT_RUNS) == 1
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 1
    assert (usage.agent_runs_started, usage.active_agent_runs) == (1, 1)


def test_two_concurrent_planner_launchers_converge_on_one_run_and_reservation(tmp_path):
    repository, process, graph, intents, runs, _human = _planner_stack(
        file_path=tmp_path / "planner-budget-concurrency.sqlite3"
    )
    intent = _create(intents, process, graph)
    barrier = Barrier(2)

    def launch_from_replica():
        replica_repository = SQLAlchemyProjectProcessRepository(repository.engine)
        replica_intents = ProjectPlannerIntentService(replica_repository, clock=lambda: NOW)
        replica_human = HumanGateService(replica_repository, clock=lambda: NOW)
        replica_runs = AgentRunService(runs.repository)
        replica_launcher = _launcher(
            replica_repository, replica_intents, replica_runs, replica_human, graph
        )
        barrier.wait()
        replica_launcher.process_once(worker_id="planner-replica")
        bound = replica_intents.get(intent.planner_intent_id)
        assert bound.run_id is not None
        return replica_runs.repository.get(tenant_id="team-a", run_id=bound.run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _item: launch_from_replica(), (1, 2))

    assert first.run_id == second.run_id
    assert _count(repository, AGENT_RUNS) == 1
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 1
    assert _usage(repository).agent_runs_started == 1
    assert _usage(repository).active_agent_runs == 1


def test_planner_budget_limit_opens_gate_without_creating_run_or_reservation():
    repository, process, graph, intents, runs, human = _planner_stack()
    with repository.transaction() as connection:
        connection.execute(
            update(PROJECT_EXECUTION_POLICIES)
            .where(PROJECT_EXECUTION_POLICIES.c.policy_id == "policy-a")
            .values(max_agent_runs=0)
        )
    _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)

    outcome = launcher.process_once(worker_id="planner-test")
    assert outcome is not None
    assert outcome.status.value == "RETRY"

    assert _count(repository, AGENT_RUNS) == 0
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 0
    usage = _usage(repository)
    assert (usage.agent_runs_started, usage.active_agent_runs, usage.version) == (0, 0, 1)
    gates = _rows(repository, PROJECT_GATES)
    assert len(gates) == 1
    assert gates[0]["gate_type"] == "BUDGET"
    assert gates[0]["status"] == "OPEN"


@pytest.mark.parametrize(
    ("status", "wait_reason"),
    [("WAITING", "HUMAN_APPROVAL"), ("BLOCKED", "DEPENDENCY")],
)
def test_waiting_or_blocked_process_does_not_admit_planner_run(status, wait_reason):
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_PROCESSES.update()
            .where(PROJECT_PROCESSES.c.process_id == process.process_id)
            .values(status=status, wait_reason=wait_reason, version=process.version + 1)
        )
    launcher = _launcher(repository, intents, runs, human, graph)

    assert launcher.process_once(worker_id="planner-test") is None
    persisted = intents.get(intent.planner_intent_id)
    assert persisted.status is ProjectPlannerIntentStatus.PENDING
    assert _count(repository, AGENT_RUNS) == 0
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 0


def test_direct_launch_rejects_changed_process_cursor_inside_transaction():
    repository, process, graph, intents, runs, _human = _planner_stack()
    intent = _create(intents, process, graph)
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_PROCESSES.update()
            .where(PROJECT_PROCESSES.c.process_id == process.process_id)
            .values(version=process.version + 1)
        )

    with pytest.raises(GovernanceConflictError, match="process snapshot changed"):
        intents.launch(
            intent=intent,
            graph=graph,
            run_service=runs,
            budget_service=ProjectExecutionBudgetService(repository, clock=lambda: NOW),
        )
    assert _count(repository, AGENT_RUNS) == 0
    assert _count(repository, PROJECT_EXECUTION_RESERVATIONS) == 0


def test_accounting_rejects_reservation_not_derived_from_intent():
    repository, process, graph, intents, runs, human = _planner_stack()
    intent = _create(intents, process, graph)
    launcher = _launcher(repository, intents, runs, human, graph)
    _, _, run = _launch_one(launcher, intents, runs, intent)
    terminal = runs.repository.cancel_queued(
        tenant_id="team-a",
        run_id=run.run_id,
        owner_principal_id=ORCHESTRATOR_PRINCIPAL_ID,
    )
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_EXECUTION_RESERVATIONS.update()
            .where(PROJECT_EXECUTION_RESERVATIONS.c.agent_run_id == run.run_id)
            .values(reservation_id="planner-budget:unexpected")
        )

    with pytest.raises(GovernanceConflictError, match="unexpected budget reservation"):
        _accounting(repository, runs).on_run_terminal(terminal)
    assert _usage(repository).active_agent_runs == 1
