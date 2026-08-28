"""Baseline failure-path freeze: crash replay, guard rejections, recovery.

Companion to ``test_project_harness_legacy_baseline.py``. Where that file
pins the happy path, this one pins how the current system fails and
recovers:

- plan import replay converges on one draft; invalid payloads are refused;
- a crash between plan-approval projections replays without duplication;
- startup replay of terminal AgentRuns finishes interrupted projections;
- a failed exchange reply run releases the recipient drafting turn;
- TeamTask transition guards reject unauthorized moves and the
  changes-requested loop re-enters in_progress;
- the governance execution gate and its reject semantics stay as they are.

All offline: in-memory SQLite, fake run readers/services, no network.
"""
import json

import pytest

from coifesp_harness.agent_runs import DurableRunStatus
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.collaboration import CollaborationRole, GovernanceBoard
from coifesp_harness.collaboration.governance_models import (
    AssignmentState,
    BoardMember,
)
from coifesp_harness.errors import (
    GovernanceConflictError,
    PolicyDenied,
    ResourceNotFound,
)
from coifesp_harness.product.models import (
    DataPropagation,
    ExchangeDraftStatus,
    ExchangeRecipientStatus,
    PlanDraftStatus,
    TeamTaskStatus,
    TurnStatus,
    TurnTriggerKind,
)
from coifesp_harness.product.turn_projection import AgentTurnProjection
from coifesp_harness.security import Classification, Principal

from test_project_harness_legacy_baseline import (
    PLAN_OUTPUT,
    FakeRun,
    complete_task_lifecycle,
    drive_plan_to_tasks,
    publish_resource,
    reader_for,
    seed,
)


# ---------------------------------------------------------------------------
# Plan import: replay convergence + payload validation
# ---------------------------------------------------------------------------
def test_plan_import_replay_converges_on_single_draft():
    s = seed()
    planning = s["planning"]
    first = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-replay-1",
    )
    replay = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-replay-1",
    )
    assert replay.draft_id == first.draft_id
    assert len(planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 1
    # a different run id is a different draft
    second = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-replay-2",
    )
    assert second.draft_id != first.draft_id
    assert len(planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 2


def test_plan_import_rejects_invalid_payloads():
    s = seed()
    planning = s["planning"]
    bad_payloads = (
        "not-json",
        json.dumps({"schema": "other.schema"}),
        json.dumps({"schema": "coifesp.project-plan.v1", "goals": "", "scope": "x"}),
        json.dumps({"schema": "coifesp.project-plan.v1", "goals": "g", "scope": ""}),
        json.dumps(
            {
                "schema": "coifesp.project-plan.v1",
                "goals": "g",
                "scope": "s",
                "team_requirements": [{"team_category": "unknown", "count": 1}],
            }
        ),
        json.dumps(
            {
                "schema": "coifesp.project-plan.v1",
                "goals": "g",
                "scope": "s",
                "team_requirements": [{"team_category": "engineering", "count": 0}],
            }
        ),
    )
    for content in bad_payloads:
        with pytest.raises(ValueError):
            planning.import_plan_draft(
                project_id="project-demo", actor_id="lead-lin", content=content
            )
    assert len(planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 0


def test_non_owner_cannot_approve_and_approved_draft_cannot_reapprove():
    s = seed()
    planning = s["planning"]
    plan = planning.import_plan_draft(
        project_id="project-demo", actor_id="lead-lin", content=PLAN_OUTPUT
    )
    with pytest.raises(GovernanceConflictError):
        planning.approve_plan_draft(
            project_id="project-demo", draft_id=plan.draft_id, actor_id="contributor-zhou"
        )
    approved = planning.approve_plan_draft(
        project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
    )
    assert approved.status is PlanDraftStatus.APPROVED
    with pytest.raises(GovernanceConflictError):
        planning.approve_plan_draft(
            project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
        )


# ---------------------------------------------------------------------------
# Plan approval crash replay: deterministic ids converge, nothing duplicates
# ---------------------------------------------------------------------------
def test_plan_approve_crash_replay_does_not_duplicate_projection(monkeypatch):
    s = seed()
    planning = s["planning"]
    collaboration = s["collaboration"]
    plan = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-crash-approve",
    )
    original_create_task = collaboration.create_task
    calls = {"count": 0}

    def crashing_create_task(**kwargs):
        result = original_create_task(**kwargs)
        calls["count"] += 1
        if calls["count"] == 2:
            # Simulate a process crash after the first phase task landed but
            # before the second one and before the draft was finalized.
            raise RuntimeError("simulated crash between approval projections")
        return result

    monkeypatch.setattr(collaboration, "create_task", crashing_create_task)
    with pytest.raises(RuntimeError, match="simulated crash"):
        planning.approve_plan_draft(
            project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
        )
    monkeypatch.undo()
    # the draft stayed open, so a retry is possible and must converge
    drafts = planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert [item.status for item in drafts] == [PlanDraftStatus.DRAFTING]
    approved = planning.approve_plan_draft(
        project_id="project-demo", draft_id=plan.draft_id, actor_id="lead-lin"
    )
    assert approved.status is PlanDraftStatus.APPROVED
    topics = collaboration.list_topics(project_id="project-demo", actor_id="lead-lin")
    tasks = collaboration.list_tasks(project_id="project-demo", actor_id="lead-lin")
    assert len([item for item in topics if item.title == "项目计划确认"]) == 1
    assert len([item for item in topics if item.title.startswith("里程碑")]) == 1
    phase_tasks = [item for item in tasks if item.title.startswith("阶段执行")]
    assert len(phase_tasks) == 2
    assert {item.target_team_id for item in phase_tasks} == {"team-engineering", "team-quality"}


# ---------------------------------------------------------------------------
# AgentRun startup replay finishes interrupted projections exactly once
# ---------------------------------------------------------------------------
class _ReplayRunService:
    def __init__(self, runs):
        self.runs = runs
        self.reads = []

    def get(self, *, principal, run_id):
        self.reads.append(run_id)
        return self.runs[run_id]


def test_startup_replay_completes_terminal_run_projection_once():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请给出项目计划",
        idempotency_key="replay-plan-1",
        trigger_kind=TurnTriggerKind.PLANNING,
    )
    # the run reached terminal state but the projection never ran (crash)
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-replay-crash", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-replay-crash",
    )
    replay_service = _ReplayRunService(
        {"run-replay-crash": FakeRun("run-replay-crash", DurableRunStatus.COMPLETED)}
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        planning=s["planning"],
        run_reader=reader_for({"role": "assistant", "content": PLAN_OUTPUT}),
    )
    replayed = projection.replay_pending(replay_service)
    assert replayed == 1
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.role for item in messages] == ["user", "assistant"]
    assert workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    ).status is TurnStatus.COMPLETED
    drafts = s["planning"].list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1 and drafts[0].source_run_id == "run-replay-crash"
    # a second startup pass finds nothing left to do and duplicates nothing
    assert projection.replay_pending(replay_service) == 0
    assert [
        item.role
        for item in workspace.list_messages(
            conversation_id=conversation.conversation_id, actor_id="lead-lin"
        )
    ] == ["user", "assistant"]
    assert len(s["planning"].list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 1


# ---------------------------------------------------------------------------
# Failed exchange reply run releases the recipient drafting turn
# ---------------------------------------------------------------------------
def _published_exchange(s):
    publish_resource(
        s,
        resource_id="res-shared-fail",
        actor_id="lead-lin",
        propagation=DataPropagation.PROJECT_READONLY,
        title="共享背景",
    )
    draft = s["exchange"].create_draft(
        draft_id="draft-fail-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="回复草稿失败",
        summary="验证失败释放",
        request="请回复确认",
        shared_resource_ids=("res-shared-fail",),
        recipient_team_ids=("team-engineering",),
    )
    return s["exchange"].approve_draft(
        project_id="project-demo",
        draft_id=draft.draft_id,
        actor_id="lead-lin",
        expected_version=1,
    )


def test_failed_exchange_reply_run_releases_drafting_turn_and_allows_retry():
    s = seed()
    exchange = s["exchange"]
    published = _published_exchange(s)
    # the engineering team binds a recipient-side drafting turn
    conversation = s["workspace"].ensure_conversation(
        project_id="project-demo", actor_id="contributor-zhou"
    )
    _, turn = s["workspace"].append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="contributor-zhou",
        content="请起草回复",
        idempotency_key="exchange-reply-fail-1",
        trigger_kind=TurnTriggerKind.EXCHANGE,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-reply-fail", actor_id="contributor-zhou"
    )
    s["workspace"].bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-reply-fail",
    )
    exchange.record_response_draft_turn(
        exchange_id=published.exchange_id,
        recipient_team_id="team-engineering",
        turn_id=turn.turn_id,
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=s["workspace"],
        exchange=exchange,
        run_reader=reader_for(),
    )
    projection.on_run_terminal(FakeRun("run-reply-fail", DurableRunStatus.FAILED))
    recipient = exchange.get_recipient(
        exchange_id=published.exchange_id, recipient_team_id="team-engineering"
    )
    # the drafting binding is released; the team is back to PENDING, not stuck
    assert recipient.draft_turn_id is None
    assert recipient.status is ExchangeRecipientStatus.PENDING
    # the turn failed, the user message is kept and a retry turn is possible
    assert s["workspace"].get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    ).status is TurnStatus.FAILED
    _, retry_turn = s["workspace"].append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="contributor-zhou",
        content="重试起草回复",
        idempotency_key="exchange-reply-fail-2",
        trigger_kind=TurnTriggerKind.EXCHANGE,
    )
    assert retry_turn.status is TurnStatus.ACTIVE


def test_exchange_draft_version_conflicts_and_uninvolved_reads():
    s = seed()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-conflict-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="版本冲突",
        summary="乐观锁",
        request="请确认",
        recipient_team_ids=("team-engineering",),
    )
    with pytest.raises(GovernanceConflictError):
        exchange.update_draft(
            project_id="project-demo",
            draft_id=draft.draft_id,
            actor_id="lead-lin",
            expected_version=2,
            purpose="版本冲突",
            summary="乐观锁",
            request="请确认",
            recipient_team_ids=("team-engineering",),
        )
    with pytest.raises(ResourceNotFound):
        exchange.update_draft(
            project_id="project-demo",
            draft_id=draft.draft_id,
            actor_id="contributor-zhou",
            expected_version=1,
            purpose="版本冲突",
            summary="跨队修改",
            request="请确认",
            recipient_team_ids=("team-engineering",),
        )
    # drafts are only visible to their own team
    assert exchange.list_drafts(project_id="project-demo", actor_id="contributor-zhou") == ()
    with pytest.raises(GovernanceConflictError):
        exchange.approve_draft(
            project_id="project-demo",
            draft_id=draft.draft_id,
            actor_id="lead-lin",
            expected_version=99,
        )


# ---------------------------------------------------------------------------
# TeamTask transition guards and the changes-requested loop
# ---------------------------------------------------------------------------
def test_team_task_transition_guards_reject_unauthorized_moves():
    s = seed()
    phase_tasks = drive_plan_to_tasks(s)
    eng_task = next(item for item in phase_tasks if item.target_team_id == "team-engineering")
    collaboration = s["collaboration"]
    # a non-target team cannot accept
    with pytest.raises(PolicyDenied):
        collaboration.respond_task(
            project_id="project-demo", task_id=eng_task.task_id, actor_id="reviewer-su", accept=True
        )
    collaboration.respond_task(
        project_id="project-demo", task_id=eng_task.task_id, actor_id="contributor-zhou", accept=True
    )
    # starting requires an internal owner first
    with pytest.raises(GovernanceConflictError):
        collaboration.start_task(
            project_id="project-demo", task_id=eng_task.task_id, actor_id="contributor-zhou"
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
    # submitting without resources, with duplicates, before in_progress is refused
    with pytest.raises(ValueError):
        collaboration.submit_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="contributor-zhou",
            resource_ids=(),
        )
    with pytest.raises(ValueError):
        collaboration.submit_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="contributor-zhou",
            resource_ids=("res-a", "res-a"),
        )
    # the source team cannot submit its own incoming task
    with pytest.raises(PolicyDenied):
        collaboration.submit_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="lead-lin",
            resource_ids=("res-x",),
        )
    publish_resource(
        s,
        resource_id="res-deliverable-x",
        actor_id="contributor-zhou",
        propagation=DataPropagation.PROJECT_READONLY,
        title="交付物",
    )
    collaboration.submit_task(
        project_id="project-demo",
        task_id=eng_task.task_id,
        actor_id="contributor-zhou",
        resource_ids=("res-deliverable-x",),
    )
    # only the source team reviews; the target team cannot verify its own work
    with pytest.raises(PolicyDenied):
        collaboration.review_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="contributor-zhou",
            accept=True,
            note="自审",
        )
    with pytest.raises(PolicyDenied):
        collaboration.review_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="reviewer-su",
            accept=True,
            note="非来源团队",
        )


def test_changes_requested_loop_reenters_in_progress_and_verifies():
    s = seed()
    phase_tasks = drive_plan_to_tasks(s)
    eng_task = next(item for item in phase_tasks if item.target_team_id == "team-engineering")
    publish_resource(
        s,
        resource_id="res-loop-1",
        actor_id="contributor-zhou",
        propagation=DataPropagation.PROJECT_READONLY,
        title="第一版交付",
    )
    publish_resource(
        s,
        resource_id="res-loop-2",
        actor_id="contributor-zhou",
        propagation=DataPropagation.PROJECT_READONLY,
        title="修订交付",
    )
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
    collaboration.submit_task(
        project_id="project-demo",
        task_id=eng_task.task_id,
        actor_id="contributor-zhou",
        resource_ids=("res-loop-1",),
    )
    changes = collaboration.review_task(
        project_id="project-demo",
        task_id=eng_task.task_id,
        actor_id="lead-lin",
        accept=False,
        note="需要补充边界说明",
    )
    assert changes.status is TeamTaskStatus.CHANGES_REQUESTED
    assert changes.review_note == "需要补充边界说明"
    # the changes-requested task re-enters in_progress with the same owner
    resumed = collaboration.start_task(
        project_id="project-demo", task_id=eng_task.task_id, actor_id="contributor-zhou"
    )
    assert resumed.status is TeamTaskStatus.IN_PROGRESS
    resubmitted = collaboration.submit_task(
        project_id="project-demo",
        task_id=eng_task.task_id,
        actor_id="contributor-zhou",
        resource_ids=("res-loop-1", "res-loop-2"),
    )
    assert resubmitted.status is TeamTaskStatus.SUBMITTED
    verified = collaboration.review_task(
        project_id="project-demo",
        task_id=eng_task.task_id,
        actor_id="lead-lin",
        accept=True,
        note="复核通过",
    )
    assert verified.status is TeamTaskStatus.VERIFIED
    assert verified.artifact_resource_ids == ("res-loop-1", "res-loop-2")


def test_task_lifecycle_before_accept_is_frozen():
    s = seed()
    phase_tasks = drive_plan_to_tasks(s)
    eng_task = next(item for item in phase_tasks if item.target_team_id == "team-engineering")
    collaboration = s["collaboration"]
    # nothing may move a proposed task into progress directly
    with pytest.raises(PolicyDenied):
        collaboration.start_task(
            project_id="project-demo", task_id=eng_task.task_id, actor_id="contributor-zhou"
        )
    with pytest.raises(PolicyDenied):
        collaboration.submit_task(
            project_id="project-demo",
            task_id=eng_task.task_id,
            actor_id="contributor-zhou",
            resource_ids=("res-y",),
        )
        with pytest.raises(PolicyDenied):
            collaboration.review_task(
                project_id="project-demo",
                task_id=eng_task.task_id,
                actor_id="lead-lin",
                accept=True,
                note="越级验收",
            )


# ---------------------------------------------------------------------------
# Governance mode: execution gate and reject semantics
# ---------------------------------------------------------------------------
def _board_with_started_assignment(program_id="program-3", assignment_id="assignment-3"):
    board = GovernanceBoard(
        program_id=program_id,
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
        plan_id=f"{program_id}-plan",
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
        assignment_id=assignment_id,
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


def test_governance_submit_requires_artifact_and_reject_returns_to_in_progress():
    board = _board_with_started_assignment()
    from coifesp_harness.collaboration.governance import GovernanceError

    with pytest.raises(GovernanceError):
        board.submit_assignment(
            actor_id="worker-b", assignment_id="assignment-3", artifact_refs=()
        )
    board.submit_assignment(
        actor_id="worker-b",
        assignment_id="assignment-3",
        artifact_refs=("git:commit:abc",),
    )
    assert board.assignments["assignment-3"].state is AssignmentState.SUBMITTED
    # only lead/reviewer may review
    with pytest.raises(GovernanceError):
        board.review_assignment(
            actor_id="worker-b",
            assignment_id="assignment-3",
            accept=True,
            note="自审",
        )
    board.review_assignment(
        actor_id="lead-a",
        assignment_id="assignment-3",
        accept=False,
        note="需要修改",
    )
    # governance rejection loops back to IN_PROGRESS (unlike TeamTask's
    # CHANGES_REQUESTED) so the assignee can resubmit without restarting
    assert board.assignments["assignment-3"].state is AssignmentState.IN_PROGRESS
    board.submit_assignment(
        actor_id="worker-b",
        assignment_id="assignment-3",
        artifact_refs=("git:commit:def",),
    )
    board.review_assignment(
        actor_id="lead-a",
        assignment_id="assignment-3",
        accept=True,
        note="通过",
    )
    assert board.assignments["assignment-3"].state is AssignmentState.VERIFIED
