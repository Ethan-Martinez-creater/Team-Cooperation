"""Reminder read/archive state and concurrency regression tests.

User read/archive state must survive reminder refreshes, refreshes must be
idempotent, concurrent notification queries must not raise uniqueness errors,
and one account's query must never mutate another account's reminders.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.product import (
    NotificationCategory,
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TaskPriority,
    TeamCollaborationService,
)
from coifesp_harness.product.repository import NOTIFICATIONS, TEAM_TASKS


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


def _create_due_soon_task(service, alpha, beta, *, hours=3):
    service.create_task(
        task_id="task-remind",
        project_id="project-a",
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        title="提醒任务",
        description="",
        acceptance_criteria="通过",
        priority=TaskPriority.NORMAL,
        due_at=datetime.now(UTC) + timedelta(hours=hours),
    )


def _reminders(page, category=NotificationCategory.DUE_SOON):
    return [item for item in page.items if item.category is category]


def test_archived_reminder_stays_archived_after_next_query():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta)
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder = _reminders(page)[0]
    notifications.archive(account_id=beta.account_id, notification_id=reminder.notification_id)

    active_page = notifications.list_notifications(account_id=beta.account_id)
    assert _reminders(active_page) == []
    archived_page = notifications.list_notifications(
        account_id=beta.account_id, archived_only=True
    )
    archived_reminders = _reminders(archived_page)
    assert len(archived_reminders) == 1
    assert archived_reminders[0].notification_id == reminder.notification_id
    assert archived_reminders[0].archived_at is not None


def test_archived_reminder_state_is_stable_across_repeated_queries():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta)
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder = _reminders(page)[0]
    notifications.archive(account_id=beta.account_id, notification_id=reminder.notification_id)
    first = notifications.list_notifications(
        account_id=beta.account_id, archived_only=True
    ).items[0]
    second = notifications.list_notifications(
        account_id=beta.account_id, archived_only=True
    ).items[0]
    third = notifications.list_notifications(account_id=beta.account_id)
    assert first.archived_at == second.archived_at
    assert first.read_at == second.read_at
    assert _reminders(third) == []


def test_read_reminder_stays_read_when_content_unchanged():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta)
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder = _reminders(page)[0]
    notifications.mark_read(account_id=beta.account_id, notification_id=reminder.notification_id)
    for _ in range(3):
        page = notifications.list_notifications(account_id=beta.account_id)
        current = _reminders(page)[0]
        assert current.read_at is not None and current.archived_at is None
    # The unread count may still cover the separate task-creation activity
    # notification; the reminder itself must not contribute to it.
    unread_reminder_ids = {
        item.notification_id
        for page_item in (page,)
        for item in page_item.items
        if item.read_at is None and item.category is NotificationCategory.DUE_SOON
    }
    assert unread_reminder_ids == set()


def test_unchanged_reminder_refresh_creates_no_duplicates():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta)
    ids = set()
    for _ in range(4):
        page = notifications.list_notifications(account_id=beta.account_id)
        ids.update(item.notification_id for item in _reminders(page))
    assert len(ids) == 1


def test_substantive_due_change_resets_read_only_for_unarchived():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta, hours=3)
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder = _reminders(page)[0]
    notifications.mark_read(account_id=beta.account_id, notification_id=reminder.notification_id)

    service.change_task_schedule(
        project_id="project-a",
        task_id="task-remind",
        actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL,
        due_at=datetime.now(UTC) + timedelta(hours=5),
        clear_due_at=False,
        expected_schedule_version=1,
        reason="",
    )
    refreshed = notifications.list_notifications(account_id=beta.account_id)
    moved = _reminders(refreshed)[0]
    assert moved.notification_id == reminder.notification_id
    assert moved.read_at is None  # substantive change re-alerts unarchived rows
    assert moved.archived_at is None
    new_due_text = (datetime.now(UTC) + timedelta(hours=5)).strftime("%Y-%m-%d %H")
    assert new_due_text in moved.summary


def test_archived_reminder_survives_substantive_due_change_as_history():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta, hours=3)
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder = _reminders(page)[0]
    notifications.archive(account_id=beta.account_id, notification_id=reminder.notification_id)

    service.change_task_schedule(
        project_id="project-a",
        task_id="task-remind",
        actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL,
        due_at=datetime.now(UTC) + timedelta(days=10),
        clear_due_at=False,
        expected_schedule_version=1,
        reason="",
    )
    active = notifications.list_notifications(account_id=beta.account_id)
    archived = notifications.list_notifications(account_id=beta.account_id, archived_only=True)
    # The moved deadline left the window: no active reminder, archived row kept.
    assert _reminders(active) == []
    archived_reminders = _reminders(archived)
    assert len(archived_reminders) == 1
    assert archived_reminders[0].archived_at is not None


def test_archived_overdue_reminder_is_not_restored_by_query():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    service.create_task(
        task_id="task-late",
        project_id="project-a",
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        title="逾期任务",
        description="",
        acceptance_criteria="通过",
        priority=TaskPriority.NORMAL,
        due_at=datetime.now(UTC) + timedelta(days=5),
    )
    with engine.begin() as connection:
        connection.execute(
            update(TEAM_TASKS)
            .where(TEAM_TASKS.c.task_id == "task-late")
            .values(due_at=datetime.now(UTC) - timedelta(days=1))
        )
    page = notifications.list_notifications(account_id=beta.account_id)
    overdue = _reminders(page, NotificationCategory.OVERDUE)[0]
    notifications.archive(account_id=beta.account_id, notification_id=overdue.notification_id)
    after = notifications.list_notifications(account_id=beta.account_id)
    assert _reminders(after, NotificationCategory.OVERDUE) == []
    archived = notifications.list_notifications(account_id=beta.account_id, archived_only=True)
    assert len(_reminders(archived, NotificationCategory.OVERDUE)) == 1


def test_cleared_deadline_keeps_archived_reminder_as_history():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    _create_due_soon_task(service, alpha, beta, hours=3)
    page = notifications.list_notifications(account_id=beta.account_id)
    reminder = _reminders(page)[0]
    notifications.archive(account_id=beta.account_id, notification_id=reminder.notification_id)

    service.change_task_schedule(
        project_id="project-a",
        task_id="task-remind",
        actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL,
        due_at=None,
        clear_due_at=True,
        expected_schedule_version=1,
        reason="取消期限",
    )
    active = notifications.list_notifications(account_id=beta.account_id)
    archived = notifications.list_notifications(account_id=beta.account_id, archived_only=True)
    assert _reminders(active) == []
    assert len(_reminders(archived)) == 1


def test_concurrent_notification_queries_are_idempotent_and_safe():
    """Concurrent list_notifications calls keep one reminder row and never raise."""
    import os
    import threading

    db_path = ".test-concurrent-notifications.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    try:
        accounts = ProductAccountService(engine)
        accounts.create_schema()
        directory = ProjectDirectoryService(engine)
        notifications = NotificationService(engine)
        team, bootstrap = accounts.register_team(
            team_id="team-alpha", team_handle="alpha-team", team_name="Alpha"
        )
        accounts.change_initial_password(
            login=bootstrap.username,
            current_password=bootstrap.initial_password,
            new_password="Admin-Correct-Horse-42!",
        )
        alpha = accounts.get_account(bootstrap.account.account_id)
        partner, partner_bootstrap = accounts.register_team(
            team_id="team-beta", team_handle="beta-team", team_name="Beta"
        )
        accounts.change_initial_password(
            login=partner_bootstrap.username,
            current_password=partner_bootstrap.initial_password,
            new_password="Admin-Correct-Horse-42!",
        )
        beta = accounts.get_account(partner_bootstrap.account.account_id)
        request = accounts.send_team_relation_request(
            request_id="relation-ab2", actor_id=alpha.account_id, recipient_team_handle="beta-team"
        )
        accounts.decide_team_relation_request(
            request_id=request.request_id, actor_id=beta.account_id, accept=True
        )
        project = directory.create_project(
            project_id="project-c",
            name="C",
            description="",
            actor_id=alpha.account_id,
            owner_assignment_name="产品",
            owner_kind=ProjectTeamKind.PRODUCT,
        )
        directory.add_team(
            project_id=project.project_id,
            team_id=partner.team_id,
            name="工程",
            kind=ProjectTeamKind.ENGINEERING,
            actor_id=alpha.account_id,
        )
        service = TeamCollaborationService(engine, notifier=notifications)
        service.create_task(
            task_id="task-concurrent",
            project_id="project-c",
            actor_id=alpha.account_id,
            target_team_id=partner.team_id,
            title="并发",
            description="",
            acceptance_criteria="通过",
            priority=TaskPriority.NORMAL,
            due_at=datetime.now(UTC) + timedelta(hours=2),
        )
        results = []
        errors = []
        barrier = threading.Barrier(12)

        def worker():
            try:
                barrier.wait()
                results.append(notifications.list_notifications(account_id=beta.account_id))
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        ids = set()
        for page in results:
            for item in _reminders(page):
                ids.add(item.notification_id)
        assert len(ids) == 1
    finally:
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def test_one_account_query_does_not_touch_teammate_reminders():
    engine, accounts, service, notifications, alpha, beta, project = setup()
    member = accounts.request_account_registration(
        account_id="acct-beta-two",
        username="beta-two",
        display_name="Beta Two",
        email="two@beta.test",
        password="Member-Correct-Horse-42!",
        team_id=beta.team_id,
    )
    accounts.decide_account_registration(
        actor_id=beta.account_id, account_id=member.account_id, accept=True
    )
    _create_due_soon_task(service, alpha, beta)
    admin_page = notifications.list_notifications(account_id=beta.account_id)
    admin_reminder = _reminders(admin_page)[0]
    notifications.mark_read(account_id=beta.account_id, notification_id=admin_reminder.notification_id)
    # The teammate queries afterwards; admin state must stay read and rows stay separate.
    member_page = notifications.list_notifications(account_id=member.account_id)
    assert len(_reminders(member_page)) == 1
    assert _reminders(member_page)[0].notification_id != admin_reminder.notification_id
    admin_after = notifications.list_notifications(account_id=beta.account_id)
    assert _reminders(admin_after)[0].read_at is not None


def test_concurrent_state_operations_keep_counts_consistent():
    """Concurrent mark-read / archive / restore / mark-page-read stay consistent."""
    import os
    import random
    import threading

    db_path = ".test-concurrent-state.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    try:
        accounts = ProductAccountService(engine)
        accounts.create_schema()
        directory = ProjectDirectoryService(engine)
        notifications = NotificationService(engine)
        team, bootstrap = accounts.register_team(
            team_id="team-alpha", team_handle="alpha-team", team_name="Alpha"
        )
        accounts.change_initial_password(
            login=bootstrap.username,
            current_password=bootstrap.initial_password,
            new_password="Admin-Correct-Horse-42!",
        )
        alpha = accounts.get_account(bootstrap.account.account_id)
        partner, partner_bootstrap = accounts.register_team(
            team_id="team-beta", team_handle="beta-team", team_name="Beta"
        )
        accounts.change_initial_password(
            login=partner_bootstrap.username,
            current_password=partner_bootstrap.initial_password,
            new_password="Admin-Correct-Horse-42!",
        )
        beta = accounts.get_account(partner_bootstrap.account.account_id)
        request = accounts.send_team_relation_request(
            request_id="relation-ab3",
            actor_id=alpha.account_id,
            recipient_team_handle="beta-team",
        )
        accounts.decide_team_relation_request(
            request_id=request.request_id, actor_id=beta.account_id, accept=True
        )
        project = directory.create_project(
            project_id="project-d",
            name="D",
            description="",
            actor_id=alpha.account_id,
            owner_assignment_name="产品",
            owner_kind=ProjectTeamKind.PRODUCT,
        )
        directory.add_team(
            project_id=project.project_id,
            team_id=partner.team_id,
            name="工程",
            kind=ProjectTeamKind.ENGINEERING,
            actor_id=alpha.account_id,
        )
        service = TeamCollaborationService(engine, notifier=notifications)
        for index in range(5):
            service.create_task(
                task_id=f"task-mix-{index}",
                project_id=project.project_id,
                actor_id=alpha.account_id,
                target_team_id=partner.team_id,
                title=f"并发状态 {index}",
                description="",
                acceptance_criteria="通过",
                priority=TaskPriority.NORMAL,
                due_at=datetime.now(UTC) + timedelta(hours=2, minutes=index),
            )
        page = notifications.list_notifications(account_id=beta.account_id)
        all_ids = [item.notification_id for item in page.items]
        assert len(all_ids) >= 5
        errors = []
        barrier = threading.Barrier(10)

        def worker(seed):
            random.seed(seed)
            try:
                barrier.wait()
                for _ in range(6):
                    action = random.choice(
                        ("list", "mark_read", "archive", "restore", "page_read")
                    )
                    if action == "list":
                        notifications.list_notifications(account_id=beta.account_id)
                    elif action == "mark_read":
                        notifications.mark_read(
                            account_id=beta.account_id,
                            notification_id=random.choice(all_ids),
                        )
                    elif action == "archive":
                        notifications.archive(
                            account_id=beta.account_id,
                            notification_id=random.choice(all_ids),
                        )
                    elif action == "restore":
                        notifications.restore(
                            account_id=beta.account_id,
                            notification_id=random.choice(all_ids),
                        )
                    else:
                        notifications.mark_page_read(
                            account_id=beta.account_id,
                            notification_ids=(random.choice(all_ids),),
                        )
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        final = notifications.list_notifications(account_id=beta.account_id, limit=200)
        recounted = sum(
            1
            for item in final.items
            if item.read_at is None and item.archived_at is None
        )
        # Unread count must match a fresh recount of active unread rows.
        with engine.connect() as connection:
            from sqlalchemy import func, select

            from coifesp_harness.product.repository import NOTIFICATIONS

            db_unread = connection.execute(
                select(func.count())
                .select_from(NOTIFICATIONS)
                .where(
                    NOTIFICATIONS.c.account_id == beta.account_id,
                    NOTIFICATIONS.c.archived_at.is_(None),
                    NOTIFICATIONS.c.read_at.is_(None),
                )
            ).scalar_one()
        assert final.unread_count == recounted == int(db_unread)
        assert len(final.items) == len({item.notification_id for item in final.items})
    finally:
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def test_stale_task_snapshot_cannot_overwrite_new_schedule_reminder():
    """A delayed GET snapshot cannot restore an older deadline reminder."""
    engine, accounts, service, notifications, alpha, beta, project = setup()
    old_due = datetime.now(UTC) + timedelta(hours=3)
    service.create_task(
        task_id="task-stale",
        project_id="project-a",
        actor_id=alpha.account_id,
        target_team_id=beta.team_id,
        title="stale schedule",
        description="",
        acceptance_criteria="accepted",
        priority=TaskPriority.NORMAL,
        due_at=old_due,
    )
    with engine.connect() as connection:
        stale_task = (
            connection.execute(
                select(TEAM_TASKS).where(TEAM_TASKS.c.task_id == "task-stale")
            )
            .mappings()
            .one()
        )

    new_due = datetime.now(UTC) + timedelta(hours=2)
    service.change_task_schedule(
        project_id="project-a",
        task_id="task-stale",
        actor_id=alpha.account_id,
        priority=TaskPriority.NORMAL,
        due_at=new_due,
        clear_due_at=False,
        expected_schedule_version=1,
        reason="race regression",
    )
    notifications.list_notifications(account_id=beta.account_id)

    # Reproduce the losing ordering: version 1 was captured, version 2
    # committed, and then the delayed version-1 refresh resumed.
    with engine.begin() as connection:
        notifications.refresh_task_reminders(
            connection, stale_task, now=datetime.now(UTC)
        )

    with engine.connect() as connection:
        summary = connection.execute(
            select(NOTIFICATIONS.c.summary).where(
                NOTIFICATIONS.c.account_id == beta.account_id,
                NOTIFICATIONS.c.subject_id == "task-stale",
                NOTIFICATIONS.c.category == NotificationCategory.DUE_SOON.value,
            )
        ).scalar_one()
    assert new_due.strftime("%Y-%m-%d %H:%M") in summary
    assert old_due.strftime("%Y-%m-%d %H:%M") not in summary
