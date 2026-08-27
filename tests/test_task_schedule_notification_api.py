import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.product import (
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    TeamCollaborationService,
)


def stack():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    notifications = NotificationService(engine)
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=ProjectDirectoryService(engine),
        team_collaboration_service=TeamCollaborationService(engine, notifier=notifications),
        notification_service=notifications,
    )
    return app


async def call(app, method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def login_team(app, handle="alpha-team", name="Alpha"):
    team = asyncio.run(
        call(app, "POST", "/v1/teams/register", json={"handle": handle, "name": name})
    )
    assert team.status_code == 201, team.text
    details = team.json()
    asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": details["administrator_username"],
                "current_password": details["administrator_initial_password"],
                "new_password": "Admin-Correct-Horse-42!",
            },
        )
    )
    session = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={
                "login": details["administrator_username"],
                "password": "Admin-Correct-Horse-42!",
            },
        )
    )
    assert session.status_code == 200, session.text
    return details["team"]["team_id"], session.json()["access_token"]


def test_cross_team_schedule_and_notification_api_flow():
    app = stack()
    team_a, token_a = login_team(app, "alpha-team", "Alpha")
    team_b, token_b = login_team(app, "beta-team", "Beta")

    request = asyncio.run(
        call(
            app,
            "POST",
            "/v1/team-relations/requests",
            token_a,
            json={"recipient_team_handle": "beta-team", "message": "建立合作"},
        )
    )
    assert request.status_code == 201, request.text
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/team-relations/requests/{request.json()['request_id']}:decide",
            token_b,
            json={"accept": True},
        )
    )
    project = asyncio.run(
        call(
            app,
            "POST",
            "/v1/projects",
            token_a,
            json={
                "name": "平台联调",
                "description": "",
                "owner_assignment_name": "产品",
                "owner_kind": "product",
            },
        )
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/teams",
            token_a,
            json={"team_id": team_b, "assignment_name": "工程", "kind": "engineering"},
        )
    )

    due_soon = datetime.now(UTC) + timedelta(hours=6)
    task = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/tasks",
            token_a,
            json={
                "target_team_id": team_b,
                "title": "接口适配交付",
                "description": "",
                "acceptance_criteria": "联调通过",
                "priority": "urgent",
                "due_at": due_soon.isoformat(),
            },
        )
    )
    assert task.status_code == 201, task.text
    body = task.json()
    assert body["priority"] == "urgent"
    assert body["due_at"] is not None
    assert body["schedule_version"] == 1
    assert body["is_due_soon"] is True and body["is_overdue"] is False
    task_id = body["task_id"]

    inbox = asyncio.run(
        call(
            app,
            "GET",
            "/v1/collaboration-inbox?due_within_hours=48",
            token_b,
        )
    )
    assert inbox.status_code == 200, inbox.text
    assert [item["task"]["task_id"] for item in inbox.json()["actions"]] == [task_id]
    assert inbox.json()["actions"][0]["task"]["priority"] == "urgent"

    overdue = asyncio.run(
        call(app, "GET", "/v1/collaboration-inbox?overdue_only=true", token_b)
    )
    assert overdue.json()["actions"] == []

    # Source team directly edits the schedule before acceptance.
    later = datetime.now(UTC) + timedelta(days=14)
    direct = asyncio.run(
        call(
            app,
            "PATCH",
            f"/v1/projects/{project_id}/tasks/{task_id}/schedule",
            token_a,
            json={
                "priority": "urgent",
                "due_at": later.isoformat(),
                "clear_due_at": False,
                "expected_schedule_version": 1,
                "reason": "联调窗口后移",
            },
        )
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["result"] == "updated"
    assert direct.json()["task"]["schedule_version"] == 2

    # Stale replay conflicts with 409.
    stale = asyncio.run(
        call(
            app,
            "PATCH",
            f"/v1/projects/{project_id}/tasks/{task_id}/schedule",
            token_a,
            json={
                "priority": "low",
                "due_at": None,
                "clear_due_at": False,
                "expected_schedule_version": 1,
                "reason": "",
            },
        )
    )
    assert stale.status_code == 409

    # Target team accepts, then a deadline shortening needs a reason.
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/tasks/{task_id}:respond",
            token_b,
            json={"accept": True},
        )
    )
    missing_reason = asyncio.run(
        call(
            app,
            "PATCH",
            f"/v1/projects/{project_id}/tasks/{task_id}/schedule",
            token_a,
            json={
                "priority": "urgent",
                "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                "clear_due_at": False,
                "expected_schedule_version": 2,
                "reason": "",
            },
        )
    )
    assert missing_reason.status_code == 422, missing_reason.text

    proposal_response = asyncio.run(
        call(
            app,
            "PATCH",
            f"/v1/projects/{project_id}/tasks/{task_id}/schedule",
            token_a,
            json={
                "priority": "urgent",
                "due_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                "clear_due_at": False,
                "expected_schedule_version": 2,
                "reason": "外部验收提前",
            },
        )
    )
    assert proposal_response.status_code == 200, proposal_response.text
    assert proposal_response.json()["result"] == "proposed"
    proposal = proposal_response.json()["proposal"]
    assert proposal["status"] == "pending" and proposal["version"] == 1

    proposals = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/tasks/{task_id}/schedule-proposals",
            token_b,
        )
    )
    assert proposals.status_code == 200
    assert proposals.json()[0]["proposal_id"] == proposal["proposal_id"]

    decide = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/tasks/{task_id}/schedule-proposals/"
            f"{proposal['proposal_id']}:decide",
            token_b,
            json={
                "accept": True,
                "reason": "同意提前",
                "expected_proposal_version": 1,
            },
        )
    )
    assert decide.status_code == 200, decide.text
    assert decide.json()["status"] == "accepted"

    tasks_after = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/tasks", token_a)
    )
    scheduled = tasks_after.json()[0]
    assert scheduled["schedule_version"] == 3
    assert scheduled["due_changed_by"] is not None

    activities = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/activities", token_b)
    )
    event_types = [item["event_type"] for item in activities.json()]
    assert "task_schedule_set" in event_types
    assert "task_schedule_proposed" in event_types
    assert "task_schedule_changed" in event_types

    # Both sides received notifications from the schedule negotiation.
    for token in (token_a, token_b):
        page = asyncio.run(call(app, "GET", "/v1/notifications?limit=50", token))
        assert page.status_code == 200, page.text
        categories = {item["category"] for item in page.json()["items"]}
        assert "task" in categories

    beta_page = asyncio.run(call(app, "GET", "/v1/notifications?limit=50", token_b))
    unread_before = beta_page.json()["unread_count"]
    assert unread_before > 0
    first_id = beta_page.json()["items"][0]["notification_id"]
    read = asyncio.run(
        call(app, "POST", f"/v1/notifications/{first_id}:mark-read", token_b)
    )
    assert read.status_code == 200 and read.json()["read_at"] is not None
    archived = asyncio.run(
        call(app, "POST", f"/v1/notifications/{first_id}:archive", token_b)
    )
    assert archived.json()["archived_at"] is not None
    restored = asyncio.run(
        call(app, "POST", f"/v1/notifications/{first_id}:restore", token_b)
    )
    assert restored.json()["archived_at"] is None
    page_read = asyncio.run(
        call(
            app,
            "POST",
            "/v1/notifications:mark-page-read",
            token_b,
            json={"notification_ids": [first_id]},
        )
    )
    assert page_read.status_code == 200

    preferences = asyncio.run(
        call(app, "GET", "/v1/notification-preferences", token_b)
    )
    assert preferences.status_code == 200
    assert preferences.json()["notify_tasks"] is True
    saved = asyncio.run(
        call(
            app,
            "PUT",
            "/v1/notification-preferences",
            token_b,
            json={
                "notify_tasks": True,
                "notify_messages": False,
                "notify_resources": True,
                "notify_topics": True,
                "notify_agent_events": True,
                "notify_due_soon": True,
                "notify_overdue": True,
                "due_soon_hours": 24,
                "time_zone": "Asia/Shanghai",
                "quiet_start_minute": 22 * 60,
                "quiet_end_minute": 7 * 60,
            },
        )
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["time_zone"] == "Asia/Shanghai"
    assert saved.json()["quiet_now"] in (True, False)
    invalid_zone = asyncio.run(
        call(
            app,
            "PUT",
            "/v1/notification-preferences",
            token_b,
            json={
                "notify_tasks": True,
                "notify_messages": True,
                "notify_resources": True,
                "notify_topics": True,
                "notify_agent_events": True,
                "notify_due_soon": True,
                "notify_overdue": True,
                "due_soon_hours": 48,
                "time_zone": "Mars/Olympus_Mons",
                "quiet_start_minute": None,
                "quiet_end_minute": None,
            },
        )
    )
    assert invalid_zone.status_code == 422

    # Cross-account isolation: alpha cannot touch beta notifications.
    foreign = asyncio.run(
        call(app, "POST", f"/v1/notifications/{first_id}:mark-read", token_a)
    )
    assert foreign.status_code == 404
