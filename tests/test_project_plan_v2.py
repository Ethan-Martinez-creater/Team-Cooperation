import copy
import json

import pytest
import asyncio

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.models import PlanDraftStatus
from coifesp_harness.product.plan_schema import PLAN_V1, PLAN_V2, parse_project_plan
from coifesp_harness.product.planning import ProjectPlanningService
from coifesp_harness.work_graph import (
    ProjectWorkGraphService,
    SQLAlchemyWorkGraphRepository,
    WorkNodeType,
    WorkRelationType,
)

from test_project_conversations import seed as seed_conversations
from test_project_planning import _call, _issue_token, _stack as api_stack

PLAN_V2_PAYLOAD = {
    "schema": PLAN_V2,
    "goal": {
        "id": "goal-demo",
        "title": "完成 Harness 演示",
        "description": "形成可验证的多团队交付闭环。",
        "success_criteria": ["核心流程可重复执行"],
    },
    "scope": "规划、实现与验收 Harness 主链。",
    "requirements": [
        {
            "id": "req-api",
            "goal_id": "goal-demo",
            "title": "提供协作 API",
            "description": "API 必须支持幂等调用。",
            "requirement_type": "functional",
            "priority": "high",
        }
    ],
    "milestones": [
        {
            "id": "milestone-m1",
            "title": "M1",
            "description": "完成首个可运行版本。",
            "target_at": "2026-09-15T00:00:00Z",
            "completion_policy": {"required": ["task-build"]},
        }
    ],
    "phases": [
        {
            "id": "phase-build",
            "title": "实现",
            "description": "工程团队完成实现。",
            "milestone_id": "milestone-m1",
            "team_category": "engineering",
        }
    ],
    "tasks": [
        {
            "id": "task-build",
            "phase_id": "phase-build",
            "title": "实现协作 API",
            "description": "实现并自测幂等 API。",
            "team_category": "engineering",
            "acceptance_criteria": ["重复调用不产生重复对象"],
        }
    ],
    "dependencies": [
        {
            "source_id": "task-build",
            "relation_type": "depends_on",
            "target_id": "req-api",
        }
    ],
    "risks": [
        {
            "id": "risk-delay",
            "title": "排期延迟",
            "description": "跨团队排期可能延迟。",
            "severity": "high",
            "likelihood": "medium",
            "mitigation": "每日同步阻塞项。",
        }
    ],
    "team_requirements": [
        {
            "team_category": "engineering",
            "count": 2,
            "rationale": "完成核心实现",
        }
    ],
    "acceptance_criteria": ["端到端流程通过"],
}


def _stack():
    base = seed_conversations()
    repository = SQLAlchemyWorkGraphRepository(base["engine"])
    repository.create_schema()
    graph = ProjectWorkGraphService(repository)
    planning = ProjectPlanningService(
        base["engine"],
        collaboration=TeamCollaborationService(base["engine"]),
        work_graph=graph,
    )
    return base, planning, graph


def _import(planning, payload=None):
    return planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=json.dumps(payload or PLAN_V2_PAYLOAD, ensure_ascii=False),
        source_conversation_id="conv-1",
        source_turn_id="turn-plan-v2",
        source_run_id="run-plan-v2",
    )


def test_plan_v2_parser_and_draft_preserve_structured_payload():
    _, planning, _ = _stack()
    draft = _import(planning)
    assert draft.schema_version == PLAN_V2
    assert draft.plan_payload["goal"]["id"] == "goal-demo"
    assert draft.plan_payload["tasks"][0]["phase_id"] == "phase-build"
    assert draft.status is PlanDraftStatus.DRAFTING


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("requirements", 0, "goal_id"), "missing-goal", "unknown goal"),
        (("phases", 0, "milestone_id"), "missing-ms", "unknown milestone"),
        (("tasks", 0, "phase_id"), "missing-phase", "unknown phase"),
        (("dependencies", 0, "target_id"), "missing-node", "unknown local ID"),
    ],
)
def test_plan_v2_rejects_unresolved_local_references(path, value, message):
    payload = copy.deepcopy(PLAN_V2_PAYLOAD)
    collection, index, field = path
    payload[collection][index][field] = value
    with pytest.raises(ValueError, match=message):
        parse_project_plan(json.dumps(payload, ensure_ascii=False))


def test_plan_v2_approval_materializes_task_and_formal_work_graph():
    base, planning, graph = _stack()
    draft = _import(planning)
    approved = planning.approve_plan_draft(
        project_id="project-demo",
        draft_id=draft.draft_id,
        actor_id="lead-lin",
    )
    assert approved.status is PlanDraftStatus.APPROVED

    tasks = base["collaboration"].list_tasks(project_id="project-demo", actor_id="lead-lin")
    assert [(task.title, task.target_team_id) for task in tasks] == [
        ("实现协作 API", "team-engineering")
    ]
    snapshot = graph.snapshot(project_id="project-demo")
    assert {node.node_type for node in snapshot.nodes} == {
        WorkNodeType.GOAL,
        WorkNodeType.REQUIREMENT,
        WorkNodeType.MILESTONE,
        WorkNodeType.PHASE,
        WorkNodeType.TASK,
        WorkNodeType.RISK,
    }
    relation_types = {relation.relation_type for relation in snapshot.relations}
    assert relation_types == {
        WorkRelationType.DERIVED_FROM,
        WorkRelationType.PART_OF,
        WorkRelationType.RELATES_TO,
        WorkRelationType.DEPENDS_ON,
    }
    assert len(snapshot.relations) == 5
    assert not any(
        topic.title.startswith("里程碑")
        for topic in base["collaboration"].list_topics(
            project_id="project-demo", actor_id="lead-lin"
        )
    )


def test_plan_v2_projection_replay_converges_without_duplicates():
    base, planning, graph = _stack()
    draft = _import(planning)
    planning.approve_plan_draft(
        project_id="project-demo", draft_id=draft.draft_id, actor_id="lead-lin"
    )
    first = graph.snapshot(project_id="project-demo")
    planning._materialize_plan(project_id="project-demo", draft=draft, actor_id="lead-lin")
    planning._materialize_work_graph(project_id="project-demo", draft=draft, actor_id="lead-lin")
    second = graph.snapshot(project_id="project-demo")
    assert second.digest == first.digest
    assert (
        len(base["collaboration"].list_tasks(project_id="project-demo", actor_id="lead-lin")) == 1
    )


def test_plan_v2_missing_assignable_team_keeps_draft_open():
    _, planning, graph = _stack()
    payload = copy.deepcopy(PLAN_V2_PAYLOAD)
    payload["tasks"][0]["team_category"] = "operations"
    draft = _import(planning, payload)
    with pytest.raises(GovernanceConflictError, match="no assignable participating team"):
        planning.approve_plan_draft(
            project_id="project-demo", draft_id=draft.draft_id, actor_id="lead-lin"
        )
    current = next(
        item
        for item in planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
        if item.draft_id == draft.draft_id
    )
    assert current.status is PlanDraftStatus.DRAFTING
    assert not graph.snapshot(project_id="project-demo").nodes


def test_plan_v1_remains_compatible_and_projects_into_work_graph():
    _, planning, graph = _stack()
    payload = {
        "schema": PLAN_V1,
        "goals": "兼容旧计划",
        "scope": "保留 v1 导入能力",
        "phases": [],
        "milestones": [],
        "risks": [],
        "dependencies": [],
        "team_requirements": [],
        "acceptance_criteria": ["旧计划仍可批准"],
    }
    draft = _import(planning, payload)
    planning.approve_plan_draft(
        project_id="project-demo", draft_id=draft.draft_id, actor_id="lead-lin"
    )
    snapshot = graph.snapshot(project_id="project-demo")
    assert [node.node_type for node in snapshot.nodes] == [WorkNodeType.GOAL]


def test_plan_v2_api_exposes_schema_and_structured_payload():
    app, engine = api_stack()
    seed_conversations(with_engine=engine)
    token = _issue_token(engine, "lead-lin")
    response = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects/project-demo/plan-drafts:import",
            token=token,
            json={"content": json.dumps(PLAN_V2_PAYLOAD, ensure_ascii=False)},
        )
    )
    assert response.status_code == 201, response.text
    assert response.json()["schema_version"] == PLAN_V2
    assert response.json()["plan_payload"]["goal"]["id"] == "goal-demo"
