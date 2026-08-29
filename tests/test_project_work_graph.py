from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.work_graph import (
    ProjectWorkGraphService,
    SQLAlchemyWorkGraphRepository,
    WorkNodeType,
    WorkRelationType,
)
from coifesp_harness.work_graph.repository import PROJECT_RISKS


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
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project",
        description="Graph project",
        actor_id=actor.account_id,
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    repository = SQLAlchemyWorkGraphRepository(engine)
    repository.create_schema()
    return engine, ProjectWorkGraphService(repository)


def _formal_graph(service):
    service.create_goal(
        goal_id="goal-1",
        project_id="project-a",
        title="Deliver",
        description="Deliver the system",
        success_criteria=("accepted",),
        created_by="lead-a",
    )
    service.create_requirement(
        requirement_id="req-1",
        project_id="project-a",
        goal_id="goal-1",
        title="API",
        description="Provide an API",
        requirement_type="functional",
        priority="high",
        source_type="plan",
        source_id="plan-1",
    )
    service.create_milestone(
        milestone_id="milestone-1",
        project_id="project-a",
        title="M1",
        description="First milestone",
        target_at=datetime(2026, 9, 1, tzinfo=UTC),
        completion_policy={"required": ["req-1"]},
    )
    service.create_phase(
        phase_id="phase-1",
        project_id="project-a",
        title="Build",
        description="Build phase",
        milestone_id="milestone-1",
        owner_team_id="team-a",
    )
    service.create_risk(
        risk_id="risk-1",
        project_id="project-a",
        title="Delay",
        description="Schedule risk",
        severity="high",
        likelihood="medium",
        mitigation="Track milestone",
    )
    service.create_decision(
        decision_id="decision-1",
        project_id="project-a",
        title="Use stable IDs",
        decision="Derive materialized IDs from plan-local IDs",
        rationale="Projection retries must converge",
        proposed_by="lead-a",
    )


def test_formal_objects_register_nodes_and_snapshot_is_deterministic():
    engine, service = _stack()
    _formal_graph(service)
    first = service.snapshot(project_id="project-a")
    assert first.digest == service.snapshot(project_id="project-a").digest
    assert {node.node_type for node in first.nodes} == {
        WorkNodeType.GOAL,
        WorkNodeType.REQUIREMENT,
        WorkNodeType.MILESTONE,
        WorkNodeType.PHASE,
        WorkNodeType.RISK,
        WorkNodeType.DECISION,
    }
    with engine.begin() as connection:
        connection.execute(
            update(PROJECT_RISKS)
            .where(PROJECT_RISKS.c.risk_id == "risk-1")
            .values(status="resolved")
        )
    assert service.snapshot(project_id="project-a").digest != first.digest


def test_idempotent_replay_converges_and_conflicting_content_fails():
    _, service = _stack()
    values = dict(
        goal_id="goal-1",
        project_id="project-a",
        title="Deliver",
        description="Deliver the system",
        success_criteria=("accepted",),
        created_by="lead-a",
    )
    first_subject, first_node = service.create_goal(**values)
    second_subject, second_node = service.create_goal(**values)
    assert first_subject["goal_id"] == second_subject["goal_id"]
    assert first_node.node_id == second_node.node_id
    with pytest.raises(GovernanceConflictError):
        service.create_goal(**{**values, "title": "Different"})


def test_relations_reject_self_cycle_and_missing_nodes():
    _, service = _stack()
    _formal_graph(service)
    values = dict(
        relation_id="edge-1",
        project_id="project-a",
        source_node_id="node:requirement:req-1",
        relation_type=WorkRelationType.DEPENDS_ON,
        target_node_id="node:goal:goal-1",
        created_by_type="human",
        created_by_id="lead-a",
    )
    first = service.add_relation(**values)
    assert service.add_relation(**values).relation_id == first.relation_id
    semantic_replay = service.add_relation(**{**values, "relation_id": "edge-same-semantics"})
    assert semantic_replay.relation_id == first.relation_id
    with pytest.raises(GovernanceConflictError, match="cycle"):
        service.add_relation(
            **{
                **values,
                "relation_id": "edge-cycle",
                "source_node_id": "node:goal:goal-1",
                "target_node_id": "node:requirement:req-1",
            }
        )
    with pytest.raises(GovernanceConflictError, match="itself"):
        service.add_relation(
            **{
                **values,
                "relation_id": "edge-self",
                "source_node_id": "node:goal:goal-1",
                "target_node_id": "node:goal:goal-1",
            }
        )
    with pytest.raises(GovernanceConflictError, match="itself"):
        service.add_relation(
            **{
                **values,
                "relation_id": "edge-self-relates",
                "relation_type": "relates_to",
                "source_node_id": "node:goal:goal-1",
                "target_node_id": "node:goal:goal-1",
            }
        )
    with pytest.raises(ResourceNotFound):
        service.add_relation(
            **{
                **values,
                "relation_id": "edge-missing",
                "relation_type": "relates_to",
                "target_node_id": "node:risk:missing",
            }
        )


def test_invalid_node_type_is_rejected():
    _, service = _stack()
    with pytest.raises(ValueError):
        service.register_existing_subject(
            node_id="node:x",
            project_id="project-a",
            node_type="invented",
            subject_id="x",
        )
