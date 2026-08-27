"""Project planning drafts and team requirement recommendations."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    TeamCollaborationService,
)
from coifesp_harness.product.models import PlanDraftStatus
from coifesp_harness.product.planning import ProjectPlanningService
from coifesp_harness.product.repository import ACCOUNT_SESSIONS
from coifesp_harness.product.workspace import ProjectWorkspaceService

from test_project_conversations import seed as seed_conversations

PLAN_OUTPUT = json.dumps(
    {
        "schema": "coifesp.project-plan.v1",
        "goals": "在 8 月底前完成多团队协作演示，覆盖规划、协商、开发与验收。",
        "scope": "包含会话工作区、Agent Exchange 与计划草案；不包含外部连接器。",
        "phases": [
            {"name": "规划", "description": "完成计划与团队需求确认", "order": 1},
            {"name": "开发", "description": "完成核心功能开发", "order": 2},
        ],
        "milestones": [{"name": "M1", "target": "8 月中旬"}],
        "risks": [{"name": "接口联调延期", "level": "high", "mitigation": "提前沟通排期"}],
        "dependencies": [{"name": "工程交付", "description": "依赖工程团队排期"}],
        "team_requirements": [
            {"team_category": "engineering", "count": 3, "rationale": "核心开发人力"},
            {"team_category": "quality", "count": 2, "rationale": "验收与回归"},
        ],
        "acceptance_criteria": ["端到端演示可运行", "回归测试通过"],
    },
    ensure_ascii=False,
)


def seed(*, with_engine=None):
    base = seed_conversations(with_engine=with_engine)
    planning = ProjectPlanningService(
        base["engine"], collaboration=TeamCollaborationService(base["engine"])
    )
    base["planning"] = planning
    return base


def test_import_plan_draft_creates_plan_and_requirements():
    s = seed()
    planning = s["planning"]
    plan = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_conversation_id="conv-1",
        source_turn_id="turn-1",
        source_run_id="run-1",
    )
    assert plan.status is PlanDraftStatus.DRAFTING
    assert len(plan.acceptance_criteria) == 2
    assert len(plan.phases) == 2
    requirements = planning.list_team_requirements(
        project_id="project-demo", actor_id="lead-lin"
    )
    assert {item.team_category for item in requirements} == {"engineering", "quality"}
    assert [item.team_count for item in requirements] == [3, 2]


def test_invalid_plan_output_rejected():
    s = seed()
    planning = s["planning"]
    for bad in ("not-json", json.dumps({"schema": "other"}), json.dumps({"schema": "coifesp.project-plan.v1", "goals": ""})):
        try:
            planning.import_plan_draft(
                project_id="project-demo", actor_id="lead-lin", content=bad
            )
            raise AssertionError(f"expected invalid plan output to be rejected: {bad[:40]}")
        except ValueError:
            pass


def test_approve_requires_owning_team():
    s = seed()
    planning = s["planning"]
    plan = planning.import_plan_draft(
        project_id="project-demo", actor_id="lead-lin", content=PLAN_OUTPUT
    )
    # engineering is not the initiating team
    try:
        planning.approve_plan_draft(
            project_id="project-demo", draft_id=plan.draft_id, actor_id="contributor-zhou"
        )
        raise AssertionError("expected non-owner approval to be rejected")
    except GovernanceConflictError:
        pass
    approved = planning.approve_plan_draft(
        project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
    )
    assert approved.status is PlanDraftStatus.APPROVED
    requirements = planning.list_team_requirements(
        project_id="project-demo", actor_id="lead-lin"
    )
    assert all(item.status is PlanDraftStatus.APPROVED for item in requirements)


def test_approve_projects_plan_topic():
    s = seed()
    planning = s["planning"]
    collaboration = s["collaboration"]
    plan = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-plan",
    )
    planning.approve_plan_draft(
        project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
    )
    topics = collaboration.list_topics(project_id="project-demo", actor_id="lead-lin")
    assert any(topic.title == "项目计划确认" for topic in topics)


def test_reject_plan_draft():
    s = seed()
    planning = s["planning"]
    plan = planning.import_plan_draft(
        project_id="project-demo", actor_id="lead-lin", content=PLAN_OUTPUT
    )
    rejected = planning.reject_plan_draft(
        project_id="project-demo",
        draft_id=plan.draft_id,
        actor_id="lead-lin",
        reason="需求尚未明确",
    )
    assert rejected.status is PlanDraftStatus.REJECTED
    assert rejected.rejection_reason == "需求尚未明确"


def test_non_participant_cannot_import_plan():
    s = seed()
    planning = s["planning"]
    s["accounts"].register_team(team_id="team-outsider", team_handle="outsider", team_name="外部")
    s["accounts"].ensure_active_account(
        account_id="outsider-nine",
        username="outsider-nine",
        display_name="外部",
        email="outsider9@demo.invalid",
        team_id="team-outsider",
    )
    try:
        planning.import_plan_draft(
            project_id="project-demo", actor_id="outsider-nine", content=PLAN_OUTPUT
        )
        raise AssertionError("expected non-participant import to be rejected")
    except ResourceNotFound:
        pass


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def _stack():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    collaboration = TeamCollaborationService(engine)
    workspace = ProjectWorkspaceService(engine)
    planning = ProjectPlanningService(engine, collaboration=collaboration)
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=directory,
        team_collaboration_service=collaboration,
        project_workspace_service=workspace,
        project_planning_service=planning,
    )
    return app, engine


def _issue_token(engine, account_id, token="test-bearer-token"):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(ACCOUNT_SESSIONS).values(
                session_hash=ProductAccountService._token_hash(token),
                account_id=account_id,
                created_at=now,
                expires_at=now + timedelta(hours=1),
                revoked_at=None,
            )
        )
    return token


async def _call(app, method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_api_plan_import_approve_flow():
    app, engine = _stack()
    seed(with_engine=engine)
    token = _issue_token(engine, "lead-lin")
    imported = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects/project-demo/plan-drafts:import",
            token=token,
            json={"content": PLAN_OUTPUT, "source_run_id": "run-plan"},
        )
    )
    assert imported.status_code == 201, imported.text
    plan_id = imported.json()["draft_id"]
    assert imported.json()["status"] == "drafting"

    requirements = asyncio.run(
        _call(
            app,
            "GET",
            "/v1/projects/project-demo/team-requirement-drafts",
            token=token,
        )
    )
    assert requirements.status_code == 200
    assert len(requirements.json()) == 2

    approved = asyncio.run(
        _call(
            app,
            "POST",
            f"/v1/projects/project-demo/plan-drafts/{plan_id}:approve",
            token=token,
            json={},
        )
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

def test_approval_materializes_milestones_and_phase_tasks():
    s = seed()
    planning = s["planning"]
    collaboration = s["collaboration"]
    output = json.loads(PLAN_OUTPUT)
    output["phases"][1]["team_category"] = "engineering"
    plan = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=json.dumps(output, ensure_ascii=False),
    )
    planning.approve_plan_draft(
        project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
    )
    # milestone became a project topic
    topics = collaboration.list_topics(project_id="project-demo", actor_id="lead-lin")
    milestone_topics = [t for t in topics if t.title.startswith("里程碑")]
    assert len(milestone_topics) == 1
    # the engineering-labelled phase became a task addressed to engineering
    tasks = collaboration.list_tasks(project_id="project-demo", actor_id="lead-lin")
    phase_tasks = [t for t in tasks if t.title.startswith("阶段执行")]
    assert len(phase_tasks) == 1
    assert phase_tasks[0].target_team_id == "team-engineering"
    assert phase_tasks[0].status.value == "proposed"
