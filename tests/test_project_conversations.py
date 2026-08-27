"""Persistent project conversations, team project agents and agent turns."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
    TeamCollaborationService,
)
from coifesp_harness.product.models import TurnStatus
from coifesp_harness.product.repository import ACCOUNT_SESSIONS
from coifesp_harness.product.workspace import ProjectWorkspaceService


def seed(*, with_engine=None):
    engine = with_engine or create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    collaboration = TeamCollaborationService(engine)
    workspace = ProjectWorkspaceService(engine)

    _, product_admin = accounts.register_team(
        team_id="team-product", team_handle="product", team_name="产品团队"
    )
    _, eng_admin = accounts.register_team(
        team_id="team-engineering", team_handle="engineering", team_name="工程团队"
    )
    _, quality_admin = accounts.register_team(
        team_id="team-quality", team_handle="quality", team_name="质量团队"
    )
    lead = accounts.ensure_active_account(
        account_id="lead-lin",
        username="lead-lin",
        display_name="林澈",
        email="lead@demo.invalid",
        team_id="team-product",
        team_role=TeamAccountRole.ADMIN,
    )
    zhou = accounts.ensure_active_account(
        account_id="contributor-zhou",
        username="contributor-zhou",
        display_name="周宁",
        email="zhou@demo.invalid",
        team_id="team-engineering",
    )
    su = accounts.ensure_active_account(
        account_id="reviewer-su",
        username="reviewer-su",
        display_name="苏禾",
        email="su@demo.invalid",
        team_id="team-quality",
    )
    rel = accounts.send_team_relation_request(
        request_id="rel-product-engineering",
        actor_id=lead.account_id,
        recipient_team_handle="engineering",
        message="合作",
    )
    accounts.decide_team_relation_request(
        request_id=rel.request_id, actor_id=eng_admin.account.account_id, accept=True
    )
    rel2 = accounts.send_team_relation_request(
        request_id="rel-product-quality",
        actor_id=lead.account_id,
        recipient_team_handle="quality",
        message="合作",
    )
    accounts.decide_team_relation_request(
        request_id=rel2.request_id, actor_id=quality_admin.account.account_id, accept=True
    )
    project = directory.create_project(
        project_id="project-demo",
        name="演示项目",
        description="多团队演示",
        actor_id=lead.account_id,
        owner_assignment_name="产品统筹",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    directory.add_team(
        project_id=project.project_id,
        team_id="team-engineering",
        name="工程交付",
        kind=ProjectTeamKind.ENGINEERING,
        actor_id=lead.account_id,
    )
    directory.add_team(
        project_id=project.project_id,
        team_id="team-quality",
        name="质量评审",
        kind=ProjectTeamKind.QUALITY,
        actor_id=lead.account_id,
    )
    return {
        "engine": engine,
        "accounts": accounts,
        "directory": directory,
        "collaboration": collaboration,
        "workspace": workspace,
        "lead": lead,
        "zhou": zhou,
        "su": su,
        "admins": (product_admin, eng_admin, quality_admin),
    }


def test_ensure_conversation_is_unique_per_project_account():
    s = seed()
    workspace = s["workspace"]
    first = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    second = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    assert first.conversation_id == second.conversation_id
    # a different account in the same project gets its own conversation
    other = workspace.ensure_conversation(project_id="project-demo", actor_id="contributor-zhou")
    assert other.conversation_id != first.conversation_id


def test_team_project_agent_is_unique_per_team():
    s = seed()
    workspace = s["workspace"]
    agent = workspace.ensure_team_project_agent(
        project_id="project-demo", team_id="team-product"
    )
    again = workspace.ensure_team_project_agent(
        project_id="project-demo", team_id="team-product"
    )
    assert agent.agent_id == again.agent_id


def test_concurrent_ensure_returns_same_conversation():
    # Two independent connections over the same shared in-memory database
    # simulate concurrent first-open; the unique (project_id, account_id)
    # constraint must converge every caller on one conversation.
    url = "sqlite+pysqlite:///file:sharedmem_concurrent?mode=memory&cache=shared&uri=true"
    engine_a = create_engine(
        url, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    engine_b = create_engine(
        url, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    s = seed(with_engine=engine_a)
    workspace_b = ProjectWorkspaceService(engine_b)
    first = s["workspace"].ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    second = workspace_b.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    assert first.conversation_id == second.conversation_id
    third = workspace_b.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    assert third.conversation_id == first.conversation_id


def test_append_message_creates_turn_and_increments_sequence():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请分析当前项目风险",
        idempotency_key="msg-1",
        expected_last_sequence=0,
    )
    assert message.sequence == 1
    assert message.role == "user"
    assert turn.user_message_sequence == 1
    assert turn.status is TurnStatus.ACTIVE
    assert conversation.last_message_sequence == 0

    assistant = workspace.complete_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        assistant_content="初步风险分析如下……",
        run_id="run-1",
    )
    assert assistant.sequence == 2
    assert assistant.role == "assistant"
    assert assistant.turn_id == turn.turn_id
    done = workspace.active_turn(conversation_id=conversation.conversation_id)
    assert done is None

    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.sequence for item in messages] == [1, 2]


def test_idempotency_key_conflict():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="第一条消息",
        idempotency_key="same-key",
    )
    try:
        workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content="第二条消息",
            idempotency_key="same-key",
        )
        raise AssertionError("expected duplicate idempotency key to be rejected")
    except GovernanceConflictError:
        pass
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert len(messages) == 1


def test_single_active_turn_blocks_second_message():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="第一个请求",
        idempotency_key="msg-a",
    )
    try:
        workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content="第二个请求",
            idempotency_key="msg-b",
        )
        raise AssertionError("expected a second message while a turn is active to be rejected")
    except GovernanceConflictError:
        pass


def test_failed_turn_can_be_retried_without_losing_message():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="会被重试的消息",
        idempotency_key="retry-1",
    )
    failed = workspace.fail_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert failed.status is TurnStatus.FAILED
    # the user message is still there and a new turn can start
    retried, retry_turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="会被重试的消息",
        idempotency_key="retry-2",
    )
    assert retried.sequence == message.sequence + 1
    assert retry_turn.user_message_sequence == retried.sequence


def test_expected_last_sequence_optimistic_lock():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="已有消息",
        idempotency_key="lock-1",
    )
    try:
        workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content="过期客户端",
            idempotency_key="lock-2",
            expected_last_sequence=0,
        )
        raise AssertionError("expected stale expected_last_sequence to be rejected")
    except GovernanceConflictError:
        pass


def test_archive_blocks_writes_and_reactivate_restores():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    archived = workspace.archive_conversation(project_id="project-demo", actor_id="lead-lin")
    assert archived.status.value == "archived"
    try:
        workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content="归档后的消息",
            idempotency_key="archived-1",
        )
        raise AssertionError("expected writes to an archived conversation to be rejected")
    except GovernanceConflictError:
        pass
    restored = workspace.reactivate_conversation(project_id="project-demo", actor_id="lead-lin")
    assert restored.status.value == "active"
    message, _ = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="恢复后的消息",
        idempotency_key="restored-1",
    )
    assert message.sequence == 1


def test_resource_propagation_change_is_owner_scoped_and_optimistic():
    from coifesp_harness.product import DataPropagation
    from coifesp_harness.product.repository import PROJECT_RESOURCES

    s = seed()
    workspace = s["workspace"]
    engine = s["engine"]
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(PROJECT_RESOURCES).values(
                resource_id="resource-prop-1",
                project_id="project-demo",
                owner_team_id="team-product",
                created_by="lead-lin",
                title="待共享资料",
                artifact_owner_team_id="team-product",
                artifact_id="artifact-prop-1",
                artifact_sha256="b" * 64,
                media_type="text/plain",
                propagation=DataPropagation.TEAM_PRIVATE.value,
                created_at=now,
            )
        )
    workspace.update_resource_propagation(
        project_id="project-demo",
        resource_id="resource-prop-1",
        actor_id="lead-lin",
        requested_propagation=DataPropagation.PROJECT_READONLY,
        expected_propagation=DataPropagation.TEAM_PRIVATE,
    )
    # stale expected value is rejected
    try:
        workspace.update_resource_propagation(
            project_id="project-demo",
            resource_id="resource-prop-1",
            actor_id="lead-lin",
            requested_propagation=DataPropagation.TEAM_PRIVATE,
            expected_propagation=DataPropagation.TEAM_PRIVATE,
        )
        raise AssertionError("expected stale optimistic value to be rejected")
    except GovernanceConflictError:
        pass
    # only the owning team can change propagation
    try:
        workspace.update_resource_propagation(
            project_id="project-demo",
            resource_id="resource-prop-1",
            actor_id="contributor-zhou",
            requested_propagation=DataPropagation.TEAM_PRIVATE,
            expected_propagation=DataPropagation.PROJECT_READONLY,
        )
        raise AssertionError("expected cross-team propagation change to be rejected")
    except ResourceNotFound:
        pass


def test_resource_scope_filtering():
    from coifesp_harness.product import DataPropagation
    from coifesp_harness.product.repository import PROJECT_RESOURCES

    s = seed()
    workspace = s["workspace"]
    engine = s["engine"]
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for index, (resource_id, team, propagation) in enumerate(
            (
                ("scope-a", "team-product", DataPropagation.TEAM_PRIVATE),
                ("scope-b", "team-product", DataPropagation.PROJECT_READONLY),
                ("scope-c", "team-engineering", DataPropagation.TEAM_PRIVATE),
                ("scope-d", "team-engineering", DataPropagation.PORTABLE),
            )
        ):
            connection.execute(
                insert(PROJECT_RESOURCES).values(
                    resource_id=resource_id,
                    project_id="project-demo",
                    owner_team_id=team,
                    created_by="lead-lin" if team == "team-product" else "contributor-zhou",
                    title=resource_id,
                    artifact_owner_team_id=team,
                    artifact_id=f"artifact-{resource_id}",
                    artifact_sha256="c" * 64,
                    media_type="text/plain",
                    propagation=propagation.value,
                    created_at=now,
                )
            )
    private = workspace.list_project_resources(
        project_id="project-demo", actor_id="lead-lin", scope="team_private"
    )
    assert {item["resource_id"] for item in private} == {"scope-a"}
    shared = workspace.list_project_resources(
        project_id="project-demo", actor_id="lead-lin", scope="project_shared"
    )
    ids = {item["resource_id"] for item in shared}
    assert "scope-b" in ids and "scope-d" in ids and "scope-c" not in ids


def test_non_participant_cannot_ensure_or_attach():
    s = seed()
    workspace = s["workspace"]
    # register an unrelated team and account with no relation and no project seat
    s["accounts"].register_team(team_id="team-outsider", team_handle="outsider", team_name="外部团队")
    s["accounts"].ensure_active_account(
        account_id="outsider-one",
        username="outsider-one",
        display_name="外部用户",
        email="outsider@demo.invalid",
        team_id="team-outsider",
    )
    try:
        workspace.ensure_conversation(project_id="project-demo", actor_id="outsider-one")
        raise AssertionError("expected non-participant ensure to fail")
    except ResourceNotFound:
        pass


def test_other_team_cannot_read_private_conversation():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    try:
        workspace.list_messages(
            conversation_id=conversation.conversation_id, actor_id="contributor-zhou"
        )
        raise AssertionError("expected another account's conversation to be private")
    except ResourceNotFound:
        pass


def test_workspace_snapshot_aggregates_counts():
    s = seed()
    workspace = s["workspace"]
    workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    snapshot = workspace.workspace(project_id="project-demo", actor_id="lead-lin")
    assert snapshot.project.project_id == "project-demo"
    assert {team.team_id for team in snapshot.teams} == {
        "team-product",
        "team-engineering",
        "team-quality",
    }
    assert snapshot.conversation is not None


def test_local_demo_bootstrap_is_idempotent():
    from coifesp_harness.product.demo_bootstrap import ensure_local_demo

    s = seed()
    first = ensure_local_demo(engine=s["engine"])
    second = ensure_local_demo(engine=s["engine"])
    assert first.conversation_ids == second.conversation_ids
    assert first.project_id == "project-coifesp-demo"
    workspace = s["workspace"]
    for account_id in ("lead-lin", "contributor-zhou", "reviewer-su"):
        conversation = workspace.ensure_conversation(
            project_id=first.project_id, actor_id=account_id
        )
        assert conversation.conversation_id in first.conversation_ids


# ---------------------------------------------------------------------------
# HTTP API smoke tests
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
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=directory,
        team_collaboration_service=collaboration,
        project_workspace_service=workspace,
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


def test_api_project_creation_returns_unique_conversation():
    app, engine = _stack()
    seed(with_engine=engine)
    token = _issue_token(engine, "lead-lin")
    result = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects",
            token=token,
            json={
                "name": "API 项目",
                "description": "通过 API 创建",
                "owner_assignment_name": "产品统筹",
                "owner_kind": "product",
                "initial_brief": "目标：完成一个多团队演示。限制：不使用外部连接器。",
            },
        )
    )
    assert result.status_code == 201, result.text
    body = result.json()
    assert body["conversation_id"].startswith("conv-")
    project_id = body["project_id"]

    # PUT conversation is idempotent and returns the same conversation
    first = asyncio.run(
        _call(app, "PUT", f"/v1/projects/{project_id}/conversation", token=token)
    )
    second = asyncio.run(
        _call(app, "PUT", f"/v1/projects/{project_id}/conversation", token=token)
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["conversation_id"] == second.json()["conversation_id"]
    assert first.json()["conversation_id"] == body["conversation_id"]


def test_api_messages_round_trip():
    app, engine = _stack()
    s = seed(with_engine=engine)
    token = _issue_token(engine, "lead-lin")
    sent = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects/project-demo/conversation/messages",
            token=token,
            json={"content": "你好，Agent", "idempotency_key": "api-msg-1"},
        )
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()
    assert body["message"]["sequence"] == 1
    assert body["turn"]["status"] == "active"
    # run is None when no agent run service is wired
    assert body["run"] is None

    page = asyncio.run(
        _call(
            app,
            "GET",
            "/v1/projects/project-demo/conversation/messages",
            token=token,
        )
    )
    assert page.status_code == 200
    assert [item["sequence"] for item in page.json()["items"]] == [1]


def test_api_workspace_projects_and_snapshot():
    app, engine = _stack()
    seed(with_engine=engine)
    token = _issue_token(engine, "lead-lin")
    projects = asyncio.run(_call(app, "GET", "/v1/workspace/projects", token=token))
    assert projects.status_code == 200, projects.text
    assert any(item["project"]["project_id"] == "project-demo" for item in projects.json())

    snapshot = asyncio.run(
        _call(app, "GET", "/v1/projects/project-demo/workspace", token=token)
    )
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert len(body["teams"]) == 3
    assert body["conversation"] is not None
    assert body["task_count"] == 0


def test_api_non_member_cannot_ensure_conversation():
    app, engine = _stack()
    s = seed(with_engine=engine)
    s["accounts"].register_team(team_id="team-outsider", team_handle="outsider", team_name="外部")
    s["accounts"].ensure_active_account(
        account_id="outsider-one",
        username="outsider-one",
        display_name="外部用户",
        email="outsider@demo.invalid",
        team_id="team-outsider",
    )
    token = _issue_token(engine, "outsider-one")
    result = asyncio.run(
        _call(app, "PUT", "/v1/projects/project-demo/conversation", token=token)
    )
    assert result.status_code == 404


def test_attachment_resource_ids_persist_with_message():
    from coifesp_harness.product import DataPropagation
    from coifesp_harness.product.repository import PROJECT_RESOURCES

    s = seed()
    workspace = s["workspace"]
    now = datetime.now(UTC)
    with s["engine"].begin() as connection:
        connection.execute(
            insert(PROJECT_RESOURCES).values(
                resource_id="resource-attach-1",
                project_id="project-demo",
                owner_team_id="team-product",
                created_by="lead-lin",
                title="附件资料",
                artifact_owner_team_id="team-product",
                artifact_id="artifact-attach-1",
                artifact_sha256="e" * 64,
                media_type="text/plain",
                propagation=DataPropagation.PROJECT_READONLY.value,
                created_at=now,
            )
        )
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请分析这份资料",
        idempotency_key="attach-msg-1",
        attachment_resource_ids=("resource-attach-1",),
    )
    assert message.attachment_resource_ids == ("resource-attach-1",)
    stored = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )[0]
    assert stored.attachment_resource_ids == ("resource-attach-1",)


class FailingRunService:
    """Always fails run creation to exercise the launch compensation path."""

    def create(self, **kwargs):
        raise RuntimeError("simulated run creation failure")


def _stack_with_run_service(run_service):
    from types import SimpleNamespace as SN

    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    collaboration = TeamCollaborationService(engine)
    workspace = ProjectWorkspaceService(engine)
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=directory,
        team_collaboration_service=collaboration,
        project_workspace_service=workspace,
        agent_run_service=run_service,
    )
    return app, engine


def test_run_launch_failure_does_not_lock_conversation():
    app, engine = _stack_with_run_service(FailingRunService())
    seed(with_engine=engine)
    token = _issue_token(engine, "lead-lin")

    for index in range(3):
        result = asyncio.run(
            _call(
                app,
                "POST",
                "/v1/projects/project-demo/conversation/messages",
                token=token,
                json={"content": f"尝试 {index}", "idempotency_key": f"flaky-{index}"},
            )
        )
        assert result.status_code == 500
    # every failed launch terminated its turn: the conversation is not locked
    # and all user messages survive for the user to retry.
    page = asyncio.run(
        _call(app, "GET", "/v1/projects/project-demo/conversation/messages", token=token)
    )
    roles = [item["role"] for item in page.json()["items"]]
    assert roles == ["user", "user", "user"]


def test_project_creation_survives_planning_run_failure():
    app, engine = _stack_with_run_service(FailingRunService())
    seed(with_engine=engine)
    token = _issue_token(engine, "lead-lin")
    created = asyncio.run(
        _call(
            app,
            "POST",
            "/v1/projects",
            token=token,
            json={
                "name": "失败规划项目",
                "description": "planning run 失败但项目仍创建",
                "owner_assignment_name": "产品统筹",
                "owner_kind": "product",
                "initial_brief": "目标：验证失败恢复。",
            },
        )
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["conversation_id"].startswith("conv-")
    # the project exists exactly once with its unique conversation
    projects = asyncio.run(_call(app, "GET", "/v1/workspace/projects", token=token))
    matches = [item for item in projects.json() if item["project"]["project_id"] == body["project_id"]]
    assert len(matches) == 1
