"""Team Agent exchanges: drafts, two-phase publication, per-team context."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.product import (
    DataPropagation,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
    TeamCollaborationService,
)
from coifesp_harness.product.exchange import AgentExchangeService
from coifesp_harness.product.repository import ACCOUNT_SESSIONS, PROJECT_RESOURCES
from coifesp_harness.product.workspace import ProjectWorkspaceService

from test_project_conversations import seed as seed_conversations


def seed(*, with_engine=None):
    base = seed_conversations(with_engine=with_engine)
    exchange = AgentExchangeService(base["engine"])
    base["exchange"] = exchange
    add_resource(
        base["engine"],
        resource_id="resource-product-private",
        project_id="project-demo",
        owner_team_id="team-product",
        created_by="lead-lin",
        propagation=DataPropagation.TEAM_PRIVATE,
    )
    add_resource(
        base["engine"],
        resource_id="resource-shared",
        project_id="project-demo",
        owner_team_id="team-product",
        created_by="lead-lin",
        propagation=DataPropagation.PROJECT_READONLY,
    )
    add_resource(
        base["engine"],
        resource_id="resource-engineering-private",
        project_id="project-demo",
        owner_team_id="team-engineering",
        created_by="contributor-zhou",
        propagation=DataPropagation.TEAM_PRIVATE,
    )
    return base


def add_resource(engine, *, resource_id, project_id, owner_team_id, created_by, propagation):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(PROJECT_RESOURCES).values(
                resource_id=resource_id,
                project_id=project_id,
                owner_team_id=owner_team_id,
                created_by=created_by,
                title=resource_id,
                artifact_owner_team_id=owner_team_id,
                artifact_id=f"artifact-{resource_id}",
                artifact_sha256="a" * 64,
                media_type="text/plain",
                propagation=propagation.value,
                created_at=now,
            )
        )


def test_create_and_update_draft():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="确认接口排期",
        summary="产品侧希望确认交付时间",
        request="请工程团队确认是否可以在 8 月底前完成接口联调",
        constraints="不包含私有资源",
        recipient_team_ids=("team-engineering",),
        source_conversation_id="conv-1",
        source_turn_id="turn-1",
    )
    assert draft.status.value == "drafting"
    assert len(draft.content_sha256) == 64
    updated = exchange.update_draft(
        project_id="project-demo",
        draft_id="draft-1",
        actor_id="lead-lin",
        expected_version=1,
        purpose="确认接口排期",
        summary="产品侧希望确认交付时间（更新）",
        request="请工程团队确认接口联调时间",
        recipient_team_ids=("team-engineering",),
    )
    assert updated.version == 2
    assert "更新" in updated.summary


def test_approval_rejects_team_private_references():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-private",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="共享资源",
        summary="引用产品私有文件",
        request="请参考该文件",
        shared_resource_ids=("resource-product-private",),
        recipient_team_ids=("team-engineering",),
    )
    try:
        exchange.approve_draft(
            project_id="project-demo",
            draft_id="draft-private",
            actor_id="lead-lin",
            expected_version=1,
        )
        raise AssertionError("expected team-private references to block approval")
    except GovernanceConflictError:
        pass


def test_approval_creates_immutable_exchange_with_per_team_snapshots():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-ok",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="排期确认",
        summary="请求工程与质量确认排期",
        request="请确认排期与验收标准",
        shared_resource_ids=("resource-shared",),
        recipient_team_ids=("team-engineering", "team-quality"),
    )
    result = exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-ok",
        actor_id="lead-lin",
        expected_version=1,
    )
    assert result.status.value == "sent"
    assert result.approved_by == "lead-lin"
    # Draft is now immutable
    try:
        exchange.update_draft(
            project_id="project-demo",
            draft_id="draft-ok",
            actor_id="lead-lin",
            expected_version=1,
            purpose="排期确认",
            summary="修改已批准的草案",
            request="修改",
            recipient_team_ids=("team-engineering",),
        )
        raise AssertionError("expected approved draft to be immutable")
    except GovernanceConflictError:
        pass

    recipients = exchange.list_recipients(
        project_id="project-demo", exchange_id=result.exchange_id, actor_id="lead-lin"
    )
    assert {item.recipient_team_id for item in recipients} == {
        "team-engineering",
        "team-quality",
    }
    # Per-team snapshots: engineering sees shared + its own private, never
    # the product team's private resource.
    by_team = {item.recipient_team_id: item.context_snapshot for item in recipients}
    engineering = by_team["team-engineering"]
    assert "resource-shared" in engineering["shared_resource_ids"]
    assert "resource-engineering-private" in engineering["own_team_private_resource_ids"]
    assert "resource-product-private" not in engineering["shared_resource_ids"]
    assert "resource-product-private" not in engineering["own_team_private_resource_ids"]


def test_recipient_responds_and_exchange_aggregates():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-respond",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="排期确认",
        summary="请求确认",
        request="请工程确认排期",
        recipient_team_ids=("team-engineering",),
    )
    result = exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-respond",
        actor_id="lead-lin",
        expected_version=1,
    )
    response = exchange.submit_response(
        project_id="project-demo",
        exchange_id=result.exchange_id,
        actor_id="contributor-zhou",
        content="工程侧可以在 8 月底前完成，替代方案是分两期交付。",
        turn_id="turn-eng",
    )
    assert response.recipient_team_id == "team-engineering"
    assert len(response.content_sha256) == 64
    latest = exchange.get_exchange(
        project_id="project-demo", exchange_id=result.exchange_id, actor_id="lead-lin"
    )
    assert latest.status.value == "responded"
    responses = exchange.list_responses(
        project_id="project-demo", exchange_id=result.exchange_id, actor_id="lead-lin"
    )
    assert [item.content_sha256 for item in responses] == [response.content_sha256]


def test_team_cannot_respond_twice_or_reply_to_unaddressed_exchange():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-once",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="排期确认",
        summary="请求确认",
        request="请质量确认验收标准",
        recipient_team_ids=("team-quality",),
    )
    result = exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-once",
        actor_id="lead-lin",
        expected_version=1,
    )
    exchange.submit_response(
        project_id="project-demo",
        exchange_id=result.exchange_id,
        actor_id="reviewer-su",
        content="验收标准确认。",
    )
    try:
        exchange.submit_response(
            project_id="project-demo",
            exchange_id=result.exchange_id,
            actor_id="reviewer-su",
            content="重复回复。",
        )
        raise AssertionError("expected duplicate response to be rejected")
    except GovernanceConflictError:
        pass
    # engineering is not addressed by this exchange
    try:
        exchange.submit_response(
            project_id="project-demo",
            exchange_id=result.exchange_id,
            actor_id="contributor-zhou",
            content="不是发给工程的。",
        )
        raise AssertionError("expected unaddressed team response to be rejected")
    except ResourceNotFound:
        pass


def test_source_team_cannot_send_to_itself():
    s = seed()
    exchange = s["exchange"]
    try:
        exchange.create_draft(
            draft_id="draft-self",
            project_id="project-demo",
            actor_id="lead-lin",
            purpose="自问自答",
            summary="自问自答",
            request="发给自己的消息",
            recipient_team_ids=("team-product",),
        )
        raise AssertionError("expected self-addressed exchange to be rejected")
    except GovernanceConflictError:
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
    exchange = AgentExchangeService(engine)
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=directory,
        team_collaboration_service=collaboration,
        project_workspace_service=workspace,
        agent_exchange_service=exchange,
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


def test_api_exchange_flow():
    app, engine = _stack()
    seed(with_engine=engine)
    add_resource(
        engine,
        resource_id="resource-api-shared",
        project_id="project-demo",
        owner_team_id="team-product",
        created_by="lead-lin",
        propagation=DataPropagation.PROJECT_READONLY,
    )
    lead_token = _issue_token(engine, "lead-lin", "lead-token")
    eng_token = _issue_token(engine, "contributor-zhou", "eng-token")

    created = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects/project-demo/agent-exchange-drafts",
            token=lead_token,
            json={
                "purpose": "排期确认",
                "summary": "请工程确认接口排期",
                "request": "是否可以在 8 月底前完成联调",
                "shared_resource_ids": ["resource-api-shared"],
                "recipient_team_ids": ["team-engineering"],
            },
        )
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "drafting"

    approved = asyncio.run(
        _call(
            app,
            "POST",
            f"/v1/projects/project-demo/agent-exchange-drafts/{draft['draft_id']}:approve",
            token=lead_token,
            json={"expected_version": 1},
        )
    )
    assert approved.status_code == 200, approved.text
    exchange_id = approved.json()["exchange_id"]

    responded = asyncio.run(
        _call(
            app,
            "POST",
            f"/v1/projects/project-demo/agent-exchanges/{exchange_id}/responses",
            token=eng_token,
            json={"content": "工程侧确认 8 月底前完成联调。"},
        )
    )
    assert responded.status_code == 201, responded.text

    listed = asyncio.run(
        _call(
            app,
            "GET",
            "/v1/projects/project-demo/agent-exchanges",
            token=lead_token,
        )
    )
    assert listed.status_code == 200
    assert [item["exchange_id"] for item in listed.json()] == [exchange_id]
    listed_body = listed.json()[0]
    assert listed_body["status"] == "responded"


def test_third_party_team_cannot_read_exchange_or_responses():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-isol",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="工程排期",
        summary="请求工程确认",
        request="请工程确认排期",
        recipient_team_ids=("team-engineering",),
    )
    result = exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-isol",
        actor_id="lead-lin",
        expected_version=1,
    )
    exchange_id = result.exchange_id
    # quality is not involved in this exchange
    for call in (
        lambda: exchange.get_exchange(project_id="project-demo", exchange_id=exchange_id, actor_id="reviewer-su"),
        lambda: exchange.list_recipients(project_id="project-demo", exchange_id=exchange_id, actor_id="reviewer-su"),
        lambda: exchange.list_responses(project_id="project-demo", exchange_id=exchange_id, actor_id="reviewer-su"),
    ):
        try:
            call()
            raise AssertionError("expected un-involved team access to be rejected")
        except ResourceNotFound:
            pass


def test_recipient_only_sees_own_snapshot_and_response():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-snap",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="排期确认",
        summary="请求两团队确认",
        request="确认排期与验收",
        recipient_team_ids=("team-engineering", "team-quality"),
    )
    result = exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-snap",
        actor_id="lead-lin",
        expected_version=1,
    )
    exchange_id = result.exchange_id
    exchange.submit_response(
        project_id="project-demo",
        exchange_id=exchange_id,
        actor_id="contributor-zhou",
        content="工程回复。",
    )
    # engineering sees only its own recipient row and own responses
    engineering_recipients = exchange.list_recipients(
        project_id="project-demo", exchange_id=exchange_id, actor_id="contributor-zhou"
    )
    assert [item.recipient_team_id for item in engineering_recipients] == ["team-engineering"]
    engineering_responses = exchange.list_responses(
        project_id="project-demo", exchange_id=exchange_id, actor_id="contributor-zhou"
    )
    assert [item.recipient_team_id for item in engineering_responses] == ["team-engineering"]
    # the source team still sees both recipient rows
    source_recipients = exchange.list_recipients(
        project_id="project-demo", exchange_id=exchange_id, actor_id="lead-lin"
    )
    assert {item.recipient_team_id for item in source_recipients} == {
        "team-engineering",
        "team-quality",
    }
