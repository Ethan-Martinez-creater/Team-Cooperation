import asyncio
from types import SimpleNamespace

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import DurableRunStatus
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    TeamCollaborationService,
)


class CompletedActionAgent:
    def __init__(self, target_team_id="team-placeholder"):
        self.target_team_id = target_team_id

    def get(self, *, principal, run_id):
        return SimpleNamespace(run_id=run_id, status=DurableRunStatus.COMPLETED)

    def conversation(self, *, principal, run_id):
        content = {
            "schema": "coifesp.collaboration-actions.v1",
            "actions": [
                {
                    "kind": "message",
                    "payload": {"target_team_id": self.target_team_id, "content": "请确认接口排期"},
                }
            ],
        }
        return (
            {"sequence": 1, "role": "user", "content": "生成协作建议", "name": None},
            {
                "sequence": 2,
                "role": "assistant",
                "content": __import__("json").dumps(content, ensure_ascii=False),
                "name": None,
            },
        )


def stack(*, agent_run_service=None):
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    app = create_app(
        settings=Settings.from_environment({"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}),
        product_account_service=accounts,
        project_directory_service=ProjectDirectoryService(engine),
        team_collaboration_service=TeamCollaborationService(engine),
        agent_run_service=agent_run_service,
    )
    return app


async def call(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_team_first_registration_and_admin_account_approval_api():
    app = stack()
    team = asyncio.run(
        call(app, "POST", "/v1/teams/register", json={"handle": "alpha-team", "name": "Alpha Team"})
    )
    assert team.status_code == 201, team.text
    details = team.json()
    initial = details["administrator_initial_password"]
    username = details["administrator_username"]
    team_id = details["team"]["team_id"]

    refused_login = asyncio.run(
        call(app, "POST", "/v1/sessions", json={"login": username, "password": initial})
    )
    assert refused_login.status_code == 401
    changed = asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": username,
                "current_password": initial,
                "new_password": "Admin-Correct-Horse-42!",
            },
        )
    )
    assert changed.status_code == 204, changed.text
    admin_login = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": username, "password": "Admin-Correct-Horse-42!"},
        )
    )
    assert admin_login.status_code == 200, admin_login.text
    token = admin_login.json()["access_token"]

    pending = asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/register",
            json={
                "username": "alice",
                "display_name": "Alice",
                "email": "alice@example.test",
                "password": "Member-Correct-Horse-42!",
                "team_id": team_id,
            },
        )
    )
    assert pending.status_code == 202, pending.text
    candidate = pending.json()["account_id"]
    not_active = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": "alice", "password": "Member-Correct-Horse-42!"},
        )
    )
    assert not_active.status_code == 401

    headers = {"Authorization": f"Bearer {token}"}
    queue = asyncio.run(
        call(app, "GET", "/v1/teams/current/account-registrations", headers=headers)
    )
    assert [item["account_id"] for item in queue.json()] == [candidate]
    approved = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/teams/current/account-registrations/{candidate}:decide",
            json={"accept": True},
            headers=headers,
        )
    )
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    active = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": "alice", "password": "Member-Correct-Horse-42!"},
        )
    )
    assert active.status_code == 200

    members = asyncio.run(call(app, "GET", "/v1/teams/current/accounts", headers=headers))
    assert members.status_code == 200
    assert {item["username"] for item in members.json()} == {username, "alice"}


def test_rejected_registration_never_becomes_login_capable():
    app = stack()
    created = asyncio.run(
        call(app, "POST", "/v1/teams/register", json={"handle": "beta-team", "name": "Beta Team"})
    ).json()
    asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": created["administrator_username"],
                "current_password": created["administrator_initial_password"],
                "new_password": "Admin-Correct-Horse-42!",
            },
        )
    )
    admin = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={
                "login": created["administrator_username"],
                "password": "Admin-Correct-Horse-42!",
            },
        )
    ).json()
    pending = asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/register",
            json={
                "username": "mallory",
                "display_name": "Mallory",
                "email": "mallory@example.test",
                "password": "Member-Correct-Horse-42!",
                "team_id": created["team"]["team_id"],
            },
        )
    ).json()
    response = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/teams/current/account-registrations/{pending['account_id']}:decide",
            json={"accept": False},
            headers={"Authorization": f"Bearer {admin['access_token']}"},
        )
    )
    assert response.json()["status"] == "rejected"
    login = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": "mallory", "password": "Member-Correct-Horse-42!"},
        )
    )
    assert login.status_code == 401


def team_admin(app, handle):
    created = asyncio.run(
        call(app, "POST", "/v1/teams/register", json={"handle": handle, "name": handle.title()})
    ).json()
    password = "Admin-Correct-Horse-42!"
    asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": created["administrator_username"],
                "current_password": created["administrator_initial_password"],
                "new_password": password,
            },
        )
    )
    session = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": created["administrator_username"], "password": password},
        )
    ).json()
    return created["team"], {"Authorization": f"Bearer {session['access_token']}"}


def test_team_directory_search_pagination_and_relationship_projection():
    app = stack()
    alpha, alpha_headers = team_admin(app, "directory-alpha")
    beta, beta_headers = team_admin(app, "directory-beta")
    gamma, _ = team_admin(app, "directory-gamma")

    first = asyncio.run(call(app, "GET", "/v1/team-directory?limit=1", headers=alpha_headers))
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 1
    assert first.json()["items"][0]["team"]["team_id"] != alpha["team_id"]
    cursor = first.json()["next_after_handle"]
    assert cursor is not None
    second = asyncio.run(
        call(app, "GET", f"/v1/team-directory?limit=1&after_handle={cursor}", headers=alpha_headers)
    )
    assert second.status_code == 200
    assert (
        second.json()["items"][0]["team"]["team_id"] != first.json()["items"][0]["team"]["team_id"]
    )

    found = asyncio.run(call(app, "GET", "/v1/team-directory?q=beta", headers=alpha_headers)).json()
    assert found["items"] == [{"team": beta, "relationship": "available"}]
    assert (
        asyncio.run(call(app, "GET", "/v1/team-directory?q=%25", headers=alpha_headers)).json()[
            "items"
        ]
        == []
    )

    relation = asyncio.run(
        call(
            app,
            "POST",
            "/v1/team-relations/requests",
            headers=alpha_headers,
            json={"recipient_team_handle": beta["handle"], "message": "建立协作"},
        )
    ).json()
    outgoing = asyncio.run(
        call(app, "GET", "/v1/team-directory?q=beta", headers=alpha_headers)
    ).json()["items"][0]
    incoming = asyncio.run(
        call(app, "GET", "/v1/team-directory?q=alpha", headers=beta_headers)
    ).json()["items"][0]
    assert outgoing["relationship"] == "pending_outgoing"
    assert incoming["relationship"] == "pending_incoming"
    assert "accounts" not in str(outgoing).lower()
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/team-relations/requests/{relation['request_id']}:decide",
            headers=beta_headers,
            json={"accept": True},
        )
    )
    connected = asyncio.run(
        call(app, "GET", "/v1/team-directory?q=beta", headers=alpha_headers)
    ).json()["items"][0]
    assert connected["relationship"] == "connected"
    assert gamma["team_id"] != beta["team_id"]


def test_team_relation_and_project_composition_are_team_scoped():
    app = stack()
    alpha, alpha_headers = team_admin(app, "alpha-team")
    beta, beta_headers = team_admin(app, "beta-team")
    relation = asyncio.run(
        call(
            app,
            "POST",
            "/v1/team-relations/requests",
            headers=alpha_headers,
            json={"recipient_team_handle": beta["handle"], "message": "共同交付项目"},
        )
    )
    assert relation.status_code == 201, relation.text
    incoming = asyncio.run(
        call(app, "GET", "/v1/team-relations/requests", headers=beta_headers)
    ).json()
    assert incoming[0]["sender_team_id"] == alpha["team_id"]
    accepted = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/team-relations/requests/{relation.json()['request_id']}:decide",
            headers=beta_headers,
            json={"accept": True},
        )
    )
    assert accepted.json()["status"] == "accepted"
    related = asyncio.run(call(app, "GET", "/v1/team-relations", headers=alpha_headers)).json()
    assert [item["team"]["team_id"] for item in related] == [beta["team_id"]]

    project = asyncio.run(
        call(
            app,
            "POST",
            "/v1/projects",
            headers=alpha_headers,
            json={
                "name": "Release A",
                "description": "跨团队发布",
                "owner_assignment_name": "产品团队",
                "owner_kind": "product",
            },
        )
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    added = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/teams",
            headers=alpha_headers,
            json={"team_id": beta["team_id"], "assignment_name": "工程团队", "kind": "engineering"},
        )
    )
    assert added.status_code == 201, added.text
    beta_projects = asyncio.run(call(app, "GET", "/v1/projects", headers=beta_headers)).json()
    assert [item["project_id"] for item in beta_projects] == [project_id]
    detail = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}", headers=beta_headers)
    ).json()
    assert {team["team_id"] for team in detail["teams"]} == {alpha["team_id"], beta["team_id"]}

    message = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/messages",
            headers=alpha_headers,
            json={"target_team_id": beta["team_id"], "content": "请工程团队确认接口计划"},
        )
    )
    assert message.status_code == 201, message.text
    task = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/tasks",
            headers=alpha_headers,
            json={
                "target_team_id": beta["team_id"],
                "title": "实现接口",
                "description": "完成服务接口",
                "acceptance_criteria": "接口测试全部通过",
            },
        )
    )
    assert task.status_code == 201, task.text
    accepted = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/tasks/{task.json()['task_id']}:respond",
            headers=beta_headers,
            json={"accept": True},
        )
    )
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    inbox = asyncio.run(call(app, "GET", "/v1/collaboration-inbox", headers=beta_headers))
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["action_count"] == 1
    assert inbox.json()["actions"][0]["action"] == "assign"
    assert inbox.json()["actions"][0]["project_id"] == project_id
    assert "created_by" not in inbox.json()["actions"][0]["task"]
    assert "assigned_account_id" not in inbox.json()["actions"][0]["task"]
    assert "actor_account_id" not in inbox.json()["unread_activities"][0]
    assert inbox.json()["unread_count"] >= 2
    brief = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/agent-brief", headers=alpha_headers)
    )
    assert brief.status_code == 200
    assert "实现接口" in brief.json()["brief"]

    beta_account = asyncio.run(call(app, "GET", "/v1/accounts/me", headers=beta_headers)).json()
    app.state.team_collaboration_service.bind_inbox_agent_run(
        run_id="run-inbox-api",
        actor_id=beta_account["account_id"],
        mode="prioritization",
    )
    beta_inbox_runs = asyncio.run(
        call(app, "GET", "/v1/collaboration-inbox/agent-runs", headers=beta_headers)
    )
    assert beta_inbox_runs.status_code == 200
    assert beta_inbox_runs.json()[0]["run_id"] == "run-inbox-api"
    assert beta_inbox_runs.json()[0]["mode"] == "prioritization"
    alpha_inbox_runs = asyncio.run(
        call(app, "GET", "/v1/collaboration-inbox/agent-runs", headers=alpha_headers)
    )
    assert alpha_inbox_runs.status_code == 200
    assert alpha_inbox_runs.json() == []

    alpha_members = asyncio.run(
        call(app, "GET", "/v1/teams/current/accounts", headers=alpha_headers)
    ).json()
    beta_members = asyncio.run(
        call(app, "GET", "/v1/teams/current/accounts", headers=beta_headers)
    ).json()
    assert {item["account_id"] for item in alpha_members}.isdisjoint(
        {item["account_id"] for item in beta_members}
    )

    beta_notifications = asyncio.run(
        call(app, "GET", "/v1/project-notifications", headers=beta_headers)
    )
    assert beta_notifications.status_code == 200
    assert beta_notifications.json()[0]["unread_count"] >= 2
    marked = asyncio.run(
        call(
            app, "POST", f"/v1/projects/{project_id}/notifications:mark-read", headers=beta_headers
        )
    )
    assert marked.status_code == 200 and marked.json()["unread_count"] == 0
    read_inbox = asyncio.run(
        call(app, "GET", "/v1/collaboration-inbox", headers=beta_headers)
    ).json()
    assert read_inbox["unread_count"] == 0
    assert read_inbox["action_count"] == 1

    topic = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/topics",
            headers=beta_headers,
            json={"title": "交付节奏", "context": "建议拆分两个迭代"},
        )
    )
    assert topic.status_code == 201, topic.text
    assert topic.json()["proposed_by_team_id"] == beta["team_id"]
    topic_id = topic.json()["topic_id"]
    contribution = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/topics/{topic_id}/contributions",
            headers=alpha_headers,
            json={"content": "产品团队建议第一阶段优先打通接口"},
        )
    )
    assert contribution.status_code == 201, contribution.text
    forbidden = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/topics/{topic_id}:decide",
            headers=beta_headers,
            json={"decision": "直接执行"},
        )
    )
    assert forbidden.status_code == 403
    decided = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/topics/{topic_id}:decide",
            headers=alpha_headers,
            json={"decision": "分两阶段交付，第一阶段完成接口"},
        )
    )
    assert decided.status_code == 200 and decided.json()["status"] == "decided"
    listed = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/topics", headers=beta_headers)
    )
    assert listed.json()[0]["decision"].startswith("分两阶段")


def test_agent_attributed_topic_requires_a_readable_real_agent_run():
    app = stack()
    alpha, headers = team_admin(app, "agent-topic-team")
    project = asyncio.run(
        call(
            app,
            "POST",
            "/v1/projects",
            headers=headers,
            json={
                "name": "Agent Project",
                "description": "",
                "owner_assignment_name": "主导团队",
                "owner_kind": "product",
            },
        )
    ).json()
    response = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project['project_id']}/topics",
            headers=headers,
            json={
                "title": "Agent 建议",
                "context": "采用两个里程碑",
                "source_agent_run_id": "missing-run",
            },
        )
    )
    assert response.status_code == 503
    assert "Agent run service" in response.text


def test_completed_project_agent_output_imports_reviewable_draft_before_execution():
    agent = CompletedActionAgent()
    app = stack(agent_run_service=agent)
    alpha, alpha_headers = team_admin(app, "draft-alpha")
    beta, beta_headers = team_admin(app, "draft-beta")
    relation = asyncio.run(
        call(
            app,
            "POST",
            "/v1/team-relations/requests",
            headers=alpha_headers,
            json={"recipient_team_handle": beta["handle"], "message": ""},
        )
    ).json()
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/team-relations/requests/{relation['request_id']}:decide",
            headers=beta_headers,
            json={"accept": True},
        )
    )
    project = asyncio.run(
        call(
            app,
            "POST",
            "/v1/projects",
            headers=alpha_headers,
            json={
                "name": "Draft Project",
                "description": "",
                "owner_assignment_name": "主导团队",
                "owner_kind": "product",
            },
        )
    ).json()
    project_id = project["project_id"]
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/teams",
            headers=alpha_headers,
            json={"team_id": beta["team_id"], "assignment_name": "工程团队", "kind": "engineering"},
        )
    )
    account = asyncio.run(call(app, "GET", "/v1/accounts/me", headers=alpha_headers)).json()
    app.state.team_collaboration_service.bind_project_agent_run(
        project_id=project_id,
        run_id="run-draft-api",
        actor_id=account["account_id"],
        mode="collaboration_actions",
    )
    project_runs = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/agent-runs", headers=alpha_headers)
    )
    assert project_runs.status_code == 200
    assert project_runs.json()[0]["mode"] == "collaboration_actions"
    agent.target_team_id = beta["team_id"]
    imported = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/agent-runs/run-draft-api:import-drafts",
            headers=alpha_headers,
            json={},
        )
    )
    assert imported.status_code == 201, imported.text
    draft = imported.json()[0]
    assert draft["status"] == "pending"
    assert (
        asyncio.run(
            call(app, "GET", f"/v1/projects/{project_id}/messages", headers=beta_headers)
        ).json()
        == []
    )
    executed = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/collaboration-drafts/{draft['draft_id']}:execute",
            headers=alpha_headers,
            json={"expected_version": draft["version"]},
        )
    )
    assert executed.status_code == 200 and executed.json()["status"] == "executed"
    messages = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/messages", headers=beta_headers)
    ).json()
    assert messages[0]["content"] == "请确认接口排期"
