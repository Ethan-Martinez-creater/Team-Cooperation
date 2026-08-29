import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import AgentRunCheckpointCodec, DurableRunStatus
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.project_process import (
    ORCHESTRATOR_PRINCIPAL_ID,
    PLANNER_DECISION_SCHEMA,
    ProjectPlannerIntentService,
    ProjectPlannerIntentStatus,
    ProjectPlannerProjection,
    ProjectProcessCommandService,
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.work_graph import ProjectGraphSnapshot

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _stack():
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
        description="Planner intent test",
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
    process = process_service.start_process(
        process_id="process-a",
        project_id="project-a",
        execution_policy_id="policy-a",
        started_by="lead-a",
    )
    graph = ProjectGraphSnapshot("project-a", (), (), (), "sha256:" + "a" * 64)
    return repository, process, graph


def _create(service, process, graph, **overrides):
    values = {
        "process_id": process.process_id,
        "project_id": process.project_id,
        "owner_team_id": "team-a",
        "reason": "INITIAL_PLANNING",
        "based_on_process_version": process.version,
        "based_on_event_sequence": process.last_event_sequence,
        "graph": graph,
    }
    values.update(overrides)
    return service.create(**values)


def test_intent_creation_is_snapshot_bound_and_idempotent():
    repository, process, graph = _stack()
    service = ProjectPlannerIntentService(repository, clock=lambda: NOW)

    first = _create(service, process, graph)
    second = _create(service, process, graph)

    assert first == second
    assert first.status is ProjectPlannerIntentStatus.PENDING
    assert first.graph_snapshot_digest == graph.digest
    assert first.planner_intent_id.startswith("planner-intent:")


def test_stale_snapshot_and_nonparticipant_owner_fail_closed():
    repository, process, graph = _stack()
    service = ProjectPlannerIntentService(repository, clock=lambda: NOW)

    with pytest.raises(GovernanceConflictError, match="snapshot is stale"):
        _create(service, process, graph, based_on_process_version=process.version + 1)
    with pytest.raises(GovernanceConflictError, match="not a project participant"):
        _create(service, process, graph, owner_team_id="team-outsider")


def test_launch_uses_an_ordinary_toolless_durable_agent_run():
    repository, process, graph = _stack()
    service = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    intent = _create(service, process, graph)
    captured = {}

    class FakeRunService:
        def create(self, **values):
            captured.update(values)
            return SimpleNamespace(run_id=values["run_id"])

    run = service.launch(intent=intent, graph=graph, run_service=FakeRunService())
    checkpoint = AgentRunCheckpointCodec().decode(captured["checkpoint"])
    bound = service.get(intent.planner_intent_id)

    assert bound.status is ProjectPlannerIntentStatus.RUNNING
    assert bound.run_id == run.run_id
    assert captured["principal"].principal_id == ORCHESTRATOR_PRINCIPAL_ID
    assert captured["principal"].is_service is True
    assert captured["principal"].tenant_id == "team-a"
    assert checkpoint["tool_authorization"] is None
    assert checkpoint["messages"][0].role == "system"
    context = checkpoint["context_items"][0]
    assert context.instruction_trust.value == "data_only"
    context_payload = json.loads(context.content)
    assert context_payload["intent"]["schema"] == PLANNER_DECISION_SCHEMA
    assert context_payload["graph"]["digest"] == graph.digest
    assert context_payload["graph"]["project_id"] == "project-a"


def test_request_loads_current_snapshot_and_reuses_deterministic_run():
    repository, process, graph = _stack()
    service = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    created = {}

    class FakeRunService:
        def create(self, **values):
            created.setdefault(values["idempotency_key"], values["run_id"])
            return SimpleNamespace(run_id=created[values["idempotency_key"]])

    work_graph = SimpleNamespace(snapshot=lambda **_: graph)
    first_intent, first_run = service.request(
        process_id=process.process_id,
        owner_team_id="team-a",
        reason="ANALYSIS",
        work_graph=work_graph,
        run_service=FakeRunService(),
    )
    second_intent, second_run = service.request(
        process_id=process.process_id,
        owner_team_id="team-a",
        reason="ANALYSIS",
        work_graph=work_graph,
        run_service=FakeRunService(),
    )

    assert first_intent == second_intent
    assert first_run.run_id == second_run.run_id
    assert len(created) == 1


def test_terminal_intent_update_is_idempotent_but_conflicts_on_different_outcome():
    repository, process, graph = _stack()
    service = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    intent = _create(service, process, graph)
    service.bind_run(intent.planner_intent_id, "run-a")

    finished = service.finish(
        intent.planner_intent_id,
        status=ProjectPlannerIntentStatus.PROJECTED,
        decision_id="decision-a",
    )
    replay = service.finish(
        intent.planner_intent_id,
        status=ProjectPlannerIntentStatus.PROJECTED,
        decision_id="decision-a",
    )

    assert replay == finished
    assert finished.projected_at.replace(tzinfo=UTC) == NOW
    with pytest.raises(GovernanceConflictError, match="already terminal"):
        service.finish(
            intent.planner_intent_id,
            status=ProjectPlannerIntentStatus.REJECTED,
            error_code="different_result",
        )


def _projection_stack(*, graph=None, payload_mutator=None):
    repository, process, original_graph = _stack()
    intent_service = ProjectPlannerIntentService(repository, clock=lambda: NOW)
    intent = _create(intent_service, process, original_graph)
    intent_service.bind_run(intent.planner_intent_id, "run-planner-a")
    payload = intent_service.protocol(intent)
    payload["commands"] = [{"type": "request_replan", "reason": "Re-evaluate risks."}]
    if payload_mutator is not None:
        payload_mutator(payload)
    projection = ProjectPlannerProjection(
        intent_service=intent_service,
        command_service=ProjectProcessCommandService(repository, clock=lambda: NOW),
        process_service=ProjectProcessService(repository, clock=lambda: NOW),
        work_graph=SimpleNamespace(
            snapshot=lambda **_: graph or original_graph
        ),
        run_reader=lambda _run: (
            {"role": "assistant", "content": json.dumps(payload)},
        ),
    )
    run = SimpleNamespace(
        run_id="run-planner-a",
        correlation_id=f"planner-intent:{intent.planner_intent_id}",
        status=DurableRunStatus.COMPLETED,
    )
    return repository, intent_service, intent, projection, run


def test_projection_validates_then_persists_commands():
    repository, intent_service, intent, projection, run = _projection_stack()

    projection.on_run_terminal(run)

    projected = intent_service.get(intent.planner_intent_id)
    assert projected.status is ProjectPlannerIntentStatus.PROJECTED
    with repository.transaction() as connection:
        decision = repository.decision(connection, projected.decision_id)
        commands = repository.commands_for_decision(connection, projected.decision_id)
    assert decision.status.value == "PENDING"
    assert len(commands) == 1
    assert commands[0].request == {"reason": "Re-evaluate risks."}


def test_projection_rejects_unknown_direct_execution_field_without_commands():
    repository, intent_service, intent, projection, run = _projection_stack(
        payload_mutator=lambda payload: payload["commands"][0].update({"tool": "shell"})
    )

    projection.on_run_terminal(run)

    rejected = intent_service.get(intent.planner_intent_id)
    assert rejected.status is ProjectPlannerIntentStatus.REJECTED
    assert rejected.error_code == "invalid_planner_output"
    with repository.transaction() as connection:
        assert repository.decision(connection, "planner-decision:missing") is None


def test_projection_marks_changed_graph_stale_and_persists_zero_commands():
    changed_graph = ProjectGraphSnapshot(
        "project-a", (), (), (), "sha256:" + "b" * 64
    )
    repository, intent_service, intent, projection, run = _projection_stack(
        graph=changed_graph
    )

    projection.on_run_terminal(run)

    stale = intent_service.get(intent.planner_intent_id)
    assert stale.status is ProjectPlannerIntentStatus.STALE
    with repository.transaction() as connection:
        decision = repository.decision(connection, stale.decision_id)
        commands = repository.commands_for_decision(connection, stale.decision_id)
        events = repository.events(connection, intent.process_id)
    assert decision.status.value == "STALE"
    assert commands == ()
    assert decision.decision_json["commands"] == []
    assert events[-1].event_type == "project.orchestrator.decision_stale"
    assert events[-1].subject_id == stale.decision_id
