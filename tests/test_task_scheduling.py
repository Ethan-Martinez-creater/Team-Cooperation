from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product import (
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TaskPriority,
    TeamCollaborationService,
    TeamTaskStatus,
    compute_team_task_schedule,
    task_due_in_seconds,
    task_is_overdue,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def setup():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    notifier = NotificationService(engine)
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
    return engine, accounts, TeamCollaborationService(engine, notifier=notifier), alpha, beta, project


def create_scheduled_task(service, *, actor, target, task_id="task-1", priority=TaskPriority.HIGH,
                          due_at=None, title="交付接口适配"):
    return service.create_task(
        task_id=task_id,
        project_id="project-a",
        actor_id=actor.account_id,
        target_team_id=target.team_id,
        title=title,
        description="",
        acceptance_criteria="接口联调通过",
        priority=priority,
        due_at=due_at,
    )


# --------------------------------------------------------------------------- unit


def test_task_is_overdue_boundaries():
    due = NOW + timedelta(hours=1)
    assert task_is_overdue(TeamTaskStatus.IN_PROGRESS, due, NOW) is False
    assert task_is_overdue(TeamTaskStatus.IN_PROGRESS, NOW, NOW) is False
    assert task_is_overdue(TeamTaskStatus.IN_PROGRESS, NOW - timedelta(seconds=1), NOW) is True
    assert task_is_overdue(TeamTaskStatus.VERIFIED, NOW - timedelta(days=1), NOW) is False
    assert task_is_overdue(TeamTaskStatus.REJECTED, NOW - timedelta(days=1), NOW) is False
    assert task_is_overdue(TeamTaskStatus.IN_PROGRESS, None, NOW) is False


def test_task_due_in_seconds_matches_distance():
    assert task_due_in_seconds(None, NOW) is None
    assert task_due_in_seconds(NOW + timedelta(minutes=10), NOW) == 600
    assert task_due_in_seconds(NOW - timedelta(minutes=10), NOW) == -600


def test_compute_schedule_marks_due_soon_inside_window():
    inside = compute_team_task_schedule(
        status=TeamTaskStatus.ACCEPTED, priority=TaskPriority.HIGH,
        due_at=NOW + timedelta(hours=47), schedule_version=1, now=NOW, due_soon_hours=48,
    )
    assert inside.is_due_soon and not inside.is_overdue
    outside = compute_team_task_schedule(
        status=TeamTaskStatus.ACCEPTED, priority=TaskPriority.HIGH,
        due_at=NOW + timedelta(hours=49), schedule_version=1, now=NOW, due_soon_hours=48,
    )
    assert not outside.is_due_soon
    overdue = compute_team_task_schedule(
        status=TeamTaskStatus.ACCEPTED, priority=TaskPriority.HIGH,
        due_at=NOW - timedelta(hours=1), schedule_version=1, now=NOW, due_soon_hours=48,
    )
    assert overdue.is_overdue and not overdue.is_due_soon


def test_due_at_validation_rejects_past_naive_and_far_future():
    engine, accounts, service, alpha, beta, project = setup()
    with pytest.raises(ValueError):
        create_scheduled_task(service, actor=alpha, target=beta, due_at=datetime(2020, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        create_scheduled_task(
            service, actor=alpha, target=beta, due_at=datetime(2030, 1, 1)
        )
    with pytest.raises(ValueError):
        create_scheduled_task(
            service, actor=alpha, target=beta,
            due_at=datetime.now(UTC) + timedelta(days=400),
        )
    task = create_scheduled_task(
        service, actor=alpha, target=beta,
        due_at=datetime.now(UTC) + timedelta(days=30),
    )
    assert task.due_at is not None and task.schedule_version == 1


# ------------------------------------------------------------------------ service


def test_source_team_directly_edits_unaccepted_task_schedule():
    engine, accounts, service, alpha, beta, project = setup()
    task = create_scheduled_task(service, actor=alpha, target=beta)
    new_due = datetime.now(UTC) + timedelta(days=10)
    outcome, value = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.URGENT, due_at=new_due, clear_due_at=False,
        expected_schedule_version=1, reason="",
    )
    assert outcome == "updated"
    assert value.priority is TaskPriority.URGENT
    assert value.due_at == new_due
    assert value.schedule_version == 2
    activities = service.list_activities(project_id="project-a", actor_id=alpha.account_id)
    assert any(item.event_type == "task_schedule_set" for item in activities)
    assert any(item.event_type == "task_schedule_changed" for item in activities)


def test_target_team_cannot_directly_edit_unaccepted_task():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=30))
    # The target team may not silently edit; before acceptance it still goes
    # through a proposal that the source team decides.
    outcome, proposal = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=beta.account_id,
        priority=TaskPriority.LOW, due_at=None, clear_due_at=False,
        expected_schedule_version=1, reason="希望放宽排期",
    )
    assert outcome == "proposed" and proposal.proposed_by_team_id == beta.team_id
    task = service.list_tasks(project_id="project-a", actor_id=alpha.account_id)[0]
    assert task.priority is not TaskPriority.LOW and task.schedule_version == 1


def test_accepted_task_requires_proposal_and_decide():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=30))
    service.respond_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                         accept=True)
    later = datetime.now(UTC) + timedelta(days=60)
    # The source team cannot silently move an accepted deadline; the same call
    # returns a pending proposal that the target team must decide.
    outcome, value = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=later, clear_due_at=False,
        expected_schedule_version=1, reason="",
    )
    assert outcome == "proposed"
    assert service.list_tasks(project_id="project-a", actor_id=alpha.account_id)[0].due_at != later
    # A replay of the same content collapses onto the same pending proposal.
    outcome, replay = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=later, clear_due_at=False,
        expected_schedule_version=1, reason="",
    )
    assert outcome == "proposed" and replay.proposal_id == value.proposal_id
    proposal = value
    task_before = service.list_tasks(project_id="project-a", actor_id=alpha.account_id)[0]
    assert task_before.due_at != later
    decided = service.decide_schedule_proposal(
        project_id="project-a", task_id="task-1", proposal_id=proposal.proposal_id,
        actor_id=beta.account_id, accept=True, reason="同意调整",
        expected_proposal_version=1,
    )
    assert decided.status.value == "accepted"
    task_after = service.list_tasks(project_id="project-a", actor_id=alpha.account_id)[0]
    assert task_after.due_at == later
    assert task_after.schedule_version == 2
    activities = service.list_activities(project_id="project-a", actor_id=alpha.account_id)
    assert any(item.event_type == "task_schedule_proposed" for item in activities)
    assert any(item.event_type == "task_schedule_changed" for item in activities)


def test_rejected_proposal_keeps_original_deadline():
    engine, accounts, service, alpha, beta, project = setup()
    original = datetime.now(UTC) + timedelta(days=30)
    create_scheduled_task(service, actor=alpha, target=beta, due_at=original)
    service.respond_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                         accept=True)
    outcome, proposal = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=datetime.now(UTC) + timedelta(days=5),
        clear_due_at=False, expected_schedule_version=1, reason="希望提前",
    )
    assert outcome == "proposed"
    service.decide_schedule_proposal(
        project_id="project-a", task_id="task-1", proposal_id=proposal.proposal_id,
        actor_id=beta.account_id, accept=False, reason="排期无法提前",
        expected_proposal_version=1,
    )
    task = service.list_tasks(project_id="project-a", actor_id=alpha.account_id)[0]
    assert task.due_at == original and task.schedule_version == 1


def test_shortened_deadline_requires_reason():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=30))
    with pytest.raises(ValueError):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
            priority=TaskPriority.HIGH, due_at=datetime.now(UTC) + timedelta(days=3),
            clear_due_at=False, expected_schedule_version=1, reason="",
        )
    outcome, _ = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.HIGH, due_at=datetime.now(UTC) + timedelta(days=3),
        clear_due_at=False, expected_schedule_version=1, reason="外部阻塞解除",
    )
    assert outcome == "updated"


def test_stale_schedule_version_conflicts():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta)
    service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.HIGH, due_at=datetime.now(UTC) + timedelta(days=9),
        clear_due_at=False, expected_schedule_version=1, reason="",
    )
    with pytest.raises(GovernanceConflictError):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
            priority=TaskPriority.LOW, due_at=None, clear_due_at=False,
            expected_schedule_version=1, reason="",
        )


def test_duplicate_proposal_is_idempotent_and_new_proposal_supersedes():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=30))
    service.respond_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                         accept=True)
    later = datetime.now(UTC) + timedelta(days=90)
    _, first = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=later, clear_due_at=False,
        expected_schedule_version=1, reason="第一次调整",
    )
    _, replay = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=later, clear_due_at=False,
        expected_schedule_version=1, reason="第一次调整",
    )
    assert replay.proposal_id == first.proposal_id
    proposals = service.list_schedule_proposals(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id
    )
    assert len(proposals) == 1
    _, second = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=datetime.now(UTC) + timedelta(days=120),
        clear_due_at=False, expected_schedule_version=1, reason="再调整",
    )
    proposals = {
        item.proposal_id: item
        for item in service.list_schedule_proposals(
            project_id="project-a", task_id="task-1", actor_id=alpha.account_id
        )
    }
    assert proposals[first.proposal_id].status.value == "superseded"
    assert proposals[second.proposal_id].status.value == "pending"


def test_duplicate_decide_conflicts_and_proposer_cannot_decide():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=30))
    service.respond_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                         accept=True)
    _, proposal = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=datetime.now(UTC) + timedelta(days=60),
        clear_due_at=False, expected_schedule_version=1, reason="调整",
    )
    with pytest.raises(PolicyDenied):
        service.decide_schedule_proposal(
            project_id="project-a", task_id="task-1", proposal_id=proposal.proposal_id,
            actor_id=alpha.account_id, accept=True, reason="",
            expected_proposal_version=1,
        )
    service.decide_schedule_proposal(
        project_id="project-a", task_id="task-1", proposal_id=proposal.proposal_id,
        actor_id=beta.account_id, accept=True, reason="", expected_proposal_version=1,
    )
    with pytest.raises(GovernanceConflictError):
        service.decide_schedule_proposal(
            project_id="project-a", task_id="task-1", proposal_id=proposal.proposal_id,
            actor_id=beta.account_id, accept=True, reason="",
            expected_proposal_version=2,
        )


def test_non_participant_cannot_change_schedule():
    from coifesp_harness.errors import ResourceNotFound

    engine, accounts, service, alpha, beta, project = setup()
    gamma, gamma_bootstrap = accounts.register_team(
        team_id="team-gamma", team_handle="gamma-team", team_name="Gamma"
    )
    accounts.change_initial_password(
        login=gamma_bootstrap.username, current_password=gamma_bootstrap.initial_password,
        new_password="Admin-Correct-Horse-42!",
    )
    outsider = accounts.get_account(gamma_bootstrap.account.account_id)
    create_scheduled_task(service, actor=alpha, target=beta)
    with pytest.raises(ResourceNotFound):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=outsider.account_id,
            priority=TaskPriority.LOW, due_at=None, clear_due_at=False,
            expected_schedule_version=1, reason="",
        )


def test_verified_task_records_completion_and_never_overdue():
    from coifesp_harness.product import DataPropagation
    from coifesp_harness.product.repository import PROJECT_RESOURCES
    from sqlalchemy import insert

    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=1))
    service.respond_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                         accept=True)
    service.assign_internal(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                            account_id=beta.account_id)
    service.start_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id)
    with engine.begin() as connection:
        connection.execute(
            insert(PROJECT_RESOURCES).values(
                resource_id="resource-delivery",
                project_id="project-a",
                owner_team_id=beta.team_id,
                created_by=beta.account_id,
                title="交付",
                artifact_owner_team_id=beta.team_id,
                artifact_id="artifact-delivery",
                artifact_sha256="b" * 64,
                media_type="text/plain",
                propagation=DataPropagation.PROJECT_READONLY.value,
                created_at=datetime.now(UTC),
            )
        )
    task = service.submit_task(project_id="project-a", task_id="task-1",
                               actor_id=beta.account_id,
                               resource_ids=("resource-delivery",))
    assert task.due_at is not None and task.completed_at is None
    task = service.review_task(project_id="project-a", task_id="task-1",
                               actor_id=alpha.account_id, accept=True, note="验收通过")
    assert task.status is TeamTaskStatus.VERIFIED
    assert task.completed_at is not None
    assert task_is_overdue(task.status, task.due_at, task.due_at + timedelta(days=5)) is False
    with pytest.raises(PolicyDenied):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
            priority=TaskPriority.LOW, due_at=None, clear_due_at=False,
            expected_schedule_version=task.schedule_version, reason="",
        )


def test_terminal_task_rejects_schedule_change():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta)
    service.respond_task(project_id="project-a", task_id="task-1", actor_id=beta.account_id,
                         accept=False)
    with pytest.raises(PolicyDenied):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
            priority=TaskPriority.LOW, due_at=datetime.now(UTC) + timedelta(days=2),
            clear_due_at=False, expected_schedule_version=1, reason="",
        )


def test_inbox_filters_and_default_ordering():
    engine, accounts, service, alpha, beta, project = setup()
    now = datetime.now(UTC)
    create_scheduled_task(service, actor=alpha, target=beta, task_id="task-urgent",
                          priority=TaskPriority.URGENT,
                          due_at=now + timedelta(hours=2))
    create_scheduled_task(service, actor=alpha, target=beta, task_id="task-normal",
                          priority=TaskPriority.NORMAL, due_at=None)
    overdue_row = create_scheduled_task(service, actor=alpha, target=beta, task_id="task-late",
                                        priority=TaskPriority.LOW,
                                        due_at=now + timedelta(days=20))
    # Force a past deadline through a direct edit of an unaccepted task.
    service.change_task_schedule(
        project_id="project-a", task_id="task-late", actor_id=alpha.account_id,
        priority=TaskPriority.LOW, due_at=now + timedelta(days=1), clear_due_at=False,
        expected_schedule_version=1, reason="校准逾期样例",
    )
    with engine.begin() as connection:
        from coifesp_harness.product.repository import TEAM_TASKS
        from sqlalchemy import update
        connection.execute(
            update(TEAM_TASKS).where(TEAM_TASKS.c.task_id == "task-late").values(
                due_at=now - timedelta(days=1))
        )
    inbox = service.collaboration_inbox(actor_id=beta.account_id)
    titles = [item.task.task_id for item in inbox.actions]
    assert titles[0] == "task-late"
    assert titles[1] == "task-urgent"
    overdue = service.collaboration_inbox(actor_id=beta.account_id, overdue_only=True)
    assert [item.task.task_id for item in overdue.actions] == ["task-late"]
    soon = service.collaboration_inbox(actor_id=beta.account_id, due_within_hours=48)
    assert [item.task.task_id for item in soon.actions] == ["task-urgent"]
    assigned = service.collaboration_inbox(actor_id=beta.account_id, assigned_only=True)
    assert assigned.actions == ()
    high_only = service.collaboration_inbox(actor_id=beta.account_id, priority=TaskPriority.URGENT)
    assert [item.task.task_id for item in high_only.actions] == ["task-urgent"]


def test_inbox_agent_brief_contains_schedule_fields():
    engine, accounts, service, alpha, beta, project = setup()
    create_scheduled_task(service, actor=alpha, target=beta,
                          priority=TaskPriority.URGENT,
                          due_at=datetime.now(UTC) + timedelta(hours=3))
    brief = service.agent_collaboration_inbox_brief(actor_id=beta.account_id)
    assert '"priority":"urgent"' in brief
    assert '"is_overdue":false' in brief
    assert '"due_in_seconds"' in brief
    assert '"ordering_rule"' in brief
    assert alpha.account_id not in brief


def test_same_priority_sorts_by_updated_time_stably():
    engine, accounts, service, alpha, beta, project = setup()
    now = datetime.now(UTC)
    create_scheduled_task(service, actor=alpha, target=beta, task_id="task-first",
                          priority=TaskPriority.NORMAL, due_at=now + timedelta(days=5))
    create_scheduled_task(service, actor=alpha, target=beta, task_id="task-second",
                          priority=TaskPriority.NORMAL, due_at=now + timedelta(days=6))
    # Touch the first task so its updated_at becomes the newest.
    service.respond_task(project_id="project-a", task_id="task-first",
                         actor_id=beta.account_id, accept=True)
    inbox = service.collaboration_inbox(actor_id=beta.account_id)
    titles = [item.task.task_id for item in inbox.actions]
    assert titles == ["task-first", "task-second"]
    again = service.collaboration_inbox(actor_id=beta.account_id)
    assert [item.task.task_id for item in again.actions] == titles


def test_third_project_team_cannot_change_or_decide_schedule():
    """A project participant that is neither source nor target has no say."""
    engine, accounts, service, alpha, beta, project = setup()
    gamma, gamma_bootstrap = accounts.register_team(
        team_id="team-gamma", team_handle="gamma-team", team_name="Gamma"
    )
    accounts.change_initial_password(
        login=gamma_bootstrap.username, current_password=gamma_bootstrap.initial_password,
        new_password="Admin-Correct-Horse-42!",
    )
    outsider = accounts.get_account(gamma_bootstrap.account.account_id)
    # Third team joins the project so it passes participant checks.
    from coifesp_harness.product import ProjectDirectoryService

    gamma_request = accounts.send_team_relation_request(
        request_id="relation-ag", actor_id=alpha.account_id, recipient_team_handle="gamma-team"
    )
    accounts.decide_team_relation_request(
        request_id=gamma_request.request_id, actor_id=outsider.account_id, accept=True
    )
    directory = ProjectDirectoryService(engine)
    directory.add_team(
        project_id="project-a",
        team_id=gamma.team_id,
        name="质量团队",
        kind=ProjectTeamKind.QUALITY,
        actor_id=alpha.account_id,
    )
    create_scheduled_task(service, actor=alpha, target=beta,
                          due_at=datetime.now(UTC) + timedelta(days=30))
    with pytest.raises(PolicyDenied):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=outsider.account_id,
            priority=TaskPriority.LOW, due_at=None, clear_due_at=False,
            expected_schedule_version=1, reason="",
        )
    service.respond_task(project_id="project-a", task_id="task-1",
                         actor_id=beta.account_id, accept=True)
    _, proposal = service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=datetime.now(UTC) + timedelta(days=60),
        clear_due_at=False, expected_schedule_version=1, reason="调整",
    )
    with pytest.raises(PolicyDenied):
        service.decide_schedule_proposal(
            project_id="project-a", task_id="task-1",
            proposal_id=proposal.proposal_id, actor_id=outsider.account_id,
            accept=True, reason="", expected_proposal_version=1,
        )
    with pytest.raises(PolicyDenied):
        service.change_task_schedule(
            project_id="project-a", task_id="task-1", actor_id=outsider.account_id,
            priority=TaskPriority.LOW, due_at=None, clear_due_at=False,
            expected_schedule_version=1, reason="第三方意见",
        )
