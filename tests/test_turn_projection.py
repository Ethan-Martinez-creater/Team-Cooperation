"""Terminal Run projection: assistant replies, failure termination, planning."""
import json

from coifesp_harness.agent_runs import DurableRunStatus
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.models import PlanDraftStatus, TurnStatus, TurnTriggerKind
from coifesp_harness.product.planning import ProjectPlanningService
from coifesp_harness.product.turn_projection import AgentTurnProjection

from test_project_conversations import seed

PLAN_OUTPUT = json.dumps(
    {
        "schema": "coifesp.project-plan.v1",
        "goals": "完成演示项目",
        "scope": "仅演示范围",
        "phases": [
            {"name": "开发", "description": "核心开发", "order": 1, "team_category": "engineering"}
        ],
        "milestones": [{"name": "M1", "target": "8 月中旬"}],
        "risks": [],
        "dependencies": [],
        "team_requirements": [{"team_category": "engineering", "count": 2, "rationale": "人力"}],
        "acceptance_criteria": ["演示可运行"],
    },
    ensure_ascii=False,
)


class FakeRun:
    def __init__(self, run_id, status=DurableRunStatus.COMPLETED):
        self.run_id = run_id
        self.status = status


def reader_for(*messages):
    return lambda run: tuple(messages)


def test_completed_run_projects_assistant_reply_once():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请分析风险",
        idempotency_key="proj-1",
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-1", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        run_id="run-1",
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        run_reader=reader_for({"role": "assistant", "content": "风险分析如下……"}),
    )
    projection.on_run_terminal(FakeRun("run-1", DurableRunStatus.COMPLETED))
    messages = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[-1].content == "风险分析如下……"
    done = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert done.status is TurnStatus.COMPLETED
    # worker retry must not duplicate the reply
    projection.on_run_terminal(FakeRun("run-1", DurableRunStatus.COMPLETED))
    again = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )
    assert [item.role for item in again] == ["user", "assistant"]


def test_failed_run_terminates_turn_and_allows_retry():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="会失败的消息",
        idempotency_key="proj-fail-1",
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-fail", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-fail"
    )
    projection = AgentTurnProjection(s["engine"], workspace=workspace)
    projection.on_run_terminal(FakeRun("run-fail", DurableRunStatus.FAILED))
    failed = workspace.get_turn(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id
    )
    assert failed.status is TurnStatus.FAILED
    # the user message is kept and a new turn can be created
    retried, retry_turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="重试的消息",
        idempotency_key="proj-fail-2",
    )
    assert retry_turn.user_message_sequence == retried.sequence


def test_user_message_and_turn_share_turn_id():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    message, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="双向关联",
        idempotency_key="proj-link-1",
    )
    assert message.turn_id == turn.turn_id
    # the stored user message carries the same turn id
    stored = workspace.list_messages(
        conversation_id=conversation.conversation_id, actor_id="lead-lin"
    )[0]
    assert stored.turn_id == turn.turn_id


def test_planning_turn_auto_imports_plan_exactly_once():
    s = seed()
    workspace = s["workspace"]
    planning = ProjectPlanningService(
        s["engine"], collaboration=TeamCollaborationService(s["engine"])
    )
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    _, turn = workspace.append_user_message(
        conversation_id=conversation.conversation_id,
        actor_id="lead-lin",
        content="请给出项目计划",
        idempotency_key="proj-plan-1",
        trigger_kind=TurnTriggerKind.PLANNING,
    )
    s["collaboration"].bind_project_agent_run(
        project_id="project-demo", run_id="run-plan", actor_id="lead-lin"
    )
    workspace.bind_turn_run(
        conversation_id=conversation.conversation_id, turn_id=turn.turn_id, run_id="run-plan"
    )
    projection = AgentTurnProjection(
        s["engine"],
        workspace=workspace,
        planning=planning,
        run_reader=reader_for({"role": "assistant", "content": PLAN_OUTPUT}),
    )
    projection.on_run_terminal(FakeRun("run-plan", DurableRunStatus.COMPLETED))
    drafts = planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")
    assert len(drafts) == 1
    assert drafts[0].status is PlanDraftStatus.DRAFTING
    # repeated projection must not import a second draft
    projection.on_run_terminal(FakeRun("run-plan", DurableRunStatus.COMPLETED))
    assert len(planning.list_plan_drafts(project_id="project-demo", actor_id="lead-lin")) == 1


def test_conversation_history_returns_chronological_messages():
    s = seed()
    workspace = s["workspace"]
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    for index in range(5):
        message, turn = workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content=f"问题 {index}",
            idempotency_key=f"proj-hist-{index}",
        )
        workspace.complete_turn(
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            assistant_content=f"回答 {index}",
        )
    history = workspace.conversation_messages_for_context(
        conversation_id=conversation.conversation_id, limit=20
    )
    assert [item.sequence for item in history] == list(range(1, 11))
    # the limit truncates to the most recent messages
    short = workspace.conversation_messages_for_context(
        conversation_id=conversation.conversation_id, limit=4
    )
    assert [item.sequence for item in short] == [7, 8, 9, 10]