"""Agent-first workspace: Local demo bootstrap and product workspace APIs."""
import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    TeamCollaborationService,
)
from coifesp_harness.product.demo_bootstrap import ensure_local_demo
from coifesp_harness.product.workspace import ProjectWorkspaceService


def _local_app():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    collaboration = TeamCollaborationService(engine)
    workspace = ProjectWorkspaceService(engine)
    ensure_local_demo(engine=engine)
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "local"}
        ),
        product_account_service=accounts,
        project_directory_service=directory,
        team_collaboration_service=collaboration,
        project_workspace_service=workspace,
    )
    return app


async def _call(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_local_config_enables_product_workspace():
    app = _local_app()
    config = asyncio.run(_call(app, "GET", "/app/config")).json()
    assert config["auth_mode"] == "local"
    assert config["product_workspace_enabled"] is True
    assert {p["profile_id"] for p in config["profiles"]} == {"lead", "contributor", "reviewer"}


def test_local_demo_three_identities_see_project_with_unique_conversations():
    app = _local_app()
    conversation_ids = set()
    for profile_id, token in (("lead", "t-lead"), ("contributor", "t-eng"), ("reviewer", "t-qa")):
        login = asyncio.run(
            _call(app, "POST", "/app/local-session", json={"profile_id": profile_id})
        )
        access_token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}
        projects = asyncio.run(_call(app, "GET", "/v1/workspace/projects", headers=headers))
        assert projects.status_code == 200, projects.text
        body = projects.json()
        assert [item["project"]["project_id"] for item in body] == ["project-coifesp-demo"]
        conversation_id = body[0]["conversation_id"]
        assert conversation_id
        conversation_ids.add(conversation_id)
    assert len(conversation_ids) == 3


def test_workspace_snapshot_aggregates_project_state():
    app = _local_app()
    login = asyncio.run(_call(app, "POST", "/app/local-session", json={"profile_id": "lead"}))
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    snapshot = asyncio.run(
        _call(app, "GET", "/v1/projects/project-coifesp-demo/workspace", headers=headers)
    )
    assert snapshot.status_code == 200, snapshot.text
    body = snapshot.json()
    assert body["conversation"] is not None
    assert {team["team_id"] for team in body["teams"]} == {
        "team-product",
        "team-engineering",
        "team-quality",
    }
    assert body["task_count"] == 0
    assert body["resource_count"] == 0


def test_demo_bootstrap_is_idempotent_across_restarts():
    from coifesp_harness.product.demo_bootstrap import ensure_local_demo

    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    first = ensure_local_demo(engine=engine)
    second = ensure_local_demo(engine=engine)
    assert first.project_id == second.project_id
    assert first.conversation_ids == second.conversation_ids
    workspace = ProjectWorkspaceService(engine)
    assert len(workspace.list_workspace_projects(actor_id="lead-lin")) == 1


def test_local_lead_can_add_related_team_to_new_agent_first_project():
    app = _local_app()
    login = asyncio.run(_call(app, "POST", "/app/local-session", json={"profile_id": "lead"}))
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects",
            headers=headers,
            json={
                "name": "Agent-first team setup",
                "description": "browser regression",
                "owner_assignment_name": "产品统筹",
                "owner_kind": "product",
            },
        )
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]
    relations = asyncio.run(_call(app, "GET", "/v1/team-relations", headers=headers))
    assert relations.status_code == 200, relations.text
    assert "team-engineering" in {item["team"]["team_id"] for item in relations.json()}
    added = asyncio.run(
        _call(
            app,
            "POST",
            f"/v1/projects/{project_id}/teams",
            headers=headers,
            json={
                "team_id": "team-engineering",
                "assignment_name": "工程交付",
                "kind": "engineering",
            },
        )
    )
    assert added.status_code == 201, added.text
    snapshot = asyncio.run(
        _call(app, "GET", f"/v1/projects/{project_id}/workspace", headers=headers)
    )
    assert {team["team_id"] for team in snapshot.json()["teams"]} == {
        "team-product",
        "team-engineering",
    }


def test_agent_first_assets_cover_browser_acceptance_fixes():
    app = _local_app()
    workspace_script = asyncio.run(_call(app, "GET", "/app/project-workspace.js"))
    app_script = asyncio.run(_call(app, "GET", "/app/app.js"))
    workspace_css = asyncio.run(_call(app, "GET", "/app/project-workspace.css"))
    assert workspace_script.status_code == app_script.status_code == workspace_css.status_code == 200
    assert 'expected_propagation: "team_private"' in workspace_script.text
    assert 'class="secondary ws-add-team">添加参与团队' in workspace_script.text
    assert 'W.api("/v1/team-relations")' in workspace_script.text
    assert 'saveRoute({ view: "project-workspace", project_id: projectId })' in workspace_script.text
    assert 'sessionStorage.getItem("local_profile")' in app_script.text
    assert 'route.view==="project-workspace"' in app_script.text
    assert "/agent-exchanges/${encodeURIComponent(x.exchange_id)}/responses" in workspace_script.text
    assert "来自 ${esc(response.recipient_team_id)} 的回复" in workspace_script.text
    assert "if(approvalBadge)" in app_script.text
    assert "grid-template-columns: auto minmax(0, 1fr) auto" in workspace_css.text
    assert "@media (max-width: 560px)" in workspace_css.text
