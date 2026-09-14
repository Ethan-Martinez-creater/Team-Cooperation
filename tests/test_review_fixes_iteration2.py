"""Regression tests for the second agent-first review round.

Covers: switch-project state isolation (frontend is JS; backend covered by
conversation regression), conversation-generated exchange drafts, recipient
reply drafting turns (Agent drafts, human confirms), attachment-only
messages, complete-turn context alignment, idempotent plan import and
crash-safe plan materialization, and the bootstrap schema revision.
"""
import json
from pathlib import Path

from test_agent_exchanges import seed as seed_exchanges
from test_project_conversations import seed as seed_conversations

from coifesp_harness.agent_runs import DurableRunStatus
from coifesp_harness.control_plane.exchange_routes import (
    _exchange_reply_trigger_idempotency_key,
)
from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.models import (
    ConversationMessageKind,
    ExchangeRecipientStatus,
    PlanDraftStatus,
    TurnStatus,
    TurnTriggerKind,
)
from coifesp_harness.product.planning import ProjectPlanningService
from coifesp_harness.product.turn_projection import AgentTurnProjection

PLAN_OUTPUT = json.dumps(
    {
        "schema": "coifesp.project-plan.v1",
        "goals": "完成演示项目",
        "scope": "仅演示范围",
        "phases": [
            {"name": "开发", "description": "核心开发", "order": 1, "team_category": "engineering"},
            {"name": "统筹协调", "description": "跨团队协调", "order": 2},
        ],
        "milestones": [{"name": "M1", "target": "8 月中旬"}],
        "risks": [],
        "dependencies": [],
        "team_requirements": [],
        "acceptance_criteria": ["演示可运行"],
    },
    ensure_ascii=False,
)

EXCHANGE_DRAFT_OUTPUT = json.dumps(
    {
        "schema": "coifesp.exchange-draft.v1",
        "purpose": "排期确认",
        "summary": "请求工程团队确认 8 月底联调排期",
        "request": "请工程团队确认是否可以在 8 月底前完成接口联调",
        "constraints": "",
    },
    ensure_ascii=False,
)


def test_exchange_reply_retry_uses_a_fresh_message_idempotency_key():
    first = _exchange_reply_trigger_idempotency_key("exchange-1", "team-engineering")
    second = _exchange_reply_trigger_idempotency_key("exchange-1", "team-engineering")

    assert first != second
    assert first.startswith("exchange-reply:exchange-1:team-engineering:")


def test_exchange_reply_run_idempotency_is_scoped_to_the_turn():
    source = Path("src/coifesp_harness/control_plane/conversation_routes.py").read_text(
        encoding="utf-8"
    )

    assert 'f"exchange-reply:{exchange_id}:{recipient_team_id}:{turn_id}"' in source


class FakeRun:
    def __init__(self, run_id, status=DurableRunStatus.COMPLETED):
        self.run_id = run_id
        self.status = status


def reader_for(*messages):
    return lambda run: tuple(messages)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def test_message_with_attachments_only_is_accepted():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="",
        idempotency_key="proj-attach-only-1",
        attachment_resource_ids=("resource-shared",),
    )
    assert message.attachment_resource_ids == ("resource-shared",)
    stored = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )[0]
    assert stored.attachment_resource_ids == ("resource-shared",)
    assert turn.trigger_kind is TurnTriggerKind.USER_MESSAGE


def test_message_without_content_or_attachment_is_rejected():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    try:
        workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content="",
            idempotency_key="proj-empty-1",
        )
        raise AssertionError("expected empty message to be rejected")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Context alignment
# ---------------------------------------------------------------------------
def test_context_window_aligns_to_complete_turn():
    s = seed_conversations()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    for index in range(6):
        _, turn = workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content=f"问题 {index}",
            idempotency_key=f"align-{index}",
        )
        workspace.complete_turn(
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            assistant_content=f"回答 {index}",
        )
    # sequences 1..12; a 5-message window would splice mid-turn (8..12 starts
    # with an assistant reply), so one older user message is pulled in.
    window = workspace.conversation_messages_for_context(
        conversation_id=conversation.conversation_id, limit=5
    )
    assert window[0].role == "user"
    assert [item.sequence for item in window] == [7, 8, 9, 10, 11, 12]


# ---------------------------------------------------------------------------
# Recipient-side reply drafting
# ---------------------------------------------------------------------------
def _approved_exchange_to_engineering(s):
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-reply-flow",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="排期确认",
        summary="请求工程确认接口排期",
        request="请工程团队确认是否可以在 8 月底前完成联调",
        recipient_team_ids=("team-engineering",),
    )
    return exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-reply-flow",
        actor_id="lead-lin",
        expected_version=1,
    ).exchange_id


def test_recipient_draft_turn_round_trip():
    s = seed_exchanges()
    exchange = s["exchange"]
    workspace = s["workspace"]
    exchange_id = _approved_exchange_to_engineering(s)
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="contributor-zhou")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="contributor-zhou",
        content="（收到跨团队 Agent 共享请求，请起草回复草案）",
        idempotency_key="exch-draft-turn-1",
        trigger_kind=TurnTriggerKind.EXCHANGE,
    )
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-engineering", turn_id=turn.turn_id
    )
    assert exchange.recipient_for_turn(turn_id=turn.turn_id) == (
        exchange_id,
        "team-engineering",
    )
    # the unbound recipient is still pending before the draft lands
    before = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert before.status is ExchangeRecipientStatus.PENDING
    exchange.store_response_draft(
        exchange_id=exchange_id, recipient_team_id="team-engineering", content="工程确认 8 月底可行。"
    )
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.status is ExchangeRecipientStatus.DRAFTING
    assert recipient.draft_content == "工程确认 8 月底可行。"

    # a human confirms the drafted reply without retyping it
    response = exchange.submit_response(
        project_id="project-demo",
        exchange_id=exchange_id,
        actor_id="contributor-zhou",
        content="",
        turn_id=turn.turn_id,
    )
    assert response.content == "工程确认 8 月底可行。"
    submitted = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert submitted.status is ExchangeRecipientStatus.RESPONDED
    # a late projection retry must not overwrite the submitted reply
    exchange.store_response_draft(
        exchange_id=exchange_id, recipient_team_id="team-engineering", content="不应覆盖"
    )
    final = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert final.draft_content == "工程确认 8 月底可行。"


def test_exchange_turn_projects_reply_draft():
    s = seed_exchanges()
    workspace = s["workspace"]
    exchange = s["exchange"]
    exchange_id = _approved_exchange_to_engineering(s)
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="contributor-zhou")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="contributor-zhou",
        content="（收到共享请求，起草回复）",
        idempotency_key="exch-proj-1",
        trigger_kind=TurnTriggerKind.EXCHANGE,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-exch", actor_id="contributor-zhou"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-exch"
    )
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-engineering", turn_id=turn.turn_id
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        exchange=exchange,
        run_reader=reader_for({"role": "assistant", "content": "Agent 起草的回复。"}),
    )
    projection.on_run_terminal(FakeRun("run-exch"))
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.status is ExchangeRecipientStatus.DRAFTING
    assert recipient.draft_content == "Agent 起草的回复。"
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED
    # a worker retry must not duplicate the draft or corrupt the turn
    projection.on_run_terminal(FakeRun("run-exch"))
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.draft_content == "Agent 起草的回复。"


def test_failed_draft_turn_releases_recipient_for_retry():
    s = seed_exchanges()
    workspace = s["workspace"]
    exchange = s["exchange"]
    exchange_id = _approved_exchange_to_engineering(s)
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="contributor-zhou")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="contributor-zhou",
        content="（起草回复）",
        idempotency_key="exch-fail-1",
        trigger_kind=TurnTriggerKind.EXCHANGE,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-fail-1", actor_id="contributor-zhou"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-fail-1"
    )
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-engineering", turn_id=turn.turn_id
    )
    projection = AgentTurnProjection(s["engine"], workspace=workspace, exchange=exchange)
    projection.on_run_terminal(FakeRun("run-fail-1", DurableRunStatus.FAILED))
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.status is ExchangeRecipientStatus.PENDING
    assert recipient.draft_turn_id is None
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.FAILED


def test_stored_draft_releases_recipient_turn_for_redraft():
    s = seed_exchanges()
    exchange = s["exchange"]
    exchange_id = _approved_exchange_to_engineering(s)
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-engineering", turn_id="turn-redraft"
    )
    exchange.store_response_draft(
        exchange_id=exchange_id, recipient_team_id="team-engineering", content="起草内容"
    )
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.status is ExchangeRecipientStatus.DRAFTING
    assert recipient.draft_content == "起草内容"
    assert recipient.draft_turn_id is None
    # the UI can now ask for a fresh draft without tripping "already drafting"
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-engineering", turn_id="turn-redraft-2"
    )
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.draft_turn_id == "turn-redraft-2"


def test_completed_without_reply_releases_recipient_and_fails_turn():
    s = seed_exchanges()
    workspace = s["workspace"]
    exchange = s["exchange"]
    exchange_id = _approved_exchange_to_engineering(s)
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="contributor-zhou")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="contributor-zhou",
        content="（起草回复）",
        idempotency_key="exch-no-reply-1",
        trigger_kind=TurnTriggerKind.EXCHANGE,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-no-reply", actor_id="contributor-zhou"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-no-reply"
    )
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-engineering", turn_id=turn.turn_id
    )
    # A completed run that produced no assistant reply must still release the
    # recipient drafting binding so the team can retry.
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        exchange=exchange,
        run_reader=lambda run: (),
    )
    projection.on_run_terminal(FakeRun("run-no-reply", DurableRunStatus.COMPLETED))
    recipient = exchange.get_recipient(exchange_id=exchange_id, recipient_team_id="team-engineering")
    assert recipient.draft_turn_id is None
    assert recipient.status is ExchangeRecipientStatus.PENDING
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.FAILED


# ---------------------------------------------------------------------------
# Conversation-generated drafts (Agent turn, human confirms)
# ---------------------------------------------------------------------------
def test_exchange_draft_turn_projects_draft():
    s = seed_exchanges()
    workspace = s["workspace"]
    exchange = s["exchange"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="（请本团队 Agent 根据对话起草跨团队共享草稿）",
        idempotency_key="exch-draft-proj-1",
        trigger_kind=TurnTriggerKind.EXCHANGE_DRAFT,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-draft", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-draft"
    )
    intent = {
        "item_id": f"exchange-draft-intent:{turn.turn_id}",
        "content": json.dumps(
            {"recipient_team_ids": ["team-engineering"], "shared_resource_ids": []},
            ensure_ascii=False,
        ),
    }
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        exchange=exchange,
        run_reader=reader_for(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "schema": "coifesp.exchange-draft.v1",
                        "purpose": "确认接口排期",
                        "summary": "产品侧希望工程确认交付时间",
                        "request": "请工程团队确认 8 月底前完成联调",
                        "constraints": "",
                    },
                    ensure_ascii=False,
                ),
            }
        ),
        context_reader=lambda run: (intent,),
    )
    projection.on_run_terminal(FakeRun("run-draft"))
    drafts = exchange.list_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1
    assert drafts[0].status.value == "drafting"
    assert drafts[0].recipient_team_ids == ("team-engineering",)
    assert "排期" in drafts[0].purpose
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED
    # a projection retry must not import a second draft
    projection.on_run_terminal(FakeRun("run-draft"))
    assert len(exchange.list_drafts(project_id="project-demo", actor_id="lead-lin")) == 1


def test_exchange_draft_source_turn_unique_constraint():
    s = seed_exchanges()
    exchange = s["exchange"]
    exchange.create_draft(
        draft_id="draft-uniq-1",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="目的",
        summary="摘要",
        request="请求",
        recipient_team_ids=("team-engineering",),
        source_turn_id="turn-uniq-1",
    )
    # the database constraint is the last line of defence against concurrent
    # terminal callbacks producing two drafts for the same turn
    from sqlalchemy.exc import IntegrityError

    try:
        exchange.create_draft(
            draft_id="draft-uniq-2",
            project_id="project-demo",
            actor_id="lead-lin",
            purpose="目的2",
            summary="摘要2",
            request="请求2",
            recipient_team_ids=("team-engineering",),
            source_turn_id="turn-uniq-1",
        )
        raise AssertionError("expected duplicate source_turn_id to be rejected")
    except IntegrityError:
        pass
    assert exchange.find_draft_by_turn(turn_id="turn-uniq-1").draft_id == "draft-uniq-1"


def test_exchange_trigger_messages_are_system_kind():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="（请 Agent 起草共享草稿）",
        idempotency_key="sys-kind-1",
        trigger_kind=TurnTriggerKind.EXCHANGE_DRAFT,
        message_kind=ConversationMessageKind.SYSTEM,
    )
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert messages[0].message_kind is ConversationMessageKind.SYSTEM


def test_terminal_projection_via_correlation_when_unbound():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="绑定窗口测试",
        idempotency_key="corr-1",
    )
    # No bind_project_agent_run / bind_turn_run: the run finished before the
    # conversation link was written. The correlation id must heal the binding
    # and still project instead of leaving the turn active forever.

    class CorrelationRun:
        def __init__(self):
            self.run_id = "run-corr-1"
            self.status = DurableRunStatus.COMPLETED
            self.correlation_id = f"conv:{conversation.conversation_id}:turn:{turn.turn_id}"

    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        run_reader=reader_for({"role": "assistant", "content": "即使未绑定也投影"}),
    )
    projection.on_run_terminal(CorrelationRun())
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[-1].content == "即使未绑定也投影"
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED


def test_correlation_binding_parses_exchange_reply_format():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    projection = AgentTurnProjection(s["engine"], workspace=workspace)

    class ReplyRun:
        correlation_id = (
            f"exchange-reply:conv:{conversation.conversation_id}:turn:turn-reply-1"
        )

    assert len(ReplyRun.correlation_id) <= 128
    binding = projection._binding_from_correlation(ReplyRun())
    assert binding == (conversation.conversation_id, "turn-reply-1", "project-demo")

    # The verbose layout written by older builds remains recoverable.
    class LegacyReplyRun:
        correlation_id = (
            f"exchange:exchange-abc:recipient:team-engineering:"
            f"conv:{conversation.conversation_id}:turn:turn-reply-1"
        )

    binding = projection._binding_from_correlation(LegacyReplyRun())
    assert binding == (conversation.conversation_id, "turn-reply-1", "project-demo")

    # legacy user-message format keeps resolving too
    class LegacyRun:
        correlation_id = f"conv:{conversation.conversation_id}:turn:turn-legacy-1"

    binding = projection._binding_from_correlation(LegacyRun())
    assert binding == (conversation.conversation_id, "turn-legacy-1", "project-demo")


def test_correlation_binding_parses_exchange_draft_formats():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    projection = AgentTurnProjection(s["engine"], workspace=workspace)

    # runs created by builds before the conv: segment must still resolve
    class LegacyDraftRun:
        correlation_id = (
            f"exchange-draft:{conversation.conversation_id}:turn:turn-draft-legacy-1"
        )

    binding = projection._binding_from_correlation(LegacyDraftRun())
    assert binding == (conversation.conversation_id, "turn-draft-legacy-1", "project-demo")

    # the current format written by start_exchange_draft_run
    class CurrentDraftRun:
        correlation_id = (
            f"exchange-draft:conv:{conversation.conversation_id}:turn:turn-draft-1"
        )

    binding = projection._binding_from_correlation(CurrentDraftRun())
    assert binding == (conversation.conversation_id, "turn-draft-1", "project-demo")


def test_exchange_draft_terminal_projection_via_correlation_when_unbound():
    s = seed_exchanges()
    workspace = s["workspace"]
    exchange = s["exchange"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="（为跨团队 Agent 共享起草草稿）",
        idempotency_key="exch-draft-corr-1",
        trigger_kind=TurnTriggerKind.EXCHANGE_DRAFT,
    )
    # The run finished before bind_turn_run was written; the exchange-draft
    # correlation id must heal the binding and project the draft anyway.
    intent = {
        "item_id": f"exchange-draft-intent:{turn.turn_id}",
        "content": json.dumps(
            {
                "schema": "coifesp.exchange-draft-intent.v1",
                "turn_id": turn.turn_id,
                "recipient_team_ids": ["team-engineering"],
                "shared_resource_ids": [],
            },
            ensure_ascii=False,
        ),
    }

    class DraftRun:
        def __init__(self):
            self.run_id = "run-exch-draft-corr"
            self.status = DurableRunStatus.COMPLETED
            self.correlation_id = (
                f"exchange-draft:conv:{conversation.conversation_id}:turn:{turn.turn_id}"
            )

    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        exchange=exchange,
        run_reader=reader_for({"role": "assistant", "content": EXCHANGE_DRAFT_OUTPUT}),
        context_reader=lambda run: [intent],
    )
    projection.on_run_terminal(DraftRun())
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED
    draft = exchange.find_draft_by_turn(turn_id=turn.turn_id)
    assert draft is not None
    assert draft.status.value == "drafting"
    assert "排期" in draft.purpose


def test_generate_rejects_unowned_conversation():
    s = seed_exchanges()
    exchange = s["exchange"]
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    # lead's conversation must not be usable by engineering as the source
    try:
        exchange.assert_conversation_owned(
            project_id="project-demo",
            conversation_id=conversation.conversation_id,
            actor_id="contributor-zhou",
        )
        raise AssertionError("expected cross-account conversation to be rejected")
    except ResourceNotFound:
        pass
    try:
        exchange.assert_conversation_owned(
            project_id="project-demo",
            conversation_id="conv-missing",
            actor_id="lead-lin",
        )
        raise AssertionError("expected missing conversation to be rejected")
    except ResourceNotFound:
        pass


def test_exchange_stays_open_while_another_recipient_drafts():
    s = seed_exchanges()
    exchange = s["exchange"]
    draft = exchange.create_draft(
        draft_id="draft-multi-open",
        project_id="project-demo",
        actor_id="lead-lin",
        purpose="排期确认",
        summary="请求两团队确认",
        request="确认排期",
        recipient_team_ids=("team-engineering", "team-quality"),
    )
    result = exchange.approve_draft(
        project_id="project-demo",
        draft_id="draft-multi-open",
        actor_id="lead-lin",
        expected_version=1,
    )
    exchange_id = result.exchange_id
    # engineering responds while quality is still drafting: the exchange must
    # stay sent, not jump to responded.
    exchange.submit_response(
        project_id="project-demo",
        exchange_id=exchange_id,
        actor_id="contributor-zhou",
        content="工程已确认。",
    )
    exchange.record_response_draft_turn(
        exchange_id=exchange_id, recipient_team_id="team-quality", turn_id="turn-q"
    )
    exchange.store_response_draft(
        exchange_id=exchange_id, recipient_team_id="team-quality", content="质量正在起草"
    )
    latest = exchange.get_exchange(
        project_id="project-demo", exchange_id=exchange_id, actor_id="lead-lin"
    )
    assert latest.status.value == "sent"
    # once quality confirms, the exchange aggregates to responded
    exchange.submit_response(
        project_id="project-demo",
        exchange_id=exchange_id,
        actor_id="reviewer-su",
        content="质量确认。",
        turn_id="turn-q",
    )
    latest = exchange.get_exchange(
        project_id="project-demo", exchange_id=exchange_id, actor_id="lead-lin"
    )
    assert latest.status.value == "responded"


# ---------------------------------------------------------------------------
# Planning reliability
# ---------------------------------------------------------------------------
def test_plan_import_is_idempotent_per_run():
    s = seed_conversations()
    planning = ProjectPlanningService(s["engine"])
    first = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-import-idem",
        source_turn_id="turn-idem",
    )
    second = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-import-idem",
        source_turn_id="turn-idem",
    )
    assert first.draft_id == second.draft_id
    drafts = planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1


def test_plan_projection_imports_before_complete_on_retry():
    """A retry after a crash between import and completion must not duplicate."""
    s = seed_conversations()
    workspace = s["workspace"]
    planning = ProjectPlanningService(
        s["engine"], collaboration=TeamCollaborationService(s["engine"])
    )
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请给出项目计划",
        idempotency_key="replay-plan-1",
        trigger_kind=TurnTriggerKind.PLANNING,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-replay-plan", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-replay-plan",
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        planning=planning,
        run_reader=reader_for({"role": "assistant", "content": PLAN_OUTPUT}),
    )
    projection.on_run_terminal(FakeRun("run-replay-plan"))
    drafts = planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1
    assert drafts[0].source_run_id == "run-replay-plan"
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED
    # racing projection retries (turn already completed) stay a no-op
    projection.on_run_terminal(FakeRun("run-replay-plan"))
    assert len(planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 1


def test_plan_approval_projection_is_crash_safe_and_idempotent():
    s = seed_conversations()
    collaboration = TeamCollaborationService(s["engine"])
    planning = ProjectPlanningService(s["engine"], collaboration=collaboration)
    draft = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-plan-crash",
    )
    # Simulate a crash after partial projection but before the final status:
    # the draft is still DRAFTING and a retry must not duplicate objects.
    planning._project_plan_topic(project_id="project-demo", draft=draft, actor_id="lead-lin")
    planning._materialize_plan(project_id="project-demo", draft=draft, actor_id="lead-lin")
    approved = planning.approve_plan_draft(
        project_id="project-demo", draft_id=draft.draft_id, actor_id="lead-lin"
    )
    assert approved.status is PlanDraftStatus.APPROVED
    topics = collaboration.list_topics(project_id="project-demo", actor_id="lead-lin")
    tasks = collaboration.list_tasks(project_id="project-demo", actor_id="lead-lin")
    assert len([t for t in topics if t.title.startswith("里程碑")]) == 1
    assert len([t for t in topics if t.title.startswith("项目计划确认")]) == 1
    assert len([t for t in topics if t.title.startswith("阶段建议")]) == 1
    assert len([t for t in tasks if t.title.startswith("阶段执行")]) == 1
    # approving again is rejected because the draft already closed
    try:
        planning.approve_plan_draft(
            project_id="project-demo", draft_id=draft.draft_id, actor_id="lead-lin"
        )
        raise AssertionError("expected second approval to be rejected")
    except GovernanceConflictError:
        pass


def test_plan_source_run_converges_to_one_draft():
    s = seed_conversations()
    planning = ProjectPlanningService(s["engine"])
    planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-unique-db",
        source_turn_id="turn-uniq",
    )
    # A projection retry for the same run must converge on the existing draft
    # (unique source_run_id is the last line of defence).
    second = planning.import_plan_draft(
        project_id="project-demo",
        actor_id="lead-lin",
        content=PLAN_OUTPUT,
        source_run_id="run-unique-db",
        source_turn_id="turn-uniq",
    )
    assert second.source_run_id == "run-unique-db"
    drafts = planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1


def test_duplicate_key_detection_only_for_unique_violations():
    import sqlite3

    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    from coifesp_harness.product.planning import _is_duplicate_key_error

    assert _is_duplicate_key_error(
        SAIntegrityError(
            "stmt", "params", sqlite3.IntegrityError("UNIQUE constraint failed: x.y")
        )
    )
    assert _is_duplicate_key_error(
        SAIntegrityError(
            "stmt", "params", sqlite3.IntegrityError("duplicate key value violates unique constraint")
        )
    )
    # foreign-key and other constraint failures must NOT be swallowed
    assert not _is_duplicate_key_error(
        SAIntegrityError(
            "stmt", "params", sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        )
    )
    assert not _is_duplicate_key_error(
        SAIntegrityError(
            "stmt", "params", sqlite3.IntegrityError("NOT NULL constraint failed: x.y")
        )
    )


# ---------------------------------------------------------------------------
# Startup replay recovery
# ---------------------------------------------------------------------------
def test_replay_pending_recovers_interrupted_projection():
    s = seed_exchanges()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请分析风险",
        idempotency_key="replay-1",
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-replay", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-replay"
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        run_reader=reader_for({"role": "assistant", "content": "补上的回复"}),
    )

    class ReplayService:
        # A crash between the run finishing and the projection writing the
        # result: the run reports a terminal state and the turn is still
        # active, exactly what a startup scan is expected to find.
        def get(self, *, principal, run_id):
            from types import SimpleNamespace

            return SimpleNamespace(
                run_id="run-replay",
                status=DurableRunStatus.COMPLETED,
                owner_principal_id="lead-lin",
                tenant_id="team-product",
            )

    assert projection.replay_pending(ReplayService()) == 1
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[-1].content == "补上的回复"
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED
    # a second scan finds nothing left to replay
    assert projection.replay_pending(ReplayService()) == 0


def test_replay_finds_unbound_run_via_correlation():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import insert

    from coifesp_harness.agent_runs.repository import AGENT_RUNS

    s = seed_exchanges()
    AGENT_RUNS.metadata.create_all(s["engine"])
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="未绑定重放",
        idempotency_key="unbound-1",
    )
    # The run was never bound (no bind_turn_run): the correlation id is the
    # only tie between the durable run and the conversation/turn.
    now = datetime.now(UTC)
    with s["engine"].begin() as connection:
        connection.execute(
            insert(AGENT_RUNS).values(
                tenant_id="team-product",
                run_id="run-unbound-1",
                owner_principal_id="lead-lin",
                correlation_id=f"conv:{conversation.conversation_id}:turn:{turn.turn_id}",
                idempotency_key="conv-turn-unbound",
                request_digest="0" * 64,
                status="completed",
                version=1,
                turns=1,
                tool_calls=0,
                total_tokens=0,
                model_cost_microusd=0,
                failure_count=0,
                max_failures=3,
                checkpoint_ciphertext=b"x",
                checkpoint_nonce=b"y",
                checkpoint_fingerprint="0" * 64,
                checkpoint_key_id="k",
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        run_reader=reader_for({"role": "assistant", "content": "未绑定的回复"}),
    )

    class ReplayService:
        def get(self, *, principal, run_id):
            # Mirrors a real DurableAgentRun: the correlation id carries the
            # conversation/turn segment that heal_binding resolves.
            return SimpleNamespace(
                run_id="run-unbound-1",
                status=DurableRunStatus.COMPLETED,
                correlation_id=f"conv:{conversation.conversation_id}:turn:{turn.turn_id}",
            )

    assert projection.replay_pending(ReplayService()) == 1
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert messages[-1].content == "未绑定的回复"
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED


def test_replay_finds_unbound_exchange_draft_run_via_correlation():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import insert

    from coifesp_harness.agent_runs.repository import AGENT_RUNS

    s = seed_exchanges()
    AGENT_RUNS.metadata.create_all(s["engine"])
    workspace = s["workspace"]
    exchange = s["exchange"]
    conv_legacy = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn_legacy = workspace.append_user_message(
        conversation_id=conv_legacy.conversation_id,
        actor_id="lead-lin",
        content="（起草共享草稿-旧格式 run）",
        idempotency_key="exch-draft-unbound-legacy",
        trigger_kind=TurnTriggerKind.EXCHANGE_DRAFT,
    )
    conv_new = workspace.ensure_conversation(project_id="project-demo", actor_id="contributor-zhou")
    _, turn_new = workspace.append_user_message(
        conversation_id=conv_new.conversation_id,
        actor_id="contributor-zhou",
        content="（起草共享草稿-新格式 run）",
        idempotency_key="exch-draft-unbound-new",
        trigger_kind=TurnTriggerKind.EXCHANGE_DRAFT,
    )
    now = datetime.now(UTC)
    runs = {
        "run-unbound-draft-legacy": {
            "tenant_id": "team-product",
            "owner_principal_id": "lead-lin",
            "turn_id": turn_legacy.turn_id,
            "correlation_id": (
                f"exchange-draft:{conv_legacy.conversation_id}:turn:{turn_legacy.turn_id}"
            ),
        },
        "run-unbound-draft-new": {
            "tenant_id": "team-engineering",
            "owner_principal_id": "contributor-zhou",
            "turn_id": turn_new.turn_id,
            "correlation_id": (
                f"exchange-draft:conv:{conv_new.conversation_id}:turn:{turn_new.turn_id}"
            ),
        },
    }
    with s["engine"].begin() as connection:
        for run_id, meta in runs.items():
            connection.execute(
                insert(AGENT_RUNS).values(
                    tenant_id=meta["tenant_id"],
                    run_id=run_id,
                    owner_principal_id=meta["owner_principal_id"],
                    correlation_id=meta["correlation_id"],
                    idempotency_key=f"corr-{run_id}",
                    request_digest="0" * 64,
                    status="completed",
                    version=1,
                    turns=1,
                    tool_calls=0,
                    total_tokens=0,
                    model_cost_microusd=0,
                    failure_count=0,
                    max_failures=3,
                    checkpoint_ciphertext=b"x",
                    checkpoint_nonce=b"y",
                    checkpoint_fingerprint="0" * 64,
                    checkpoint_key_id="k",
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                )
            )
    conversation_by_turn = {
        turn_legacy.turn_id: conv_legacy,
        turn_new.turn_id: conv_new,
    }
    recipient_by_turn = {
        turn_legacy.turn_id: ("team-engineering",),
        turn_new.turn_id: ("team-product",),
    }
    intents = {
        turn_id: {
            "item_id": f"exchange-draft-intent:{turn_id}",
            "content": json.dumps(
                {
                    "schema": "coifesp.exchange-draft-intent.v1",
                    "turn_id": turn_id,
                    "recipient_team_ids": list(recipient_by_turn[turn_id]),
                    "shared_resource_ids": [],
                },
                ensure_ascii=False,
            ),
        }
        for turn_id in (turn_legacy.turn_id, turn_new.turn_id)
    }
    run_to_turn = {run_id: meta["turn_id"] for run_id, meta in runs.items()}

    class ReplayService:
        def get(self, *, principal, run_id):
            return SimpleNamespace(
                run_id=run_id,
                status=DurableRunStatus.COMPLETED,
                correlation_id=runs[run_id]["correlation_id"],
            )

    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        exchange=exchange,
        run_reader=reader_for({"role": "assistant", "content": EXCHANGE_DRAFT_OUTPUT}),
        context_reader=lambda run: [intents[run_to_turn[run.run_id]]],
    )
    assert projection.replay_pending(ReplayService()) == 2
    for turn in (turn_legacy, turn_new):
        done = workspace.get_turn(
            conversation_id=conversation_by_turn[turn.turn_id].conversation_id,
            turn_id=turn.turn_id,
        )
        assert done.status is TurnStatus.COMPLETED
        draft = exchange.find_draft_by_turn(turn_id=turn.turn_id)
        assert draft is not None
        assert draft.status.value == "drafting"


# ---------------------------------------------------------------------------
# Bootstrap schema revision
# ---------------------------------------------------------------------------
def test_bootstrap_schema_revision_matches_latest_migration():
    from alembic.script import ScriptDirectory

    from coifesp_harness.control_plane.bootstrap import SCHEMA_REVISION

    script = ScriptDirectory("alembic")
    heads = script.get_heads()
    assert SCHEMA_REVISION == sorted(heads)[-1]
