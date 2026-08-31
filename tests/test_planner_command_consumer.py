"""Real-store Planner projection -> atomic command consumption regressions."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_project_planner_intents import NOW, _create, _run_service, _stack

from coifesp_harness.agent_runs import DurableRunStatus
from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.product.repository import TEAM_TASKS
from coifesp_harness.project_process import (
    ProjectPlannerIntentService,
    ProjectPlannerProjection,
    ProjectProcessCommandService,
    ProjectProcessService,
)
from coifesp_harness.project_process.human_service import HumanGateService
from coifesp_harness.project_process.planner_consumer import PlannerCommandConsumer
from coifesp_harness.project_process.repository import (
    PROJECT_EXECUTION_USAGE,
    PROJECT_GATES,
    PROJECT_INPUT_REQUESTS,
    PROJECT_ORCHESTRATION_DECISIONS,
    PROJECT_PLANNER_INTENTS,
)
from coifesp_harness.work_graph.repository import SQLAlchemyWorkGraphRepository


def setup_batch(commands, *, graph_effects=None, process_state=None):
    repository, process, _ = _stack()
    from coifesp_harness.product import ProductAccountService
    from coifesp_harness.product.repository import PROJECT_TEAMS

    ProductAccountService(repository.engine).register_team(
        team_id="team-b", team_handle="team-b", team_name="Implementers"
    )
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_TEAMS.insert().values(
                project_id="project-a",
                team_id="team-b",
                name="Implementers",
                kind="engineering",
                assigned_by="lead-a",
                created_at=NOW,
            )
        )
    HumanGateService(repository, clock=lambda: NOW).answer_input(
        request_id="process-a:goal-input",
        response={"goal": "Build a reviewed deliverable"},
        actor_id="lead-a",
        idempotency_key="answer-goal",
        event_id="answer-goal",
        expected_object_version=1,
        expected_process_version=process.version,
        correlation_id="answer-goal",
    )
    with repository.transaction() as connection:
        if process_state is not None:
            from coifesp_harness.project_process.repository import PROJECT_PROCESSES

            connection.execute(PROJECT_PROCESSES.update().values(**process_state))
        process = repository.process(connection, "process-a")
    graph_repo = SQLAlchemyWorkGraphRepository(repository.engine)
    graph_repo.create_schema()
    with repository.transaction() as connection:
        graph = graph_repo.snapshot(connection, project_id=process.project_id)
    intents = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    intent = _create(intents, process, graph)
    runs = _run_service(repository.engine)
    run = intents.launch(intent=intent, graph=graph, run_service=runs)
    with repository.transaction() as connection:
        connection.execute(
            AGENT_RUNS.update()
            .where(AGENT_RUNS.c.run_id == run.run_id)
            .values(status="completed", completed_at=NOW)
        )
    payload = intents.protocol(intent)
    payload["commands"] = commands
    projection = ProjectPlannerProjection(
        intent_service=intents,
        command_service=ProjectProcessCommandService(repository),
        process_service=ProjectProcessService(repository),
        work_graph=SimpleNamespace(snapshot=lambda **_: graph),
        run_reader=lambda _: [{"role": "assistant", "content": json.dumps(payload)}],
    )
    projection.on_run_terminal(
        SimpleNamespace(
            run_id=run.run_id,
            correlation_id=run.correlation_id,
            status=DurableRunStatus.COMPLETED,
        )
    )
    projected = intents.get(intent.planner_intent_id)
    assert projected.status.value == "PROJECTED"
    consumer = PlannerCommandConsumer(
        repository=repository,
        work_graph_repository=graph_repo,
        graph_effects=graph_effects,
        clock=lambda: NOW,
    )
    return repository, consumer, projected.decision_id, run.run_id


def rows(repository, table):
    with repository.transaction() as connection:
        return connection.execute(select(table)).mappings().all()


def task(task_id="task-new", dependencies=()):
    return {
        "type": "propose_task",
        "task_id": task_id,
        "team_id": "team-b",
        "title": "Implement result",
        "description": "Produce a reviewed result",
        "dependencies": list(dependencies),
        "contract": {
            "requested_capability": {
                "tags": ["write"],
                "protocol": "coifesp.task.v1",
                "input_contract_ref": "input:v1",
                "output_contract_ref": "output:v1",
                "verification_policy_ref": "verify:v1",
            },
            "input_manifest": {"resources": [], "work_nodes": []},
            "output_contract": {
                "artifact_types": ["text/plain"],
                "required": True,
                "max_count": 1,
            },
            "verification_policy": {
                "criteria": [
                    {"criterion_id": "review", "type": "human_review", "required": True}
                ]
            },
            "autonomy_requirement": "supervised",
        },
    }


def test_input_consumption_survives_restart_and_is_idempotent():
    repository, consumer, decision_id, _ = setup_batch(
        [
            {
                "type": "request_human_input",
                "question": "Which target?",
                "input_schema": {"type": "object"},
            }
        ],
        graph_effects=object(),
    )
    assert consumer.process_once(worker_id="one").status.value == "APPLIED"
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "APPLIED"
    assert len(rows(repository, PROJECT_INPUT_REQUESTS)) == 2
    with repository.transaction() as connection:
        process = repository.process(connection, "process-a")
        assert process.status.value == "WAITING"
        sequence = process.last_event_sequence
    consumer.consume(decision_id)
    assert consumer.process_once(worker_id="restarted") is None
    with repository.transaction() as connection:
        assert (
            repository.process(connection, "process-a").last_event_sequence == sequence
        )


def test_replan_creates_one_new_intent_and_counts_once():
    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Updated scope"}], graph_effects=object()
    )
    consumer.consume(decision_id)
    consumer.consume(decision_id)
    intents = rows(repository, PROJECT_PLANNER_INTENTS)
    assert len(intents) == 2
    assert len([row for row in intents if row["status"] == "PENDING"]) == 1
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 1


def test_stale_batch_has_no_domain_effects():
    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Updated scope"}], graph_effects=object()
    )
    with repository.transaction() as connection:
        current = repository.process(connection, "process-a")
    ProjectProcessService(repository).append_fact(
        process_id="process-a",
        event_id="changed",
        event_type="risk.created",
        expected_version=current.version,
        expected_event_sequence=current.last_event_sequence,
        subject_type="risk",
        subject_id="elsewhere",
        initiated_by="lead-a",
        executed_as="lead-a",
        correlation_id="changed",
        payload={},
    )
    assert consumer.consume(decision_id).status.value == "STALE"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 0
    assert len(rows(repository, PROJECT_PLANNER_INTENTS)) == 1


@pytest.mark.parametrize(
    "corruption", ["owner", "status", "batch_digest", "double_replan"]
)
def test_source_and_batch_binding_rejected_without_effects(corruption):
    commands = [{"type": "request_replan", "reason": "Updated scope"}]
    if corruption == "double_replan":
        commands.append({"type": "request_replan", "reason": "Again"})
    repository, consumer, decision_id, run_id = setup_batch(
        commands, graph_effects=object()
    )
    with repository.transaction() as connection:
        if corruption in {"owner", "status"}:
            values = (
                {"owner_principal_id": "lead-a"}
                if corruption == "owner"
                else {"status": "failed"}
            )
            connection.execute(
                AGENT_RUNS.update()
                .where(AGENT_RUNS.c.run_id == run_id)
                .values(**values)
            )
        elif corruption == "batch_digest":
            connection.execute(
                PROJECT_ORCHESTRATION_DECISIONS.update().values(
                    command_batch_digest="f" * 64
                )
            )
    consumer.consume(decision_id)
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"


def test_outer_transaction_failure_after_savepoint_release_rolls_back(monkeypatch):
    from contextlib import contextmanager

    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Updated scope"}], graph_effects=object()
    )
    original = repository.transaction

    @contextmanager
    def fail_commit():
        with original() as connection:
            yield connection
            raise OSError("before outer commit")

    monkeypatch.setattr(repository, "transaction", fail_commit)
    with pytest.raises(OSError, match="before outer commit"):
        consumer.consume(decision_id)
    monkeypatch.setattr(repository, "transaction", original)
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "PENDING"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 0
    assert len(rows(repository, PROJECT_PLANNER_INTENTS)) == 1
    consumer.consume(decision_id)
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 1


def test_multiple_input_commands_do_not_stale_their_own_batch():
    repository, consumer, decision_id, _ = setup_batch(
        [
            {
                "type": "request_human_input",
                "question": "Scope?",
                "input_schema": {"type": "object"},
            },
            {
                "type": "request_human_input",
                "question": "Deadline?",
                "input_schema": {"type": "object"},
            },
        ],
        graph_effects=object(),
    )
    consumer.consume(decision_id)
    assert len(rows(repository, PROJECT_INPUT_REQUESTS)) == 3
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "APPLIED"


def test_empty_batch_is_a_durable_noop():
    repository, consumer, decision_id, _ = setup_batch([], graph_effects=object())
    consumer.consume(decision_id)
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "APPLIED"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["generated_task_count"] == 0


def test_forward_task_dependencies_and_graph_proposals_apply_atomically():
    from coifesp_harness.work_graph.repository import PROJECT_DECISIONS, PROJECT_RISKS

    commands = [
        task("consumer", ["supplier"]),
        task("supplier"),
        {
            "type": "propose_risk",
            "risk_id": "risk-new",
            "title": "Capacity risk",
            "description": "Need a slot",
            "severity": "high",
            "likelihood": "medium",
            "mitigation": "Discuss schedule",
        },
        {
            "type": "propose_decision",
            "decision_id": "choice-new",
            "title": "Choose format",
            "description": "Confirm a format",
            "options": ["text", "html"],
        },
    ]
    repository, consumer, decision_id, _ = setup_batch(commands)
    consumer.consume(decision_id)
    consumer.consume(decision_id)
    assert len(rows(repository, TEAM_TASKS)) == 2
    assert len(rows(repository, PROJECT_RISKS)) == 1
    assert rows(repository, PROJECT_DECISIONS)[0]["status"] == "proposed"
    with repository.transaction() as connection:
        graph = consumer.graph.snapshot(connection, project_id="project-a")
    assert len(graph.relations) == 1
    assert graph.relations[0].source_node_id == "node:task:consumer"
    assert graph.relations[0].target_node_id == "node:task:supplier"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["generated_task_count"] == 2


def test_graph_batch_domain_error_rolls_back_precreated_tasks():
    from coifesp_harness.errors import GovernanceConflictError

    class RejectGraph:
        def apply(self, *_args, **_kwargs):
            raise GovernanceConflictError("domain rule rejects dependency")

    repository, consumer, decision_id, _ = setup_batch(
        [task("consumer", ["supplier"]), task("supplier")], graph_effects=RejectGraph()
    )
    consumer.consume(decision_id)
    assert rows(repository, TEAM_TASKS) == []
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["generated_task_count"] == 0
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"


def test_input_work_node_outside_project_rejected():
    command = task()
    command["contract"]["input_manifest"]["work_nodes"] = ["node:task:foreign"]
    repository, consumer, decision_id, _ = setup_batch(
        [command], graph_effects=object()
    )
    consumer.consume(decision_id)
    assert rows(repository, TEAM_TASKS) == []
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"


def test_replan_budget_opens_gate_without_new_intent():
    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Another attempt"}],
        graph_effects=object(),
    )
    with repository.transaction() as connection:
        connection.execute(PROJECT_EXECUTION_USAGE.update().values(replan_count=3))
    consumer.consume(decision_id)
    assert len(rows(repository, PROJECT_PLANNER_INTENTS)) == 1
    assert rows(repository, PROJECT_GATES)[0]["gate_type"] == "BUDGET"


def test_runner_consumes_pending_commands_before_deterministic_wakeup():
    from coifesp_harness.project_process.runner import ProjectOrchestratorRunner

    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Updated scope"}], graph_effects=object()
    )

    def unexpected(**_):
        pytest.fail("scheduler must not claim before pending Planner batch consumption")

    runner = ProjectOrchestratorRunner(
        repository=repository,
        process_service=ProjectProcessService(repository),
        command_service=ProjectProcessCommandService(repository),
        scheduler=SimpleNamespace(claim=unexpected),
        snapshot_loader=None,
        command_consumer=consumer,
    )
    result = runner.process_once(worker_id="restarted", process_id="process-a")
    assert result.decision_id == decision_id
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "APPLIED"

    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 1


def test_crash_after_effect_rolls_back_control_and_keeps_pending():
    repository, consumer, decision_id, _ = setup_batch(
        [
            {
                "type": "request_human_input",
                "question": "Confirm?",
                "input_schema": {"type": "object"},
            }
        ],
        graph_effects=object(),
    )

    def crash(*_):
        raise OSError("simulated consumer crash")

    consumer.after_effect = crash
    with pytest.raises(OSError, match="simulated"):
        consumer.consume(decision_id)
    assert [row["status"] for row in rows(repository, PROJECT_INPUT_REQUESTS)] == [
        "ANSWERED"
    ]
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "PENDING"
    consumer.after_effect = None
    consumer.consume(decision_id)
    assert len(rows(repository, PROJECT_INPUT_REQUESTS)) == 2


def test_budget_exhaustion_opens_gate_without_creating_task():
    repository, consumer, decision_id, _ = setup_batch([task()], graph_effects=object())
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_EXECUTION_USAGE.update().values(generated_task_count=20)
        )
    consumer.consume(decision_id)
    assert rows(repository, TEAM_TASKS) == []
    assert rows(repository, PROJECT_GATES)[0]["gate_type"] == "BUDGET"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["generated_task_count"] == 20
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"


def test_task_creation_persists_real_machine_provenance_and_unaccepted_contract():
    repository, consumer, decision_id, run_id = setup_batch(
        [task()], graph_effects=object()
    )
    consumer.consume(decision_id)
    consumer.consume(decision_id)
    created = rows(repository, TEAM_TASKS)
    assert len(created) == 1
    assert created[0]["created_by"] is None
    assert created[0]["source_planner_run_id"] == run_id
    assert created[0]["produced_by_principal_id"] == "service:project-orchestrator"
    assert created[0]["status"] == "proposed"
    assert created[0]["accepted_contract_version"] is None
    assert created[0]["source_contract_version"] == 1
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["generated_task_count"] == 1
    from coifesp_harness.control_plane.product_routes import _task_view
    from coifesp_harness.product.service import TeamCollaborationService

    model = TeamCollaborationService._task(created[0])
    view = _task_view(model)
    assert view.created_by is None
    assert view.source_planner_run_id == run_id


def test_legacy_incomplete_task_is_rejected_not_dispatched():
    command = task()
    command.pop("contract")
    repository, consumer, decision_id, _ = setup_batch(
        [command], graph_effects=object()
    )
    consumer.consume(decision_id)
    assert rows(repository, TEAM_TASKS) == []
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"


@pytest.mark.parametrize("change", ["process_version", "graph"])
def test_additional_stale_boundaries_apply_no_commands(change):
    from coifesp_harness.project_process.repository import PROJECT_PROCESSES
    from coifesp_harness.work_graph.repository import PROJECT_RISKS

    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Updated scope"}], graph_effects=object()
    )
    with repository.transaction() as connection:
        if change == "process_version":
            connection.execute(PROJECT_PROCESSES.update().values(version=2))
        else:
            connection.execute(
                PROJECT_RISKS.insert().values(
                    risk_id="concurrent-risk",
                    project_id="project-a",
                    title="Changed",
                    description="Concurrent graph update",
                    severity="low",
                    likelihood="low",
                    status="open",
                    mitigation="",
                    created_at=NOW,
                )
            )
            consumer.graph.register_node(
                connection,
                values={
                    "node_id": "node:risk:concurrent-risk",
                    "project_id": "project-a",
                    "node_type": "risk",
                    "subject_id": "concurrent-risk",
                    "created_at": NOW,
                },
            )
    assert consumer.consume(decision_id).status.value == "STALE"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 0


def test_same_team_task_is_rejected_before_storage_constraint():
    command = task()
    command["team_id"] = "team-a"
    repository, consumer, decision_id, _ = setup_batch(
        [command], graph_effects=object()
    )
    consumer.consume(decision_id)
    assert rows(repository, TEAM_TASKS) == []
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"


@pytest.mark.parametrize(
    "actual_mode,declared_mode,resource_project,allowed",
    [
        ("team_private", "team_private", "project-a", False),
        ("team_private", "project_readonly", "project-a", False),
        ("project_readonly", "project_readonly", "project-other", False),
        ("project_readonly", "project_readonly", "project-a", True),
        ("portable", "portable", "project-a", True),
    ],
)
def test_task_input_preserves_project_and_team_sharing(
    actual_mode, declared_mode, resource_project, allowed
):
    from coifesp_harness.product.repository import PROJECT_RESOURCES

    command = task()
    command["contract"]["input_manifest"]["resources"] = [
        {"resource_id": "resource-input", "required": True, "mode": declared_mode}
    ]
    repository, consumer, decision_id, _ = setup_batch(
        [command], graph_effects=object()
    )
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_RESOURCES.insert().values(
                resource_id="resource-input",
                project_id=resource_project,
                owner_team_id="team-a",
                created_by="lead-a",
                title="Source",
                artifact_owner_team_id="team-a",
                artifact_id="source-artifact",
                artifact_sha256="a" * 64,
                media_type="text/plain",
                propagation=actual_mode,
                created_at=NOW,
            )
        )
    consumer.consume(decision_id)
    assert len(rows(repository, TEAM_TASKS)) == int(allowed)
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == (
        "APPLIED" if allowed else "REJECTED"
    )


def test_preexisting_human_control_prevents_consumption():
    repository, consumer, decision_id, _ = setup_batch(
        [{"type": "request_replan", "reason": "Updated scope"}], graph_effects=object()
    )
    # Fault injection preserves the snapshot cursor to exercise the explicit
    # open-control check independently from the normal stale-event guard.
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_INPUT_REQUESTS.update().values(
                status="OPEN",
                response_json=None,
                answered_by=None,
                answered_at=None,
                resolution_idempotency_key=None,
                resolution_event_id=None,
                resolution_sha256=None,
            )
        )
    consumer.consume(decision_id)
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["replan_count"] == 0


def test_two_real_sqlite_consumers_create_and_count_one_task(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import URL, create_engine

    from coifesp_harness.project_process import SQLAlchemyProjectProcessRepository

    original, _, decision_id, _ = setup_batch([task()], graph_effects=object())
    engine = create_engine(
        URL.create("sqlite", database=str(tmp_path / "consumers.db")),
        connect_args={"check_same_thread": False, "timeout": 20},
    )
    # Copy the complete real-store fixture into a shared file-backed database,
    # then compete using separate SQLAlchemy connections (not a StaticPool).
    with original.engine.connect() as source, engine.connect() as target:
        source.connection.driver_connection.backup(target.connection.driver_connection)
    repository = SQLAlchemyProjectProcessRepository(engine)
    barrier = Barrier(2)

    def compete():
        consumer = PlannerCommandConsumer(
            repository=repository,
            work_graph_repository=SQLAlchemyWorkGraphRepository(engine),
            graph_effects=object(),
            clock=lambda: NOW,
        )
        barrier.wait(timeout=10)
        return consumer.consume(decision_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.submit(compete), workers.submit(compete)
        assert first.result(timeout=30).status.value == "APPLIED"
        assert second.result(timeout=30).status.value == "APPLIED"
    assert len(rows(repository, TEAM_TASKS)) == 1
    assert rows(repository, PROJECT_EXECUTION_USAGE)[0]["generated_task_count"] == 1
    engine.dispose()


@pytest.mark.parametrize(
    "status,reason", [("BLOCKED", "DEPENDENCY"), ("WAITING", "SCHEDULE")]
)
def test_exhausted_budget_preserves_existing_wait_without_poisoning_queue(
    status, reason
):
    repository, consumer, decision_id, _ = setup_batch(
        [task()],
        graph_effects=object(),
        process_state={"phase": "EXECUTION", "status": status, "wait_reason": reason},
    )
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_EXECUTION_USAGE.update().values(generated_task_count=20)
        )
    consumer.consume(decision_id)
    assert rows(repository, PROJECT_ORCHESTRATION_DECISIONS)[0]["status"] == "REJECTED"
    assert rows(repository, TEAM_TASKS) == []
    assert rows(repository, PROJECT_GATES) == []
    with repository.transaction() as connection:
        process = repository.process(connection, "process-a")
        assert (process.status.value, process.wait_reason.value) == (status, reason)
        assert repository.usage(connection, "process-a").generated_task_count == 20
    assert consumer.process_once(worker_id="next-poll") is None
    from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS

    events = rows(repository, PROJECT_PROCESS_EVENTS)
    budget_facts = [row for row in events if row["event_type"] == "project.budget.exhausted"]
    assert len(budget_facts) == 1
    assert budget_facts[0]["payload_json"]["gate_deferred"] is True
