from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product import (
    DataPropagation,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    ProjectTopicStatus,
    TeamCollaborationService,
    TeamTaskStatus,
)
from coifesp_harness.product.repository import PROJECT_RESOURCES


def setup():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    values = []
    for name in ("alpha", "beta"):
        team, bootstrap = accounts.register_team(
            team_id=f"team-{name}", team_handle=f"{name}-team", team_name=name.title()
        )
        accounts.change_initial_password(
            login=bootstrap.username,
            current_password=bootstrap.initial_password,
            new_password="Admin-Correct-Horse-42!",
        )
        values.append(accounts.get_account(bootstrap.account.account_id))
    alpha, beta = values
    request = accounts.send_team_relation_request(
        request_id="relation-ab", actor_id=alpha.account_id, recipient_team_handle="beta-team"
    )
    accounts.decide_team_relation_request(
        request_id=request.request_id, actor_id=beta.account_id, accept=True
    )
    project = directory.create_project(
        project_id="project-a",
        name="A",
        description="",
        actor_id=alpha.account_id,
        owner_assignment_name="产品团队",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    directory.add_team(
        project_id=project.project_id,
        team_id=beta.team_id,
        name="工程团队",
        kind=ProjectTeamKind.ENGINEERING,
        actor_id=alpha.account_id,
    )
    return engine, accounts, TeamCollaborationService(engine), alpha, beta, project


def test_messages_are_team_to_team_and_visible_only_to_participating_sides():
    _, _, service, alpha, beta, project = setup()
    message = service.send_message(
        message_id="message-1",
        project_id=project.project_id,
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        content="请确认接口契约",
    )
    assert message.source_team_id == alpha.team_id and message.target_team_id == beta.team_id
    assert service.list_messages(project_id=project.project_id, actor_id=beta.account_id) == [
        message
    ]
    with pytest.raises(ValueError, match="another team"):
        service.send_message(
            message_id="message-self",
            project_id=project.project_id,
            actor_id=alpha.account_id,
            target_team_id=alpha.team_id,
            content="invalid",
        )


def test_team_task_lifecycle_keeps_external_owner_at_team_boundary():
    engine, accounts, service, alpha, beta, project = setup()
    pending = accounts.request_account_registration(
        account_id="beta-worker",
        username="beta-worker",
        display_name="Beta Worker",
        email="worker@beta.test",
        password="Member-Correct-Horse-42!",
        team_id=beta.team_id,
    )
    accounts.decide_account_registration(
        actor_id=beta.account_id, account_id=pending.account_id, accept=True
    )
    worker = accounts.get_account(pending.account_id)
    task = service.create_task(
        task_id="task-1",
        project_id=project.project_id,
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        title="实现接口",
        description="完成服务端实现",
        acceptance_criteria="测试通过",
    )
    assert task.status is TeamTaskStatus.PROPOSED and task.target_team_id == beta.team_id
    assert service.collaboration_inbox(actor_id=beta.account_id).actions[0].action == "respond"
    assert service.collaboration_inbox(actor_id=alpha.account_id).actions == ()
    task = service.respond_task(
        project_id=project.project_id, task_id=task.task_id, actor_id=beta.account_id, accept=True
    )
    assert service.collaboration_inbox(actor_id=beta.account_id).actions[0].action == "assign"
    with pytest.raises(GovernanceConflictError, match="internal owner"):
        service.start_task(
            project_id=project.project_id, task_id=task.task_id, actor_id=beta.account_id
        )
    service.assign_internal(
        project_id=project.project_id,
        task_id=task.task_id,
        actor_id=beta.account_id,
        account_id=worker.account_id,
    )
    assert service.collaboration_inbox(actor_id=worker.account_id).actions[0].action == "start"
    task = service.start_task(
        project_id=project.project_id, task_id=task.task_id, actor_id=worker.account_id
    )
    assert service.collaboration_inbox(actor_id=beta.account_id).actions[0].action == "submit"
    with engine.begin() as connection:
        connection.execute(
            insert(PROJECT_RESOURCES).values(
                resource_id="resource-delivery",
                project_id=project.project_id,
                owner_team_id=beta.team_id,
                created_by=worker.account_id,
                title="交付",
                artifact_owner_team_id=beta.team_id,
                artifact_id="artifact-delivery",
                artifact_sha256="b" * 64,
                media_type="text/plain",
                propagation=DataPropagation.PROJECT_READONLY.value,
                created_at=datetime.now(UTC),
            )
        )
    task = service.submit_task(
        project_id=project.project_id,
        task_id=task.task_id,
        actor_id=worker.account_id,
        resource_ids=("resource-delivery",),
    )
    review = service.collaboration_inbox(actor_id=alpha.account_id)
    assert review.actions[0].action == "review"
    assert review.actions[0].project_name == project.name
    task = service.review_task(
        project_id=project.project_id,
        task_id=task.task_id,
        actor_id=alpha.account_id,
        accept=True,
        note="验收通过",
    )
    assert task.status is TeamTaskStatus.VERIFIED
    assert service.collaboration_inbox(actor_id=alpha.account_id).actions == ()
    activities = service.list_activities(project_id=project.project_id, actor_id=beta.account_id)
    assert [item.event_type for item in activities] == [
        "task.proposed",
        "task.accepted",
        "task.assigned_internal",
        "task.started",
        "task.submitted",
        "task.verified",
    ]


def test_project_notification_cursor_is_per_account_and_ignores_own_events():
    _, accounts, service, alpha, beta, project = setup()
    service.mark_project_read(project_id=project.project_id, actor_id=alpha.account_id)
    service.mark_project_read(project_id=project.project_id, actor_id=beta.account_id)
    service.send_message(
        message_id="message-notify",
        project_id=project.project_id,
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        content="请查看",
    )
    alpha_summary = service.notification_summaries(actor_id=alpha.account_id)[0]
    beta_summary = service.notification_summaries(actor_id=beta.account_id)[0]
    assert alpha_summary.unread_count == 0
    assert beta_summary.unread_count == 1
    service.mark_project_read(project_id=project.project_id, actor_id=beta.account_id)
    assert service.notification_summaries(actor_id=beta.account_id)[0].unread_count == 0


def test_collaboration_inbox_limits_rows_without_undercounting_totals():
    _, _, service, alpha, beta, project = setup()
    for index in range(2):
        service.create_task(
            task_id=f"task-inbox-{index}",
            project_id=project.project_id,
            actor_id=alpha.account_id,
            target_team_id=beta.team_id,
            title=f"任务 {index}",
            description="",
            acceptance_criteria="完成",
        )
    inbox = service.collaboration_inbox(actor_id=beta.account_id, limit=1)
    assert inbox.action_count == 2 and len(inbox.actions) == 1
    assert inbox.unread_count == 2 and len(inbox.unread_activities) == 1
    with pytest.raises(ValueError, match="limit"):
        service.collaboration_inbox(actor_id=beta.account_id, limit=0)


def test_inbox_agent_brief_is_minimal_and_run_history_is_account_scoped():
    _, _, service, alpha, beta, project = setup()
    service.create_task(
        task_id="task-agent-inbox",
        project_id=project.project_id,
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        title="审查接口",
        description="检查接口变更",
        acceptance_criteria="兼容测试通过",
    )
    brief = json.loads(service.agent_collaboration_inbox_brief(actor_id=beta.account_id))
    assert brief["schema"] == "coifesp.collaboration-inbox-brief.v1"
    assert brief["actions"][0]["required_action"] == "respond"
    assert "created_by" not in brief["actions"][0]
    assert "assigned_account_id" not in brief["actions"][0]

    bound = service.bind_inbox_agent_run(
        run_id="run-inbox-alpha", actor_id=alpha.account_id, mode="status_briefing"
    )
    assert bound.mode.value == "status_briefing"
    assert service.list_inbox_agent_runs(actor_id=alpha.account_id) == [bound]
    assert service.list_inbox_agent_runs(actor_id=beta.account_id) == []
    service.bind_inbox_agent_run(
        run_id="run-inbox-alpha", actor_id=alpha.account_id, mode="status_briefing"
    )
    with pytest.raises(GovernanceConflictError, match="cannot change"):
        service.bind_inbox_agent_run(
            run_id="run-inbox-alpha", actor_id=alpha.account_id, mode="prioritization"
        )
    with pytest.raises(GovernanceConflictError, match="another collaboration inbox"):
        service.bind_inbox_agent_run(
            run_id="run-inbox-alpha", actor_id=beta.account_id, mode="status_briefing"
        )


def test_team_topic_discussion_requires_initiating_team_to_confirm_decision():
    _, _, service, alpha, beta, project = setup()
    topic = service.create_topic(
        topic_id="topic-architecture",
        project_id=project.project_id,
        actor_id=beta.account_id,
        title="接口版本策略",
        context="建议先保持 v1 兼容",
    )
    assert topic.status is ProjectTopicStatus.OPEN
    assert topic.proposed_by_team_id == beta.team_id and topic.origin == "human"
    contribution = service.contribute_topic(
        contribution_id="contribution-alpha",
        project_id=project.project_id,
        topic_id=topic.topic_id,
        actor_id=alpha.account_id,
        content="产品侧同意兼容窗口为两周",
    )
    assert contribution.team_id == alpha.team_id
    with pytest.raises(PolicyDenied, match="initiating team"):
        service.decide_topic(
            project_id=project.project_id,
            topic_id=topic.topic_id,
            actor_id=beta.account_id,
            decision="立即切换",
        )
    decided = service.decide_topic(
        project_id=project.project_id,
        topic_id=topic.topic_id,
        actor_id=alpha.account_id,
        decision="保留 v1 两周后切换 v2",
    )
    assert decided.status is ProjectTopicStatus.DECIDED
    assert decided.decided_by_team_id == alpha.team_id
    with pytest.raises(GovernanceConflictError, match="closed"):
        service.contribute_topic(
            contribution_id="contribution-late",
            project_id=project.project_id,
            topic_id=topic.topic_id,
            actor_id=beta.account_id,
            content="late",
        )


def test_confirmed_agent_topic_keeps_source_run_provenance():
    _, _, service, alpha, _, project = setup()
    service.bind_project_agent_run(
        project_id=project.project_id, run_id="run-project-plan", actor_id=alpha.account_id
    )
    service.assert_project_agent_run(
        project_id=project.project_id, run_id="run-project-plan", actor_id=alpha.account_id
    )
    topic = service.create_topic(
        topic_id="topic-agent",
        project_id=project.project_id,
        actor_id=alpha.account_id,
        title="Agent 生成的里程碑建议",
        context="建议分两个迭代交付",
        source_agent_run_id="run-project-plan",
    )
    assert topic.origin == "agent_confirmed"
    assert topic.source_agent_run_id == "run-project-plan"


def test_agent_collaboration_drafts_are_strict_editable_and_execute_once():
    _, _, service, alpha, beta, project = setup()
    service.bind_project_agent_run(
        project_id=project.project_id,
        run_id="run-actions",
        actor_id=alpha.account_id,
        mode="collaboration_actions",
    )
    document = (
        '{"schema":"coifesp.collaboration-actions.v1","actions":['
        '{"kind":"message","payload":{"target_team_id":"team-beta",'
        '"content":"请确认排期"}},'
        '{"kind":"task","payload":{"target_team_id":"team-beta",'
        '"title":"实现接口","description":"完成 v1",'
        '"acceptance_criteria":"测试通过"}},'
        '{"kind":"topic","payload":{"title":"发布窗口",'
        '"context":"讨论发布时间"}}]}'
    )
    drafts = service.import_action_drafts(
        project_id=project.project_id,
        actor_id=alpha.account_id,
        run_id="run-actions",
        message_sequence=2,
        content=document,
    )
    assert [item.kind.value for item in drafts] == ["message", "task", "topic"]
    updated = service.update_action_draft(
        project_id=project.project_id,
        draft_id=drafts[0].draft_id,
        actor_id=alpha.account_id,
        expected_version=1,
        payload={"target_team_id": beta.team_id, "content": "请在明天前确认排期"},
    )
    executed = service.execute_action_draft(
        project_id=project.project_id,
        draft_id=updated.draft_id,
        actor_id=alpha.account_id,
        expected_version=updated.version,
    )
    assert executed.status.value == "executed"
    assert (
        service.list_messages(project_id=project.project_id, actor_id=beta.account_id)[0].content
        == "请在明天前确认排期"
    )
    with pytest.raises(GovernanceConflictError, match="version or status"):
        service.execute_action_draft(
            project_id=project.project_id,
            draft_id=updated.draft_id,
            actor_id=alpha.account_id,
            expected_version=updated.version,
        )
    rejected = service.reject_action_draft(
        project_id=project.project_id,
        draft_id=drafts[1].draft_id,
        actor_id=alpha.account_id,
        expected_version=1,
        reason="需要重新评估范围",
    )
    assert rejected.status.value == "rejected"
    topic_draft = service.execute_action_draft(
        project_id=project.project_id,
        draft_id=drafts[2].draft_id,
        actor_id=alpha.account_id,
        expected_version=1,
    )
    assert topic_draft.executed_subject_id.startswith("topic-draft-")


def test_agent_draft_import_rejects_duplicate_json_keys_and_invalid_target():
    _, _, service, alpha, _, project = setup()
    service.bind_project_agent_run(
        project_id=project.project_id,
        run_id="run-invalid",
        actor_id=alpha.account_id,
        mode="collaboration_actions",
    )
    duplicate = (
        '{"schema":"coifesp.collaboration-actions.v1",'
        '"schema":"coifesp.collaboration-actions.v1","actions":[]}'
    )
    with pytest.raises(ValueError, match="strict JSON"):
        service.import_action_drafts(
            project_id=project.project_id,
            actor_id=alpha.account_id,
            run_id="run-invalid",
            message_sequence=1,
            content=duplicate,
        )
    outsider = (
        '{"schema":"coifesp.collaboration-actions.v1","actions":['
        '{"kind":"message","payload":{"target_team_id":"team-outsider",'
        '"content":"invalid"}}]}'
    )
    with pytest.raises(Exception, match="target project team"):
        service.import_action_drafts(
            project_id=project.project_id,
            actor_id=alpha.account_id,
            run_id="run-invalid",
            message_sequence=1,
            content=outsider,
        )


def test_analysis_agent_cannot_import_actions_or_change_registered_mode():
    _, _, service, alpha, _, project = setup()
    service.bind_project_agent_run(
        project_id=project.project_id,
        run_id="run-analysis-only",
        actor_id=alpha.account_id,
        mode="analysis",
    )
    runs = service.list_project_agent_runs(project_id=project.project_id, actor_id=alpha.account_id)
    assert [(item.run_id, item.mode.value) for item in runs] == [("run-analysis-only", "analysis")]
    with pytest.raises(GovernanceConflictError, match="mode cannot be changed"):
        service.bind_project_agent_run(
            project_id=project.project_id,
            run_id="run-analysis-only",
            actor_id=alpha.account_id,
            mode="collaboration_actions",
        )
    document = (
        '{"schema":"coifesp.collaboration-actions.v1","actions":['
        '{"kind":"topic","payload":{"title":"x","context":"y"}}]}'
    )
    with pytest.raises(PolicyDenied, match="mode does not permit"):
        service.import_action_drafts(
            project_id=project.project_id,
            actor_id=alpha.account_id,
            run_id="run-analysis-only",
            message_sequence=1,
            content=document,
        )
