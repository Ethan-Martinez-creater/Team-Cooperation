from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import dataclasses

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.product import (
    NotificationCategory,
    NotificationPreference,
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TaskPriority,
    TeamCollaborationService,
    default_preference,
    is_in_quiet_hours,
)
from coifesp_harness.product.notifications import category_for_event


def setup():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    directory = ProjectDirectoryService(engine)
    notifications = NotificationService(engine)
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
    return (
        engine,
        accounts,
        TeamCollaborationService(engine, notifier=notifications),
        notifications,
        alpha,
        beta,
        project,
    )


# --------------------------------------------------------------------------- unit


def test_category_for_event_maps_known_prefixes_and_rejects_others():
    assert category_for_event("task.proposed") is NotificationCategory.TASK
    assert category_for_event("message.sent") is NotificationCategory.MESSAGE
    assert category_for_event("resource.published") is NotificationCategory.RESOURCE
    assert category_for_event("topic.opened") is NotificationCategory.TOPIC
    assert category_for_event("agent_draft.imported") is NotificationCategory.AGENT
    assert category_for_event("task_schedule_changed") is None


def variant(base, **overrides):
    return dataclasses.replace(base, **overrides)


def test_quiet_hours_window_including_midnight_crossing():
    preference = variant(
        default_preference("acct-1"),
        time_zone="Asia/Shanghai",
        quiet_start_minute=22 * 60,
        quiet_end_minute=7 * 60,
    )
    night = datetime(2026, 8, 22, 23, 30, tzinfo=UTC)  # 07:30 Shanghai next logic aside
    # 23:30 UTC == 07:30 Shanghai -> quiet (after 07:00 end? no: 07:30 >= 07:00 end -> not quiet)
    assert is_in_quiet_hours(preference, night) is False
    late = datetime(2026, 8, 22, 16, 30, tzinfo=UTC)  # 00:30 Shanghai -> quiet
    assert is_in_quiet_hours(preference, late) is True
    evening = datetime(2026, 8, 22, 15, 30, tzinfo=UTC)  # 23:30 Shanghai -> quiet
    assert is_in_quiet_hours(preference, evening) is True
    morning = datetime(2026, 8, 22, 1, 30, tzinfo=UTC)  # 09:30 Shanghai -> not quiet
    assert is_in_quiet_hours(preference, morning) is False


def test_quiet_hours_respect_daylight_saving_boundary():
    preference = variant(
        default_preference("acct-1"),
        time_zone="America/New_York",
        quiet_start_minute=1 * 60,
        quiet_end_minute=5 * 60,
    )
    winter = datetime(2027, 1, 15, 7, 30, tzinfo=UTC)  # 02:30 EST -> quiet
    assert is_in_quiet_hours(preference, winter) is True
    summer = datetime(2027, 7, 15, 7, 30, tzinfo=UTC)  # 03:30 EDT -> quiet
    assert is_in_quiet_hours(preference, summer) is True
    outside = datetime(2027, 7, 15, 10, 30, tzinfo=UTC)  # 06:30 EDT -> not quiet
    assert is_in_quiet_hours(preference, outside) is False


def test_invalid_preferences_are_rejected():
    service = NotificationService(
        create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    )
    base = default_preference("acct-1")
    with pytest.raises(ValueError):
        service.put_preferences(
            account_id="acct-1",
            preference=variant(base, time_zone="Mars/Olympus_Mons"),
        )
    with pytest.raises(ValueError):
        service.put_preferences(
            account_id="acct-1",
            preference=variant(base, quiet_start_minute=60, quiet_end_minute=None),
        )
    with pytest.raises(ValueError):
        service.put_preferences(
            account_id="acct-1",
            preference=variant(base, quiet_start_minute=120, quiet_end_minute=120),
        )
    with pytest.raises(ValueError):
        service.put_preferences(
            account_id="acct-1", preference=variant(base, due_soon_hours=0)
        )


# ------------------------------------------------------------------------ service


def test_activity_projection_is_idempotent_and_per_account():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    service.send_message(
        message_id="message-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, content="请确认排期",
    )
    alpha_page = notifications.list_notifications(account_id=alpha.account_id)
    beta_page = notifications.list_notifications(account_id=beta.account_id)
    assert alpha_page.unread_count == 0  # actors never notify themselves
    assert beta_page.unread_count == 1
    assert beta_page.items[0].category is NotificationCategory.MESSAGE
    # Reading as beta must not clear alpha-side state (alpha has none) and a
    # second listing stays at one row: replays cannot duplicate.
    beta_page_again = notifications.list_notifications(account_id=beta.account_id)
    assert beta_page_again.unread_count == 1
    assert len(beta_page_again.items) == 1


def test_one_account_reading_does_not_affect_teammates():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    beta_member = accounts.request_account_registration(
        account_id="acct-beta-member", username="beta-member", display_name="Beta Member",
        email="member@beta.test", password="Member-Correct-Horse-42!", team_id=beta.team_id,
    )
    accounts.decide_account_registration(
        actor_id=beta.account_id, account_id=beta_member.account_id, accept=True
    )
    service.send_message(
        message_id="message-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, content="请确认排期",
    )
    admin_page = notifications.list_notifications(account_id=beta.account_id)
    member_page = notifications.list_notifications(account_id=beta_member.account_id)
    assert admin_page.unread_count == 1 and member_page.unread_count == 1
    notifications.mark_read(account_id=beta.account_id,
                            notification_id=admin_page.items[0].notification_id)
    after_admin = notifications.list_notifications(account_id=beta.account_id)
    after_member = notifications.list_notifications(account_id=beta_member.account_id)
    assert after_admin.unread_count == 0
    assert after_member.unread_count == 1


def test_disabled_preference_skips_new_notifications_but_keeps_existing():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    service.send_message(
        message_id="message-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, content="第一条",
    )
    existing = notifications.list_notifications(account_id=beta.account_id)
    assert existing.unread_count == 1
    notifications.put_preferences(
        account_id=beta.account_id,
        preference=variant(default_preference(beta.account_id), notify_messages=False),
    )
    service.send_message(
        message_id="message-2", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, content="第二条",
    )
    page = notifications.list_notifications(account_id=beta.account_id)
    assert page.unread_count == 1  # only the first message remains
    assert page.items[0].summary.startswith("团队发送了项目消息")


def test_task_notifications_created_for_target_team():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    service.create_task(
        task_id="task-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, title="交付", description="",
        acceptance_criteria="通过", priority=TaskPriority.HIGH,
        due_at=datetime.now(UTC) + timedelta(days=2),
    )
    page = notifications.list_notifications(account_id=beta.account_id)
    categories = {item.category for item in page.items}
    assert NotificationCategory.TASK in categories
    assert NotificationCategory.DUE_SOON in categories


def test_due_soon_reminder_updates_when_deadline_moves_and_clears():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    first_due = datetime.now(UTC) + timedelta(hours=3)
    service.create_task(
        task_id="task-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, title="交付", description="",
        acceptance_criteria="通过", priority=TaskPriority.NORMAL, due_at=first_due,
    )
    page = notifications.list_notifications(account_id=beta.account_id)
    due_soon = [item for item in page.items if item.category is NotificationCategory.DUE_SOON]
    assert len(due_soon) == 1
    first_summary = due_soon[0].summary
    assert first_due.strftime("%Y-%m-%d %H:%M") in first_summary

    later_due = datetime.now(UTC) + timedelta(days=10)
    service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=later_due, clear_due_at=False,
        expected_schedule_version=1, reason="",
    )
    page = notifications.list_notifications(account_id=beta.account_id)
    due_soon = [item for item in page.items if item.category is NotificationCategory.DUE_SOON]
    assert due_soon == []  # the moved deadline no longer falls inside the window
    overdue = [item for item in page.items if item.category is NotificationCategory.OVERDUE]
    assert overdue == []

    service.change_task_schedule(
        project_id="project-a", task_id="task-1", actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL, due_at=None, clear_due_at=True,
        expected_schedule_version=2, reason="取消期限",
    )
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder_rows = [
        item for item in page.items
        if item.category in (NotificationCategory.DUE_SOON, NotificationCategory.OVERDUE)
    ]
    assert reminder_rows == []


def test_overdue_reminder_appears_when_deadline_passes():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    past = datetime.now(UTC) - timedelta(days=1)
    service.create_task(
        task_id="task-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, title="交付", description="",
        acceptance_criteria="通过", priority=TaskPriority.NORMAL,
        due_at=datetime.now(UTC) + timedelta(days=5),
    )
    from sqlalchemy import update

    from coifesp_harness.product.repository import TEAM_TASKS

    with engine.begin() as connection:
        connection.execute(
            update(TEAM_TASKS).where(TEAM_TASKS.c.task_id == "task-1").values(due_at=past)
        )
    page = notifications.list_notifications(account_id=beta.account_id)
    overdue = [item for item in page.items if item.category is NotificationCategory.OVERDUE]
    assert len(overdue) == 1
    assert overdue[0].title == "任务已逾期"


def test_archive_restore_and_pagination_cursor():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    for index in range(4):
        service.send_message(
            message_id=f"message-{index}", project_id="project-a", actor_id=alpha.account_id,
            target_team_id=beta.team_id, content=f"消息 {index}",
        )
    page_one = notifications.list_notifications(account_id=beta.account_id, limit=2)
    assert len(page_one.items) == 2 and page_one.next_cursor is not None
    page_two = notifications.list_notifications(
        account_id=beta.account_id, limit=2, cursor=page_one.next_cursor
    )
    ids_one = {item.notification_id for item in page_one.items}
    ids_two = {item.notification_id for item in page_two.items}
    assert not ids_one & ids_two
    assert page_one.unread_count == 4

    archived = notifications.archive(
        account_id=beta.account_id, notification_id=page_one.items[0].notification_id
    )
    assert archived.archived_at is not None and archived.read_at is not None
    active = notifications.list_notifications(account_id=beta.account_id)
    assert all(item.archived_at is None for item in active.items)
    assert active.unread_count == 3
    archived_view = notifications.list_notifications(
        account_id=beta.account_id, archived_only=True
    )
    assert len(archived_view.items) == 1
    restored = notifications.restore(
        account_id=beta.account_id, notification_id=page_one.items[0].notification_id
    )
    assert restored.archived_at is None
    with pytest.raises(Exception):
        notifications.list_notifications(account_id=beta.account_id, cursor="not-a-cursor")


def test_mark_page_read_only_touches_listed_ids():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    for index in range(3):
        service.send_message(
            message_id=f"message-{index}", project_id="project-a", actor_id=alpha.account_id,
            target_team_id=beta.team_id, content=f"消息 {index}",
        )
    page = notifications.list_notifications(account_id=beta.account_id)
    target_ids = tuple(item.notification_id for item in page.items[:2])
    notifications.mark_page_read(account_id=beta.account_id, notification_ids=target_ids)
    after = notifications.list_notifications(account_id=beta.account_id)
    assert after.unread_count == 1
    unread = [item for item in after.items if item.read_at is None]
    assert len(unread) == 1
    assert unread[0].notification_id == page.items[2].notification_id
    with pytest.raises(ValueError):
        notifications.mark_page_read(
            account_id=beta.account_id, notification_ids=("a", "a")
        )


def test_notification_access_is_restricted_to_owner():
    from coifesp_harness.errors import ResourceNotFound

    engine, accounts, service, notifications, alpha, beta, project = setup()
    service.send_message(
        message_id="message-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, content="请确认排期",
    )
    page = notifications.list_notifications(account_id=beta.account_id)
    notification_id = page.items[0].notification_id
    with pytest.raises(ResourceNotFound):
        notifications.mark_read(account_id=alpha.account_id, notification_id=notification_id)


def test_accounts_without_project_access_receive_no_notifications():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    gamma, gamma_bootstrap = accounts.register_team(
        team_id="team-gamma", team_handle="gamma-team", team_name="Gamma"
    )
    accounts.change_initial_password(
        login=gamma_bootstrap.username, current_password=gamma_bootstrap.initial_password,
        new_password="Admin-Correct-Horse-42!",
    )
    outsider = accounts.get_account(gamma_bootstrap.account.account_id)
    service.send_message(
        message_id="message-1", project_id="project-a", actor_id=alpha.account_id,
        target_team_id=beta.team_id, content="请确认排期",
    )
    outsider_page = notifications.list_notifications(account_id=outsider.account_id)
    assert outsider_page.items == ()
    assert outsider_page.unread_count == 0
