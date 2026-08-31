from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.project_process.commands import (
    ProjectProcessCommand,
    ProjectProcessCommandStatus,
    ProjectProcessCommandType,
)
from coifesp_harness.project_process.planner_graph_effects import PlannerGraphMutations
from coifesp_harness.work_graph import (
    ProjectWorkGraphService,
    SQLAlchemyWorkGraphRepository,
)
from coifesp_harness.work_graph.repository import (
    PROJECT_DECISIONS,
    PROJECT_RISKS,
    PROJECT_WORK_NODES,
    PROJECT_WORK_RELATIONS,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    accounts.register_team(team_id="team-a", team_handle="team-a", team_name="Team A")
    actor = accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    projects = ProjectDirectoryService(engine)
    for project_id in ("project-a", "project-b"):
        projects.create_project(
            project_id=project_id,
            name=project_id,
            description="Planner graph test",
            actor_id=actor.account_id,
            owner_assignment_name="Owner",
            owner_kind=ProjectTeamKind.PRODUCT,
        )
    repository = SQLAlchemyWorkGraphRepository(engine)
    repository.create_schema()
    process = SimpleNamespace(process_id="process-a", project_id="project-a")
    return engine, repository, PlannerGraphMutations(repository), process


def _command(command_type, request, *, command_id="command-1", project_id="project-a"):
    return ProjectProcessCommand(
        command_id=command_id,
        process_id="process-a",
        project_id=project_id,
        decision_id="decision-batch",
        command_type=command_type,
        request_digest="request-digest",
        based_on_process_version=1,
        based_on_event_sequence=0,
        graph_snapshot_digest="graph-digest",
        status=ProjectProcessCommandStatus.PENDING,
        result_subject_id=None,
        created_at=NOW,
        applied_at=None,
        request_json=request,
    )


def _apply(adapter, engine, process, command):
    with engine.begin() as connection:
        return adapter.apply(
            connection,
            command=command,
            process=process,
            source_run_id="run-planner-1",
            now=NOW,
        )


def _goal_nodes(repository):
    service = ProjectWorkGraphService(repository)
    for project_id, goal_id in (
        ("project-a", "goal-a"),
        ("project-a", "goal-b"),
        ("project-a", "goal-c"),
        ("project-b", "goal-other"),
    ):
        service.create_goal(
            goal_id=goal_id,
            project_id=project_id,
            title=goal_id,
            description="Goal",
            success_criteria=("done",),
            created_by="lead-a",
        )


def test_risk_is_materialized_with_open_status_and_graph_node():
    engine, _repository, adapter, process = _stack()
    command = _command(
        ProjectProcessCommandType.PROPOSE_RISK,
        {
            "risk_id": "risk-1",
            "title": "Capacity",
            "description": "May be late",
            "severity": "high",
            "likelihood": "medium",
            "mitigation": "Track load",
        },
    )

    assert _apply(adapter, engine, process, command) == "risk-1"
    with engine.connect() as connection:
        risk = (
            connection.execute(select(PROJECT_RISKS).where(PROJECT_RISKS.c.risk_id == "risk-1"))
            .mappings()
            .one()
        )
        node = (
            connection.execute(
                select(PROJECT_WORK_NODES).where(PROJECT_WORK_NODES.c.subject_id == "risk-1")
            )
            .mappings()
            .one()
        )
    assert risk["status"] == "open"
    assert risk["source_run_id"] == "run-planner-1"
    assert node["node_id"] == "node:risk:risk-1"
    assert node["node_type"] == "risk"


def test_decision_uses_canonical_options_and_proposed_identity():
    engine, _repository, adapter, process = _stack()
    command = _command(
        ProjectProcessCommandType.PROPOSE_DECISION,
        {
            "decision_id": "decision-1",
            "title": "Deployment",
            "description": "Choose a path",
            "options": ["blue", "green"],
        },
    )

    assert _apply(adapter, engine, process, command) == "decision-1"
    with engine.connect() as connection:
        decision = (
            connection.execute(
                select(PROJECT_DECISIONS).where(PROJECT_DECISIONS.c.decision_id == "decision-1")
            )
            .mappings()
            .one()
        )
        node = (
            connection.execute(
                select(PROJECT_WORK_NODES).where(PROJECT_WORK_NODES.c.subject_id == "decision-1")
            )
            .mappings()
            .one()
        )
    assert decision["decision"] == '["blue","green"]'
    assert decision["rationale"] == "Choose a path"
    assert decision["status"] == "proposed"
    assert decision["proposed_by"] == "service:project-orchestrator"
    assert decision["approved_by"] is None
    assert node["node_id"] == "node:decision:decision-1"


@pytest.mark.parametrize(
    "kind, payload",
    [
        (
            ProjectProcessCommandType.PROPOSE_RISK,
            {
                "risk_id": "claimed",
                "title": "New",
                "description": "New",
                "severity": "low",
                "likelihood": "low",
                "mitigation": "New",
            },
        ),
        (
            ProjectProcessCommandType.PROPOSE_DECISION,
            {
                "decision_id": "claimed",
                "title": "New",
                "description": "New",
                "options": ["one", "two"],
            },
        ),
    ],
)
def test_risk_and_decision_identifier_collision_does_not_overwrite(kind, payload):
    engine, _repository, adapter, process = _stack()
    first = _command(
        ProjectProcessCommandType.PROPOSE_RISK,
        {
            "risk_id": "claimed",
            "title": "Original",
            "description": "Original",
            "severity": "high",
            "likelihood": "high",
            "mitigation": "Keep",
        },
    )
    _apply(adapter, engine, process, first)

    with engine.begin() as connection:  # noqa: SIM117 - show rejection inside caller transaction
        with pytest.raises(GovernanceConflictError, match="identifier"):
            adapter.apply(
                connection,
                command=_command(kind, payload, command_id="command-2"),
                process=process,
                source_run_id="run-planner-2",
                now=NOW,
            )
    with engine.connect() as connection:
        row = (
            connection.execute(select(PROJECT_RISKS).where(PROJECT_RISKS.c.risk_id == "claimed"))
            .mappings()
            .one()
        )
    assert row["title"] == "Original"


def test_existing_decision_identifier_is_also_a_hard_collision():
    engine, _repository, adapter, process = _stack()
    original = _command(
        ProjectProcessCommandType.PROPOSE_DECISION,
        {
            "decision_id": "claimed-decision",
            "title": "Original",
            "description": "Original",
            "options": ["one", "two"],
        },
    )
    _apply(adapter, engine, process, original)
    replacement = _command(
        ProjectProcessCommandType.PROPOSE_DECISION,
        {
            "decision_id": "claimed-decision",
            "title": "Replacement",
            "description": "Replacement",
            "options": ["three", "four"],
        },
        command_id="command-2",
    )
    with engine.begin() as connection:  # noqa: SIM117 - show rejection inside caller transaction
        with pytest.raises(GovernanceConflictError, match="identifier"):
            adapter.apply(
                connection,
                command=replacement,
                process=process,
                source_run_id="run-planner-2",
                now=NOW,
            )
    with engine.connect() as connection:
        row = connection.execute(
            select(PROJECT_DECISIONS).where(
                PROJECT_DECISIONS.c.decision_id == "claimed-decision"
            )
        ).mappings().one()
    assert row["title"] == "Original"


def test_dependency_resolves_subject_and_node_aliases_and_reuses_semantic_edge():
    engine, repository, adapter, process = _stack()
    _goal_nodes(repository)
    first = _command(
        ProjectProcessCommandType.PROPOSE_DEPENDENCY,
        {
            "source_id": "goal-a",
            "target_id": "node:goal:goal-b",
        },
    )
    relation_id = _apply(adapter, engine, process, first)
    assert relation_id == PlannerGraphMutations.relation_id_for_command(first.command_id)

    second = _command(
        ProjectProcessCommandType.PROPOSE_DEPENDENCY,
        {
            "source_id": "node:goal:goal-a",
            "target_id": "goal-b",
        },
        command_id="command-2",
    )
    assert _apply(adapter, engine, process, second) == relation_id
    with engine.connect() as connection:
        relations = connection.execute(select(PROJECT_WORK_RELATIONS)).mappings().all()
    assert len(relations) == 1
    assert relations[0]["source_node_id"] == "node:goal:goal-a"
    assert relations[0]["target_node_id"] == "node:goal:goal-b"
    assert relations[0]["relation_type"] == "depends_on"
    assert relations[0]["created_by_id"] == "service:project-orchestrator"


def test_dependency_rejects_self_cross_project_alias_and_cycle():
    engine, repository, adapter, process = _stack()
    _goal_nodes(repository)
    with pytest.raises(GovernanceConflictError, match="itself"):
        _apply(
            adapter,
            engine,
            process,
            _command(
                ProjectProcessCommandType.PROPOSE_DEPENDENCY,
                {"source_id": "goal-a", "target_id": "node:goal:goal-a"},
            ),
        )
    with pytest.raises(ResourceNotFound, match="target_id"):
        _apply(
            adapter,
            engine,
            process,
            _command(
                ProjectProcessCommandType.PROPOSE_DEPENDENCY,
                {"source_id": "goal-a", "target_id": "goal-other"},
            ),
        )

    _apply(
        adapter,
        engine,
        process,
        _command(
            ProjectProcessCommandType.PROPOSE_DEPENDENCY,
            {"source_id": "goal-a", "target_id": "goal-b"},
            command_id="edge-a-b",
        ),
    )
    _apply(
        adapter,
        engine,
        process,
        _command(
            ProjectProcessCommandType.PROPOSE_DEPENDENCY,
            {"source_id": "goal-b", "target_id": "goal-c"},
            command_id="edge-b-c",
        ),
    )
    with pytest.raises(GovernanceConflictError, match="cycle"):
        _apply(
            adapter,
            engine,
            process,
            _command(
                ProjectProcessCommandType.PROPOSE_DEPENDENCY,
                {"source_id": "goal-c", "target_id": "goal-a"},
                command_id="edge-c-a",
            ),
        )


def test_outer_transaction_rolls_back_prior_graph_effects():
    engine, _repository, adapter, process = _stack()
    risk = _command(
        ProjectProcessCommandType.PROPOSE_RISK,
        {
            "risk_id": "risk-rollback",
            "title": "Capacity",
            "description": "May be late",
            "severity": "high",
            "likelihood": "medium",
            "mitigation": "Track load",
        },
    )
    invalid = _command(
        ProjectProcessCommandType.PROPOSE_DECISION,
        {
            "decision_id": "decision-rollback",
            "title": "Invalid",
            "description": "No choices",
            "options": ["only-one"],
        },
        command_id="command-invalid",
    )
    with pytest.raises(ValueError, match="options"):  # noqa: SIM117 - exception must leave transaction
        with engine.begin() as connection:
            adapter.apply(
                connection, command=risk, process=process, source_run_id="run-planner-1", now=NOW
            )
            adapter.apply(
                connection, command=invalid, process=process, source_run_id="run-planner-1", now=NOW
            )
    with engine.connect() as connection:
        assert connection.execute(select(PROJECT_RISKS)).mappings().all() == []
        assert connection.execute(select(PROJECT_WORK_NODES)).mappings().all() == []
