"""Pre-Harness baseline regression freeze (Wave 0 / task B1).

These tests pin the observable behaviour of the current (legacy) key path
before the Multi-Agent Harness refactor starts:

    Project create
    → project conversation
    → planning run projection
    → plan approve
    → TeamTask materialization
    → task accept
    → task in_progress
    → artifact/project resource
    → task submit
    → task verify

plus AgentRun terminal projection idempotency, Exchange Draft/Reply,
team-private / project-readonly data isolation, Capability
match/reserve/release and the current Product/Governance dual-mode setup.

Everything is offline: in-memory SQLite, fake run readers, a fake artifact
repository and no network, LLM, OAuth or Docker. The behaviour pinned here
must not change while the Harness work lands; see
docs/testing/project-harness-baseline.md for the frozen invariants and the
migration-only compatibility notes.
"""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import DurableRunStatus
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.capabilities import (
    CapabilityDirectoryService,
    SQLAlchemyCapabilityRepository,
)
from coifesp_harness.collaboration import (
    CollaborationRole,
    GovernanceBoard,
)
from coifesp_harness.collaboration.governance_models import (
    AssignmentState,
    BoardMember,
)
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from coifesp_harness.execution import SQLAlchemyTaskRepository, TaskExecutionService
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.product import (
    AgentExchangeService,
    ProductAccountService,
    ProjectDataPolicy,
    ProjectDirectoryService,
    ProjectResourceService,
    ProjectTeamKind,
    TeamAccountRole,
    TeamCollaborationService,
)
from coifesp_harness.product.models import (
    DataPropagation,
    ExchangeDraftStatus,
    ExchangeRecipientStatus,
    ExchangeStatus,
    PlanDraftStatus,
    ResourceAction,
    TeamTaskStatus,
    TurnStatus,
    TurnTriggerKind,
)
from coifesp_harness.product.planning import ProjectPlanningService
from coifesp_harness.product.workspace import ProjectWorkspaceService
from coifesp_harness.security import Classification, Principal


# ---------------------------------------------------------------------------
# Offline fixtures
# ---------------------------------------------------------------------------
PLAN_OUTPUT = json.dumps(
    {
        "schema": "coifesp.project-plan.v1",
        "goals": "在 8 月底前完成多团队协作演示",
        "scope": "覆盖规划、开发与验收",
        "phases": [
            {"name": "开发", "description": "核心功能开发", "order": 1, "team_category": "engineering"},
            {"name": "测试", "description": "验收回归", "order": 2, "team_category": "quality"},
        ],
        "milestones": [{"name": "M1", "target": "8 月中旬"}],
        "risks": [{"name": "联调延期", "level": "high", "mitigation": "提前排期"}],
        "dependencies": [{"name": "工程交付", "description": "依赖工程团队"}],
        "team_requirements": [
            {"team_category": "engineering", "count": 2, "rationale": "开发人力"},
            {"team_category": "quality", "count": 1, "rationale": "验收人力"},
        ],
        "acceptance_criteria": ["端到端演示可运行", "回归测试通过"],
    },
    ensure_ascii=False,
)


class FakeArtifactRepository:
    """Offline stand-in for the artifact repository used by resource publish."""

    def read(self, *, principal, owner_tenant_id, artifact_id, expected_sha256):
        return SimpleNamespace(
            artifact_id=artifact_id,
            media_type="text/markdown",
            sha256=expected_sha256,
        )


def _engine():
    return create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def seed(*, with_engine=None):
    """Three related teams, one product-owned project with all participants."""
    engine = with_engine or _engine()
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    collaboration = TeamCollaborationService(engine)
    workspace = ProjectWorkspaceService(engine)
    planning = ProjectPlanningService(engine, collaboration=collaboration)
    exchange = AgentExchangeService(engine)
    resources = ProjectResourceService(engine, artifact_repository=FakeArtifactRepository())

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
        "planning": planning,
        "exchange": exchange,
        "resources": resources,
        "lead": lead,
        "zhou": zhou,
        "su": su,
        "admins": (product_admin, eng_admin, quality_admin),
    }


def publish_resource(s, *, resource_id, actor_id, propagation, title="资料"):
    return s["resources"].publish(
        resource_id=resource_id,
        project_id="project-demo",
        actor_id=actor_id,
        title=title,
        propagation=propagation,
        artifact_id=f"artifact-{resource_id}",
        artifact_sha256="a" * 64,
    )


def drive_plan_to_tasks(s, *, actor_id="lead-lin", content=PLAN_OUTPUT, source_run_id=None):
    """Import + approve the demo plan; returns the materialized phase tasks."""
    plan = s["planning"].import_plan_draft(
        project_id="project-demo",
        actor_id=actor_id,
        content=content,
        source_run_id=source_run_id,
    )
    s["planning"].approve_plan_draft(
        project_id="project-demo", draft_id=plan.draft_id, actor_id=actor_id
    )
    tasks = s["collaboration"].list_tasks(project_id="project-demo", actor_id=actor_id)
    return [task for task in tasks if task.title.startswith("阶段执行")]


def complete_task_lifecycle(s, *, task, worker_id="contributor-zhou", reviewer_id="lead-lin"):
    """accept → assign → in_progress → submit → verify on a materialized task."""
    collaboration = s["collaboration"]
    collaboration.respond_task(
        project_id="project-demo", task_id=task.task_id, actor_id=worker_id, accept=True
    )
    collaboration.assign_internal(
        project_id="project-demo", task_id=task.task_id, actor_id=worker_id, account_id=worker_id
    )
    started = collaboration.start_task(
        project_id="project-demo", task_id=task.task_id, actor_id=worker_id
    )
    assert started.status is TeamTaskStatus.IN_PROGRESS
    submitted = collaboration.submit_task(
        project_id="project-demo",
        task_id=task.task_id,
        actor_id=worker_id,
        resource_ids=(f"res-{worker_id}",),
    )
    assert submitted.status is TeamTaskStatus.SUBMITTED
    verified = collaboration.review_task(
        project_id="project-demo",
        task_id=task.task_id,
        actor_id=reviewer_id,
        accept=True,
        note="验收通过",
    )
    return verified


class FakeRun:
    def __init__(self, run_id, status=DurableRunStatus.COMPLETED, correlation_id=""):
        self.run_id = run_id
        self.status = status
        self.correlation_id = correlation_id


def reader_for(*messages):
    return lambda run: tuple(messages)


# ---------------------------------------------------------------------------
# 1. Main chain: create → conversation → planning → approve → TeamTask
#    → accept → in_progress → resource → submit → verify
# ---------------------------------------------------------------------------
def test_baseline_main_chain_project_to_verified_task():
    s = seed()

    # project conversation
    conversation = s["workspace"].ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = s["workspace"].append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请给出项目计划",
        idempotency_key="chain-plan-1",
        trigger_kind=TurnTriggerKind.PLANNING,
    )
    assert message.turn_id == turn.turn_id
    assert turn.status is TurnStatus.ACTIVE

    # planning run projection (terminal AgentRun with a plan payload)
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-chain-plan", actor_id="lead-lin"
    )
    s["workspace"].bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-chain-plan",
    )
    from coifesp_harness.product.turn_projection import AgentTurnProjection

    projection = AgentTurnProjection(
        s["engine"],
        workspace=s["workspace"],
        planning=s["planning"],
        run_reader=reader_for({"role": "assistant", "content": PLAN_OUTPUT}),
    )
    projection.on_run_terminal(FakeRun("run-chain-plan", DurableRunStatus.COMPLETED))
    drafts = s["planning"].list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1 and drafts[0].status is PlanDraftStatus.DRAFTING
    done = s["workspace"].get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED

    # plan approve → TeamTask materialization
    approved = s["planning"].approve_plan_draft(
        project_id="project-demo", draft_id=drafts[0].draft_id, actor_id="lead-lin"
    )
    assert approved.status is PlanDraftStatus.APPROVED
    tasks = s["collaboration"].list_tasks(project_id="project-demo", actor_id="lead-lin")
    phase_tasks = [task for task in tasks if task.title.startswith("阶段执行")]
    assert len(phase_tasks) == 2
    by_target = {task.target_team_id: task for task in phase_tasks}
    assert set(by_target) == {"team-engineering", "team-quality"}
    assert all(task.status is TeamTaskStatus.PROPOSED for task in phase_tasks)
    requirements = s["planning"].list_team_requirements(
        project_id="project-demo", actor_id="lead-lin"
    )
    assert all(item.status is PlanDraftStatus.APPROVED for item in requirements)

    # accept → in_progress → artifact/project resource → submit → verify
    eng_task = by_target["team-engineering"]
    publish_resource(
        s,
        resource_id="res-contributor-zhou",
        actor_id="contributor-zhou",
        propagation=DataPropagation.PROJECT_READONLY,
        title="工程交付物",
    )
    verified = complete_task_lifecycle(s, task=eng_task)
    assert verified.status is TeamTaskStatus.VERIFIED
    assert verified.completed_at is not None
    assert verified.artifact_resource_ids == ("res-contributor-zhou",)
    assert verified.review_note == "验收通过"

    # the quality phase runs the same lifecycle to completion
    quality_task = by_target["team-quality"]
    publish_resource(
        s,
        resource_id="res-reviewer-su",
        actor_id="reviewer-su",
        propagation=DataPropagation.PROJECT_READONLY,
        title="验收报告",
    )
    quality_verified = complete_task_lifecycle(s, task=quality_task, worker_id="reviewer-su")
    assert quality_verified.status is TeamTaskStatus.VERIFIED


def test_baseline_task_schedule_and_activity_events_are_recorded():
    s = seed()
    phase_tasks = drive_plan_to_tasks(s)
    collaboration = s["collaboration"]
    task = next(item for item in phase_tasks if item.target_team_id == "team-engineering")
    activities = collaboration.list_activities(project_id="project-demo", actor_id="lead-lin")
    event_types = [item.event_type for item in activities]
    # the approved plan projected its confirmation topic and phase tasks
    assert "topic.opened" in event_types
    assert "task.proposed" in event_types
    collaboration.respond_task(
        project_id="project-demo", task_id=task.task_id, actor_id="contributor-zhou", accept=True
    )
    moved = collaboration.list_tasks(project_id="project-demo", actor_id="lead-lin")
    target = next(item for item in moved if item.task_id == task.task_id)
    assert target.status is TeamTaskStatus.ACCEPTED


# ---------------------------------------------------------------------------
# 2. AgentRun terminal projection idempotency
# ---------------------------------------------------------------------------
def test_agent_run_terminal_projection_is_idempotent_across_retries():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请分析风险",
        idempotency_key="proj-idem-1",
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-idem", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-idem",
    )
    from coifesp_harness.product.turn_projection import AgentTurnProjection

    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        run_reader=reader_for({"role": "assistant", "content": "风险分析如下"}),
    )
    for _ in range(3):
        projection.on_run_terminal(FakeRun("run-idem", DurableRunStatus.COMPLETED))
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[-1].content == "风险分析如下"
    assert workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    ).status is TurnStatus.COMPLETED


def test_planning_projection_imports_exactly_one_draft_per_run():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请给出项目计划",
        idempotency_key="proj-plan-idem-1",
        trigger_kind=TurnTriggerKind.PLANNING,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-plan-idem", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-plan-idem",
    )
    from coifesp_harness.product.turn_projection import AgentTurnProjection

    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        planning=s["planning"],
        run_reader=reader_for({"role": "assistant", "content": PLAN_OUTPUT}),
    )
    for _ in range(2):
        projection.on_run_terminal(FakeRun("run-plan-idem", DurableRunStatus.COMPLETED))
    drafts = s["planning"].list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1
    assert drafts[0].source_run_id == "run-plan-idem"
    # a second import with the same run id converges on the same draft
    again = s["planning"].import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-plan-idem",
    )
    assert again.draft_id == drafts[0].draft_id
    assert len(s["planning"].list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 1


# ---------------------------------------------------------------------------
# 3. Exchange Draft/Reply baseline
# ---------------------------------------------------------------------------
def test_exchange_draft_reply_baseline():
    s = seed()
    publish_resource(
        s,
        resource_id="res-shared",
        actor_id="lead-lin",
        propagation=DataPropagation.PROJECT_READONLY,
        title="共享背景资料",
    )
    exchange = s["exchange"]

    # human-reviewed draft on the source side
    draft = exchange.create_draft(
        draft_id="draft-baseline-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="接口字段确认",
        summary="需要工程团队确认字段命名",
        request="请确认 API 字段名称并回复",
        constraints="本周内回复",
        shared_resource_ids=("res-shared",),
        recipient_team_ids=("team-engineering",),
    )
    assert draft.status is ExchangeDraftStatus.DRAFTING
    assert draft.version == 1

    # optimistic-lock editing while drafting
    updated = exchange.update_draft(
        project_id="project-demo",
        draft_id=draft.draft_id,
        actor_id="lead-lin",
        expected_version=1,
        purpose="接口字段确认",
        summary="需要工程团队确认字段命名（更新）",
        request="请确认 API 字段名称并回复",
        constraints="本周内回复",
        shared_resource_ids=("res-shared",),
        recipient_team_ids=("team-engineering",),
    )
    assert updated.version == 2

    # approval publishes one exchange + one pending recipient row
    published = exchange.approve_draft(
        project_id="project-demo",
        draft_id=draft.draft_id,
        actor_id="lead-lin",
        expected_version=2,
    )
    assert published.status is ExchangeStatus.SENT
    assert [d.status for d in exchange.list_drafts(project_id="project-demo", actor_id="lead-lin")] == [
        ExchangeDraftStatus.APPROVED
    ]
    recipient = exchange.get_recipient(
        exchange_id=published.exchange_id, recipient_team_id="team-engineering"
    )
    assert recipient.status is ExchangeRecipientStatus.PENDING
    # the snapshot records shared context but never another team's private rows
    assert "res-shared" in recipient.context_snapshot["shared_resource_ids"]

    # recipient side: Agent drafts, human confirms by submitting
    exchange.record_response_draft_turn(
        exchange_id=published.exchange_id,
        recipient_team_id="team-engineering",
        turn_id="turn-reply-1",
    )
    exchange.store_response_draft(
        exchange_id=published.exchange_id,
        recipient_team_id="team-engineering",
        content="字段名称已确认为 snake_case",
    )
    recipient = exchange.get_recipient(
        exchange_id=published.exchange_id, recipient_team_id="team-engineering"
    )
    assert recipient.status is ExchangeRecipientStatus.DRAFTING
    response = exchange.submit_response(
        project_id="project-demo",
        exchange_id=published.exchange_id,
        actor_id="contributor-zhou",
        content="",
    )
    assert response.content == "字段名称已确认为 snake_case"
    recipient = exchange.get_recipient(
        exchange_id=published.exchange_id, recipient_team_id="team-engineering"
    )
    assert recipient.status is ExchangeRecipientStatus.RESPONDED
    # every recipient answered → the exchange itself is responded
    assert exchange.get_exchange(
        project_id="project-demo", exchange_id=published.exchange_id, actor_id="lead-lin"
    ).status is ExchangeStatus.RESPONDED
    # source team sees the reply; a second reply from the same team is refused
    responses = exchange.list_responses(
        project_id="project-demo", exchange_id=published.exchange_id, actor_id="lead-lin"
    )
    assert [item.content for item in responses] == ["字段名称已确认为 snake_case"]
    try:
        exchange.submit_response(
            project_id="project-demo",
            exchange_id=published.exchange_id,
            actor_id="contributor-zhou",
            content="重复回复",
        )
        raise AssertionError("expected duplicate reply to be refused")
    except GovernanceConflictError:
        pass


def test_exchange_visibility_follows_involvement():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-vis-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="仅工程可见",
        summary="工程专项确认",
        request="请工程团队确认",
        recipient_team_ids=("team-engineering",),
    )
    published = exchange.approve_draft(
        project_id="project-demo",
        draft_id=draft.draft_id,
        actor_id="lead-lin",
        expected_version=1,
    )
    # source and addressed team see it
    source_view = exchange.list_exchanges(project_id="project-demo", actor_id="lead-lin")
    recipient_view = exchange.list_exchanges(project_id="project-demo", actor_id="contributor-zhou")
    assert published.exchange_id in [item.exchange_id for item in source_view]
    assert published.exchange_id in [item.exchange_id for item in recipient_view]
    # an uninvolved team does not see the exchange at all
    try:
        exchange.get_exchange(
            project_id="project-demo", exchange_id=published.exchange_id, actor_id="reviewer-su"
        )
        raise AssertionError("expected uninvolved team read to be refused")
    except ResourceNotFound:
        pass
    # a recipient team never sees another recipient's snapshot
    recipients = exchange.list_recipients(
        project_id="project-demo", exchange_id=published.exchange_id, actor_id="contributor-zhou"
    )
    assert [item.recipient_team_id for item in recipients] == ["team-engineering"]


# ---------------------------------------------------------------------------
# 4. team-private / project-readonly data isolation
# ---------------------------------------------------------------------------
def test_resource_isolation_team_private_and_project_readonly():
    s = seed()
    publish_resource(
        s,
        resource_id="res-eng-private",
        actor_id="contributor-zhou",
        propagation=DataPropagation.TEAM_PRIVATE,
        title="工程内部笔记",
    )
    publish_resource(
        s,
        resource_id="res-prod-readonly",
        actor_id="lead-lin",
        propagation=DataPropagation.PROJECT_READONLY,
        title="项目章程",
    )
    publish_resource(
        s,
        resource_id="res-portable",
        actor_id="lead-lin",
        propagation=DataPropagation.PORTABLE,
        title="可携带素材",
    )

    # list_visible: team-private rows only reach their owner team
    eng_visible = {item.resource_id for item in s["resources"].list_visible(
        actor_id="contributor-zhou", project_id="project-demo"
    )}
    lead_visible = {item.resource_id for item in s["resources"].list_visible(
        actor_id="lead-lin", project_id="project-demo"
    )}
    assert "res-eng-private" in eng_visible
    assert "res-eng-private" not in lead_visible
    assert {"res-prod-readonly", "res-portable"} <= lead_visible

    policy = ProjectDataPolicy(s["engine"])

    # team-private: invisible cross-team, and the owner cannot propagate it
    assert not policy.decide(
        account_id="lead-lin", resource_id="res-eng-private", action=ResourceAction.VIEW
    ).allowed
    assert not policy.decide(
        account_id="contributor-zhou",
        resource_id="res-eng-private",
        action=ResourceAction.RESHARE,
        project_id="project-demo",
    ).allowed
    assert policy.decide(
        account_id="contributor-zhou",
        resource_id="res-eng-private",
        action=ResourceAction.VIEW,
        project_id="project-demo",
    ).allowed

    # project-readonly: project teams may view / agent-use in context, never export
    for action in (ResourceAction.VIEW, ResourceAction.AGENT_USE):
        access = policy.decide(
            account_id="contributor-zhou",
            resource_id="res-prod-readonly",
            action=action,
            project_id="project-demo",
        )
        assert access.allowed, action
    for action in (ResourceAction.DOWNLOAD, ResourceAction.SAVE, ResourceAction.RESHARE):
        access = policy.decide(
            account_id="contributor-zhou",
            resource_id="res-prod-readonly",
            action=action,
            project_id="project-demo",
        )
        assert not access.allowed, action
    # outside the project context even reading is refused
    assert not policy.decide(
        account_id="contributor-zhou",
        resource_id="res-prod-readonly",
        action=ResourceAction.VIEW,
        project_id=None,
    ).allowed

    # portable: usable by project teams inside the project
    assert policy.decide(
        account_id="reviewer-su",
        resource_id="res-portable",
        action=ResourceAction.DOWNLOAD,
        project_id="project-demo",
    ).allowed

    # propagation flips are owner-only optimistic-lock transitions
    s["workspace"].update_resource_propagation(
        project_id="project-demo",
        resource_id="res-eng-private",
        actor_id="contributor-zhou",
        requested_propagation=DataPropagation.PROJECT_READONLY,
        expected_propagation=DataPropagation.TEAM_PRIVATE,
    )
    assert "res-eng-private" in {
        item.resource_id for item in s["resources"].list_visible(
            actor_id="lead-lin", project_id="project-demo"
        )
    }
    try:
        s["workspace"].update_resource_propagation(
            project_id="project-demo",
            resource_id="res-eng-private",
            actor_id="contributor-zhou",
            requested_propagation=DataPropagation.TEAM_PRIVATE,
            expected_propagation=DataPropagation.TEAM_PRIVATE,
        )
        raise AssertionError("expected stale propagation update to be refused")
    except GovernanceConflictError:
        pass


def test_task_submission_and_exchange_reject_team_private_resources():
    s = seed()
    phase_tasks = drive_plan_to_tasks(s)
    eng_task = next(task for task in phase_tasks if task.target_team_id == "team-engineering")
    collaboration = s["collaboration"]
    collaboration.respond_task(
        project_id="project-demo", task_id=eng_task.task_id, actor_id="contributor-zhou", accept=True
    )
    collaboration.assign_internal(
        project_id="project-demo",
        task_id=eng_task.task_id,
        actor_id="contributor-zhou",
        account_id="contributor-zhou",
    )
    collaboration.start_task(
        project_id="project-demo", task_id=eng_task.task_id, actor_id="contributor-zhou"
    )
    # a team-private deliverable can never be submitted onto the task
    publish_resource(
        s,
        resource_id="res-eng-private-deliverable",
        actor_id="contributor-zhou",
        propagation=DataPropagation.TEAM_PRIVATE,
        title="内部草稿",
    )
    try:
        collaboration.submit_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="contributor-zhou",
            resource_ids=("res-eng-private-deliverable",),
        )
        raise AssertionError("expected team-private submission to be refused")
    except PolicyDenied:
        pass
    # resources owned by another team are refused as well
    publish_resource(
        s,
        resource_id="res-product-owned",
        actor_id="lead-lin",
        propagation=DataPropagation.PROJECT_READONLY,
        title="产品资料",
    )
    try:
        collaboration.submit_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="contributor-zhou",
            resource_ids=("res-product-owned",),
        )
        raise AssertionError("expected foreign-resource submission to be refused")
    except PolicyDenied:
        pass

    # and a shared exchange package can never carry team-private context:
    # drafting accepts it, but the approval boundary refuses publication
    publish_resource(
        s,
        resource_id="res-quality-private",
        actor_id="reviewer-su",
        propagation=DataPropagation.TEAM_PRIVATE,
        title="质量内部清单",
    )
    leaked = s["exchange"].create_draft(
        draft_id="draft-leak-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="泄漏边界",
        summary="发布必须被拒绝",
        request="携带私有资源",
        shared_resource_ids=("res-quality-private",),
        recipient_team_ids=("team-quality",),
    )
    assert leaked.status is ExchangeDraftStatus.DRAFTING
    try:
        s["exchange"].approve_draft(
            project_id="project-demo",
            draft_id=leaked.draft_id,
            actor_id="lead-lin",
            expected_version=1,
        )
        raise AssertionError("expected private resource publication to be refused")
    except GovernanceConflictError:
        pass


# ---------------------------------------------------------------------------
# 5. Capability match / reserve / release
# ---------------------------------------------------------------------------
def _capability_stack():
    engine = _engine()
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}
        ),
    )
    audit.create_schema()
    repository = SQLAlchemyCapabilityRepository(engine=engine, audit_log=audit)
    repository.create_schema()
    service = CapabilityDirectoryService(repository)
    publisher = Principal(
        "lead-a",
        "team-a",
        roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED,
        compartments=frozenset({"program-1"}),
    )
    consumer = Principal(
        "lead-b",
        "team-b",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
    )
    return service, publisher, consumer


def _publish_capability(service, publisher, **overrides):
    values = dict(
        idempotency_key="publish-baseline",
        capability_id="doc-review",
        version="1.0.0",
        name="Document review",
        description="Reviews disclosed documents",
        tags=("office", "review"),
        protocols=("a2a-1.0",),
        input_contract="urn:contract:doc-review-input:v1",
        output_contract="urn:contract:doc-review-output:v1",
        max_input_classification=Classification.CONFIDENTIAL,
        required_compartments=("program-1",),
        residency_regions=("cn-north",),
        visible_to_tenants=("team-a", "team-b"),
    )
    values.update(overrides)
    return service.publish(principal=publisher, **values)


def test_capability_publish_match_reserve_release_baseline():
    service, publisher, consumer = _capability_stack()
    first = _publish_capability(service, publisher)
    second = _publish_capability(service, publisher)
    assert not first.duplicate and second.duplicate

    valid_until = datetime.now(UTC) + timedelta(hours=1)
    capacity = service.declare_capacity(
        principal=publisher,
        provider_tenant_id="team-a",
        capability_id="doc-review",
        version="1.0.0",
        status="available",
        available_slots=2,
        valid_until=valid_until,
    )
    assert capacity.available_slots == 2

    matched = service.match(
        principal=consumer,
        required_tags=("review",),
        protocol="a2a-1.0",
        input_classification=Classification.CONFIDENTIAL,
        compartments=("program-1",),
        residency_regions=("cn-north",),
    )
    assert len(matched) == 1
    assert matched[0].capability.capability_id == "doc-review"
    assert "tag_overlap=1" in matched[0].reasons

    expires = datetime.now(UTC) + timedelta(minutes=30)
    reservation = service.reserve(
        principal=consumer,
        reservation_id="reserve-baseline-1",
        provider_tenant_id="team-a",
        capability_id="doc-review",
        version="1.0.0",
        slots=2,
        expires_at=expires,
    )
    assert reservation.status == "active"
    # reservation replay is idempotent and keeps the booking
    replayed = service.reserve(
        principal=consumer,
        reservation_id="reserve-baseline-1",
        provider_tenant_id="team-a",
        capability_id="doc-review",
        version="1.0.0",
        slots=2,
        expires_at=expires,
    )
    assert replayed.status == "active"
    # all slots are booked: match fails closed and overbooking conflicts
    assert service.match(
        principal=consumer,
        required_tags=("review",),
        protocol="a2a-1.0",
        input_classification=Classification.CONFIDENTIAL,
        compartments=("program-1",),
        residency_regions=("cn-north",),
    ) == ()
    try:
        service.reserve(
            principal=consumer,
            reservation_id="reserve-baseline-2",
            provider_tenant_id="team-a",
            capability_id="doc-review",
            version="1.0.0",
            slots=1,
            expires_at=expires,
        )
        raise AssertionError("expected overbooking reservation to be refused")
    except GovernanceConflictError:
        pass
    released = service.release_reservation(
        principal=consumer, provider_tenant_id="team-a", reservation_id="reserve-baseline-1"
    )
    assert released.status == "released"
    # slots are free again
    after_release = service.match(
        principal=consumer,
        required_tags=("review",),
        protocol="a2a-1.0",
        input_classification=Classification.CONFIDENTIAL,
        compartments=("program-1",),
        residency_regions=("cn-north",),
    )
    assert len(after_release) == 1


# ---------------------------------------------------------------------------
# 6. Product/Governance dual-mode behaviour
# ---------------------------------------------------------------------------
def _governance_board_in_progress():
    board = GovernanceBoard(
        program_id="program-1",
        owner_tenant_id="team-a",
        title="Program",
        objective="Objective",
        classification=Classification.INTERNAL,
        compartments=frozenset(),
        audit=InMemoryAuditSink(),
    )
    board.add_member(BoardMember("lead-a", "team-a", CollaborationRole.LEAD))
    board.add_member(
        BoardMember("worker-b", "team-b", CollaborationRole.CONTRIBUTOR), actor_id="lead-a"
    )
    plan = board.create_plan(
        actor_id="lead-a",
        plan_id="plan-1",
        version=1,
        title="Plan",
        objective="Objective",
        deliverables=("deliverable",),
        required_approvers=frozenset({"lead-a"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    board.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    assignment = board.propose_assignment(
        actor_id="lead-a",
        assignment_id="assignment-1",
        plan_id=plan.plan_id,
        assignee_id="worker-b",
        title="Implement",
        description="Implementation details",
        deliverable_contract="Signed artifact",
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.respond_to_assignment(
        actor_id="worker-b", assignment_id=assignment.assignment_id, accept=True
    )
    board.start_assignment(actor_id="worker-b", assignment_id=assignment.assignment_id)
    return board


class _GovernanceView:
    """The narrow governance port TaskExecutionService depends on today."""

    def __init__(self, board):
        self.board = board

    def read_program(self, *, principal, program_id):
        assert program_id == self.board.program_id
        return self.board


def _execution_stack():
    engine = _engine()
    repository = SQLAlchemyTaskRepository(engine=engine)
    repository.create_schema()
    return TaskExecutionService(
        repository=repository,
        governance=_GovernanceView(_governance_board_in_progress()),
    )


def test_product_and_governance_task_modes_run_side_by_side():
    # Mode 1 (legacy governance): TeamTask-independent TaskAssignment drives
    # durable execution via TaskExecutionService.
    execution = _execution_stack()
    worker = Principal("worker-b", "team-b")
    task = execution.enqueue_assignment(
        principal=worker,
        idempotency_key="enqueue-baseline-1",
        task_id="execution-1",
        program_id="program-1",
        assignment_id="assignment-1",
        queue="coding",
        payload={"tool": "contract-tests"},
    )
    assert task.assignment_id == "assignment-1"
    assert execution.repository.get(tenant_id="team-b", task_id="execution-1") is not None
    service_worker = Principal(
        "worker-service",
        "team-b",
        roles=frozenset({"execution_worker"}),
        is_service=True,
    )
    lease = execution.claim(worker=service_worker, queue="coding")
    assert lease is not None
    execution.start(worker=service_worker, task_id=task.task_id, lease_token=lease.lease_token)
    execution.succeed(
        worker=service_worker,
        task_id=task.task_id,
        lease_token=lease.lease_token,
        result={"artifact_ref": "git:commit:abc"},
    )

    # Mode 2 (product): the TeamTask state machine runs independently and no
    # Governance assignment is created or required anywhere on the path.
    s = seed()
    phase_tasks = drive_plan_to_tasks(s)
    eng_task = next(item for item in phase_tasks if item.target_team_id == "team-engineering")
    publish_resource(
        s,
        resource_id="res-contributor-zhou",
        actor_id="contributor-zhou",
        propagation=DataPropagation.PROJECT_READONLY,
        title="工程交付物",
    )
    verified = complete_task_lifecycle(s, task=eng_task)
    assert verified.status is TeamTaskStatus.VERIFIED


def test_governance_gate_still_requires_in_progress_assignment_state():
    """The current execution gate reads TaskAssignment.state, not TeamTask.status.

    Frozen as migration-period behaviour: A5 will swap this gate to the
    project work contract, and when that happens this assertion is expected
    to be replaced deliberately.
    """
    engine = _engine()
    repository = SQLAlchemyTaskRepository(engine=engine)
    repository.create_schema()
    board = GovernanceBoard(
        program_id="program-2",
        owner_tenant_id="team-a",
        title="Program",
        objective="Objective",
        classification=Classification.INTERNAL,
        compartments=frozenset(),
        audit=InMemoryAuditSink(),
    )
    board.add_member(BoardMember("lead-a", "team-a", CollaborationRole.LEAD))
    board.add_member(
        BoardMember("worker-b", "team-b", CollaborationRole.CONTRIBUTOR), actor_id="lead-a"
    )
    plan = board.create_plan(
        actor_id="lead-a",
        plan_id="plan-2",
        version=1,
        title="Plan",
        objective="Objective",
        deliverables=("deliverable",),
        required_approvers=frozenset({"lead-a"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    board.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    assignment = board.propose_assignment(
        actor_id="lead-a",
        assignment_id="assignment-2",
        plan_id=plan.plan_id,
        assignee_id="worker-b",
        title="Implement",
        description="Implementation details",
        deliverable_contract="Signed artifact",
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.respond_to_assignment(
        actor_id="worker-b", assignment_id=assignment.assignment_id, accept=True
    )
    assert board.assignments["assignment-2"].state is AssignmentState.ACCEPTED

    service = TaskExecutionService(
        repository=repository, governance=_GovernanceView(board)
    )
    worker = Principal("worker-b", "team-b")
    try:
        service.enqueue_assignment(
            principal=worker,
            idempotency_key="enqueue-gate-1",
            task_id="execution-2",
            program_id="program-2",
            assignment_id="assignment-2",
            queue="coding",
            payload={"tool": "contract-tests"},
        )
        raise AssertionError("expected enqueue before start_assignment to be refused")
    except PolicyDenied:
        pass
    board.start_assignment(actor_id="worker-b", assignment_id="assignment-2")
    task = service.enqueue_assignment(
        principal=worker,
        idempotency_key="enqueue-gate-2",
        task_id="execution-3",
        program_id="program-2",
        assignment_id="assignment-2",
        queue="coding",
        payload={"tool": "contract-tests"},
    )
    assert task.assignment_id == "assignment-2"
