from __future__ import annotations

import base64
import binascii
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, delete, desc, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as sa_pg_insert
from sqlalchemy.dialects.sqlite import insert as sa_sqlite_insert
from sqlalchemy.engine import Engine

from ..errors import ResourceNotFound
from .models import (
    Notification,
    NotificationCategory,
    NotificationPage,
    NotificationPreference,
    TaskPriority,
    TeamTaskStatus,
    compute_team_task_schedule,
)
from .repository import (
    ACCOUNTS,
    NOTIFICATIONS,
    NOTIFICATION_PREFERENCES,
    PROJECT_TEAMS,
    TEAM_TASKS,
)

EVENT_CATEGORY_PREFIXES = (
    ("task.", NotificationCategory.TASK),
    ("message.", NotificationCategory.MESSAGE),
    ("resource.", NotificationCategory.RESOURCE),
    ("topic.", NotificationCategory.TOPIC),
    ("agent_draft.", NotificationCategory.AGENT),
)

REMINDER_CATEGORIES = (NotificationCategory.DUE_SOON, NotificationCategory.OVERDUE)

_VALID_TIME_ZONES_CACHE: set[str] | None = None


def _valid_time_zones() -> set[str]:
    global _VALID_TIME_ZONES_CACHE
    if _VALID_TIME_ZONES_CACHE is None:
        try:
            from zoneinfo import available_timezones

            _VALID_TIME_ZONES_CACHE = set(available_timezones())
        except Exception:  # pragma: no cover - tzdata missing falls back to UTC-only
            _VALID_TIME_ZONES_CACHE = {"UTC"}
    return _VALID_TIME_ZONES_CACHE


def category_for_event(event_type: str) -> NotificationCategory | None:
    for prefix, category in EVENT_CATEGORY_PREFIXES:
        if event_type.startswith(prefix):
            return category
    return None


def default_preference(account_id: str) -> NotificationPreference:
    return NotificationPreference(
        account_id=account_id,
        notify_tasks=True,
        notify_messages=True,
        notify_resources=True,
        notify_topics=True,
        notify_agent_events=True,
        notify_due_soon=True,
        notify_overdue=True,
        due_soon_hours=48,
        time_zone="UTC",
        quiet_start_minute=None,
        quiet_end_minute=None,
        updated_at=None,
    )


def is_in_quiet_hours(preference: NotificationPreference, now: datetime) -> bool:
    """Return True when ``now`` falls inside the account's local quiet window.

    The window may cross midnight (for example 22:00 -> 07:00). Quiet hours never
    block notification creation; they only gate proactive presentation.
    """
    start = preference.quiet_start_minute
    end = preference.quiet_end_minute
    if start is None or end is None:
        return False
    try:
        local = now.astimezone(ZoneInfo(preference.time_zone))
    except (ZoneInfoNotFoundError, ValueError):
        local = now.astimezone(ZoneInfo("UTC"))
    minute = local.hour * 60 + local.minute
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def encode_notification_cursor(notification: Notification) -> str:
    raw = f"{notification.created_at.isoformat()}|{notification.notification_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_notification_cursor(cursor: str) -> tuple[datetime, str] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created, notification_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(created), notification_id
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def _reminder_identity(category: NotificationCategory) -> str:
    return "reminder" if category in REMINDER_CATEGORIES else "activity"


def _aware(value: datetime) -> datetime:
    """Interpret SQLite-returned naive datetimes as UTC; keep aware values."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class NotificationService:
    """Account-scoped notification center with idempotent activity projection.

    Activity notifications are inserted in the same transaction as the project
    activity itself; the unique projection key ``(account, project, sequence,
    category, subject)`` makes replays collapse onto a single row. Due-soon and
    overdue reminders are derived rows (``activity_sequence = 0``) that are
    upserted whenever a task schedule changes and refreshed on read, so a moved
    deadline invalidates the old reminder instead of duplicating it.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------------ preferences

    def get_preferences(self, *, account_id: str) -> NotificationPreference:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(NOTIFICATION_PREFERENCES).where(
                        NOTIFICATION_PREFERENCES.c.account_id == account_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._preference(row) if row is not None else default_preference(account_id)

    def put_preferences(
        self, *, account_id: str, preference: NotificationPreference
    ) -> NotificationPreference:
        if preference.time_zone not in _valid_time_zones():
            raise ValueError("notification time zone is not a valid IANA zone")
        if (
            (preference.quiet_start_minute is None) != (preference.quiet_end_minute is None)
            or (preference.quiet_start_minute is not None and preference.quiet_start_minute == preference.quiet_end_minute)
        ):
            raise ValueError("quiet hours must be a non-empty minute window")
        if not 1 <= preference.due_soon_hours <= 336:
            raise ValueError("due-soon window must be between 1 and 336 hours")
        now = datetime.now(UTC)
        stored = NotificationPreference(
            account_id=account_id,
            notify_tasks=preference.notify_tasks,
            notify_messages=preference.notify_messages,
            notify_resources=preference.notify_resources,
            notify_topics=preference.notify_topics,
            notify_agent_events=preference.notify_agent_events,
            notify_due_soon=preference.notify_due_soon,
            notify_overdue=preference.notify_overdue,
            due_soon_hours=preference.due_soon_hours,
            time_zone=preference.time_zone,
            quiet_start_minute=preference.quiet_start_minute,
            quiet_end_minute=preference.quiet_end_minute,
            updated_at=now,
        )
        with self.engine.begin() as connection:
            connection.execute(
                delete(NOTIFICATION_PREFERENCES).where(
                    NOTIFICATION_PREFERENCES.c.account_id == account_id
                )
            )
            connection.execute(
                insert(NOTIFICATION_PREFERENCES).values(
                    account_id=account_id,
                    notify_tasks=stored.notify_tasks,
                    notify_messages=stored.notify_messages,
                    notify_resources=stored.notify_resources,
                    notify_topics=stored.notify_topics,
                    notify_agent_events=stored.notify_agent_events,
                    notify_due_soon=stored.notify_due_soon,
                    notify_overdue=stored.notify_overdue,
                    due_soon_hours=stored.due_soon_hours,
                    time_zone=stored.time_zone,
                    quiet_start_minute=stored.quiet_start_minute,
                    quiet_end_minute=stored.quiet_end_minute,
                    updated_at=now,
                )
            )
        return stored

    # ------------------------------------------------------------------ projection

    def project_activity(
        self,
        connection,
        *,
        project_id: str,
        actor_account_id: str,
        sequence: int,
        event_type: str,
        subject_id: str,
        summary: str,
        created_at: datetime,
        title: str,
    ) -> int:
        """Create notifications for one project activity inside its transaction.

        Returns the number of inserted rows. Recipients are active accounts of
        participating teams except the actor; disabled preference categories are
        skipped at creation time without touching existing notifications.
        """
        category = category_for_event(event_type)
        if category is None:
            return 0
        audience = list(
            connection.execute(
                select(ACCOUNTS.c.account_id)
                .join(PROJECT_TEAMS, PROJECT_TEAMS.c.team_id == ACCOUNTS.c.team_id)
                .where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        ACCOUNTS.c.registration_status == "active",
                        ACCOUNTS.c.enabled,
                        ACCOUNTS.c.account_id != actor_account_id,
                    )
                )
            ).scalars()
        )
        preferences = {
            row["account_id"]: self._preference(row)
            for row in connection.execute(
                select(NOTIFICATION_PREFERENCES).where(
                    NOTIFICATION_PREFERENCES.c.account_id.in_(audience)
                )
            ).mappings()
        }
        inserted = 0
        for account_id in audience:
            preference = preferences.get(account_id)
            if preference is not None and not preference.category_enabled(category):
                continue
            existing = connection.execute(
                select(NOTIFICATIONS.c.notification_id).where(
                    and_(
                        NOTIFICATIONS.c.account_id == account_id,
                        NOTIFICATIONS.c.project_id == project_id,
                        NOTIFICATIONS.c.activity_sequence == sequence,
                        NOTIFICATIONS.c.category == category.value,
                        NOTIFICATIONS.c.subject_id == subject_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            connection.execute(
                insert(NOTIFICATIONS).values(
                    notification_id=f"ntf-{secrets.token_hex(12)}",
                    account_id=account_id,
                    project_id=project_id,
                    activity_sequence=sequence,
                    category=category.value,
                    title=title[:256],
                    summary=summary[:512],
                    subject_id=subject_id[:128],
                    read_at=None,
                    archived_at=None,
                    created_at=created_at,
                )
            )
            inserted += 1
        return inserted

    # ------------------------------------------------------------------ reminders

    def refresh_task_reminders(self, connection, task_row, *, now: datetime) -> None:
        """Recompute due-soon/overdue reminder rows for one task row.

        Called inside the transaction that changes a task schedule; it refreshes
        every active account of the target team. Reminder rows are keyed by
        ``(account, project, 0, category, task)``. Moving a deadline refreshes
        the row content while preserving user read/archive state; clearing the
        deadline or reaching a terminal status removes only the unarchived
        rows so archived reminders remain as history.
        """
        audience = list(
            connection.execute(
                select(ACCOUNTS.c.account_id).where(
                    and_(
                        ACCOUNTS.c.team_id == task_row["target_team_id"],
                        ACCOUNTS.c.registration_status == "active",
                        ACCOUNTS.c.enabled,
                    )
                )
            ).scalars()
        )
        preferences = {
            row["account_id"]: self._preference(row)
            for row in connection.execute(
                select(NOTIFICATION_PREFERENCES).where(
                    NOTIFICATION_PREFERENCES.c.account_id.in_(audience)
                )
            ).mappings()
        }
        for account_id in audience:
            self._refresh_task_reminder_for_account(
                connection,
                task_row,
                account_id=account_id,
                preference=preferences.get(account_id),
                now=now,
            )

    def _refresh_task_reminder_for_account(
        self, connection, task_row, *, account_id: str, preference, now: datetime
    ) -> None:
        """Refresh the reminder projection of one task for a single account.

        This is the account-scoped unit used by the read path so that listing
        one account's notifications never writes reminder rows that belong to
        another account. The current task row is locked and compared with the
        caller snapshot before any reminder mutation. This prevents a delayed
        notification GET from overwriting a reminder that a newer schedule
        transaction has already refreshed.
        """
        current_state = (
            connection.execute(
                select(
                    TEAM_TASKS.c.schedule_version,
                    TEAM_TASKS.c.status,
                )
                .where(TEAM_TASKS.c.task_id == task_row["task_id"])
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current_state is None or (
            current_state["schedule_version"] != task_row["schedule_version"]
            or current_state["status"] != task_row["status"]
        ):
            return
        status = TeamTaskStatus(task_row["status"])
        terminal = status in (TeamTaskStatus.VERIFIED, TeamTaskStatus.REJECTED)
        due_at = _aware(task_row["due_at"]) if task_row["due_at"] is not None else None
        if terminal or due_at is None:
            self._delete_reminders(connection, account_id, task_row["task_id"])
            return
        window_hours = preference.due_soon_hours if preference is not None else 48
        personal = compute_team_task_schedule(
            status=status,
            priority=TaskPriority(task_row["priority"]),
            due_at=due_at,
            schedule_version=task_row["schedule_version"],
            now=now,
            due_soon_hours=window_hours,
        )
        desired = (
            (NotificationCategory.OVERDUE, "任务已逾期", personal.is_overdue),
            (
                NotificationCategory.DUE_SOON,
                "任务临近截止时间",
                personal.is_due_soon,
            ),
        )
        active = {category for category, _, enabled in desired if enabled}
        for category in REMINDER_CATEGORIES:
            if category not in active:
                self._delete_reminder(connection, account_id, task_row["task_id"], category)
        for category, title, enabled in desired:
            if not enabled:
                continue
            if preference is not None and not preference.category_enabled(category):
                self._delete_reminder(
                    connection, account_id, task_row["task_id"], category
                )
                continue
            due_text = due_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            summary = (
                f"任务「{task_row['title']}」截止时间 {due_text}"
                f"（优先级 {task_row['priority']}）"
            )[:512]
            self._upsert_reminder(
                connection,
                account_id=account_id,
                project_id=task_row["project_id"],
                task_id=task_row["task_id"],
                category=category,
                title=title,
                summary=summary,
                created_at=now,
            )

    def refresh_account_reminders(self, connection, *, account_id: str, now: datetime) -> None:
        """Refresh only the requesting account's reminder rows on read.

        The task query is scoped to the account's team and to tasks whose
        target team is that team, and the refresh unit is account-scoped, so a
        notification GET never writes or mutates another account's state.
        """
        row = connection.execute(
            select(ACCOUNTS.c.team_id).where(ACCOUNTS.c.account_id == account_id)
        ).one_or_none()
        if row is None:
            return
        team_id = row[0]
        tasks = (
            connection.execute(
                select(TEAM_TASKS)
                .join(PROJECT_TEAMS, PROJECT_TEAMS.c.project_id == TEAM_TASKS.c.project_id)
                .where(
                    and_(
                        PROJECT_TEAMS.c.team_id == team_id,
                        TEAM_TASKS.c.target_team_id == team_id,
                        TEAM_TASKS.c.due_at.isnot(None),
                        TEAM_TASKS.c.status.notin_(
                            (TeamTaskStatus.VERIFIED.value, TeamTaskStatus.REJECTED.value)
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        if not tasks:
            return
        preference_row = connection.execute(
            select(NOTIFICATION_PREFERENCES).where(
                NOTIFICATION_PREFERENCES.c.account_id == account_id
            )
        ).mappings().one_or_none()
        preference = self._preference(preference_row) if preference_row is not None else None
        for task_row in tasks:
            self._refresh_task_reminder_for_account(
                connection,
                task_row,
                account_id=account_id,
                preference=preference,
                now=now,
            )

    # ------------------------------------------------------------------ queries

    def list_notifications(
        self,
        *,
        account_id: str,
        unread_only: bool = False,
        archived_only: bool = False,
        category: NotificationCategory | None = None,
        project_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> NotificationPage:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("notification page limit is invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            self.refresh_account_reminders(connection, account_id=account_id, now=now)
        with self.engine.connect() as connection:
            conditions = [NOTIFICATIONS.c.account_id == account_id]
            if archived_only:
                conditions.append(NOTIFICATIONS.c.archived_at.isnot(None))
            else:
                conditions.append(NOTIFICATIONS.c.archived_at.is_(None))
                if unread_only:
                    conditions.append(NOTIFICATIONS.c.read_at.is_(None))
            if category is not None:
                conditions.append(NOTIFICATIONS.c.category == category.value)
            if project_id is not None:
                conditions.append(NOTIFICATIONS.c.project_id == project_id)
            parsed_cursor = _decode_notification_cursor(cursor) if cursor else None
            if parsed_cursor is not None:
                created_before, cursor_id = parsed_cursor
                conditions.append(
                    or_(
                        NOTIFICATIONS.c.created_at < created_before,
                        and_(
                            NOTIFICATIONS.c.created_at == created_before,
                            NOTIFICATIONS.c.notification_id < cursor_id,
                        ),
                    )
                )
            elif cursor:
                raise ValueError("notification cursor is invalid")
            rows = (
                connection.execute(
                    select(NOTIFICATIONS)
                    .where(and_(*conditions))
                    .order_by(
                        desc(NOTIFICATIONS.c.created_at),
                        desc(NOTIFICATIONS.c.notification_id),
                    )
                    .limit(limit + 1)
                )
                .mappings()
                .all()
            )
            unread_count = connection.execute(
                select(func.count()).select_from(NOTIFICATIONS).where(
                    and_(
                        NOTIFICATIONS.c.account_id == account_id,
                        NOTIFICATIONS.c.archived_at.is_(None),
                        NOTIFICATIONS.c.read_at.is_(None),
                    )
                )
            ).scalar_one()
        items = tuple(self._notification(row) for row in rows[:limit])
        next_cursor = encode_notification_cursor(items[-1]) if len(rows) > limit and items else None
        return NotificationPage(
            items=items, unread_count=int(unread_count), next_cursor=next_cursor
        )

    def mark_read(self, *, account_id: str, notification_id: str) -> Notification:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = self._owned_notification(connection, account_id, notification_id)
            if row["read_at"] is None:
                connection.execute(
                    update(NOTIFICATIONS)
                    .where(NOTIFICATIONS.c.notification_id == notification_id)
                    .values(read_at=now)
                )
                row = dict(row)
                row["read_at"] = now
        return self._notification(row)

    def archive(self, *, account_id: str, notification_id: str) -> Notification:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = self._owned_notification(connection, account_id, notification_id)
            values = {}
            if row["archived_at"] is None:
                values["archived_at"] = now
            if row["read_at"] is None:
                values["read_at"] = now
            if values:
                connection.execute(
                    update(NOTIFICATIONS)
                    .where(NOTIFICATIONS.c.notification_id == notification_id)
                    .values(**values)
                )
                row = dict(row)
                row.update(values)
        return self._notification(row)

    def restore(self, *, account_id: str, notification_id: str) -> Notification:
        with self.engine.begin() as connection:
            row = self._owned_notification(connection, account_id, notification_id)
            if row["archived_at"] is not None:
                connection.execute(
                    update(NOTIFICATIONS)
                    .where(NOTIFICATIONS.c.notification_id == notification_id)
                    .values(archived_at=None)
                )
                row = dict(row)
                row["archived_at"] = None
        return self._notification(row)

    def mark_page_read(
        self, *, account_id: str, notification_ids: tuple[str, ...]
    ) -> tuple[Notification, ...]:
        if not notification_ids or len(notification_ids) > 200 or len(set(notification_ids)) != len(
            notification_ids
        ):
            raise ValueError("notification ids are invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            rows = []
            for notification_id in notification_ids:
                row = self._owned_notification(connection, account_id, notification_id)
                if row["read_at"] is None:
                    connection.execute(
                        update(NOTIFICATIONS)
                        .where(NOTIFICATIONS.c.notification_id == notification_id)
                        .values(read_at=now)
                    )
                    row = dict(row)
                    row["read_at"] = now
                rows.append(self._notification(row))
        return tuple(rows)

    def preferences_with_state(self, *, account_id: str) -> tuple[NotificationPreference, bool]:
        preference = self.get_preferences(account_id=account_id)
        return preference, is_in_quiet_hours(preference, datetime.now(UTC))

    # ------------------------------------------------------------------ internals

    def _owned_notification(self, connection, account_id: str, notification_id: str):
        row = (
            connection.execute(
                select(NOTIFICATIONS).where(
                    and_(
                        NOTIFICATIONS.c.notification_id == notification_id,
                        NOTIFICATIONS.c.account_id == account_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("notification is unavailable")
        return row

    def _delete_reminders(self, connection, account_id: str, task_id: str) -> None:
        """Remove unarchived derived reminders; archived ones stay as history."""
        connection.execute(
            delete(NOTIFICATIONS).where(
                and_(
                    NOTIFICATIONS.c.account_id == account_id,
                    NOTIFICATIONS.c.subject_id == task_id,
                    NOTIFICATIONS.c.activity_sequence == 0,
                    NOTIFICATIONS.c.archived_at.is_(None),
                    NOTIFICATIONS.c.category.in_(
                        (category.value for category in REMINDER_CATEGORIES)
                    ),
                )
            )
        )

    def _delete_reminder(
        self, connection, account_id: str, task_id: str, category: NotificationCategory
    ) -> None:
        connection.execute(
            delete(NOTIFICATIONS).where(
                and_(
                    NOTIFICATIONS.c.account_id == account_id,
                    NOTIFICATIONS.c.subject_id == task_id,
                    NOTIFICATIONS.c.activity_sequence == 0,
                    NOTIFICATIONS.c.archived_at.is_(None),
                    NOTIFICATIONS.c.category == category.value,
                )
            )
        )

    def _upsert_reminder(
        self,
        connection,
        *,
        account_id: str,
        project_id: str,
        task_id: str,
        category: NotificationCategory,
        title: str,
        summary: str,
        created_at: datetime,
    ) -> None:
        """Atomically insert or update one derived reminder row.

        State contract:

        - missing row: insert unread and unarchived via ``ON CONFLICT DO NOTHING``
          so concurrent readers cannot raise uniqueness errors;
        - unchanged content: leave the row untouched regardless of read/archive
          state (repeated refresh is fully idempotent);
        - changed content (substantive deadline/priority change): refresh
          title/summary/created_at, never reset ``archived_at``, and only reset
          ``read_at`` for rows the user has not archived yet.
        """
        values = dict(
            notification_id=f"ntf-{secrets.token_hex(12)}",
            account_id=account_id,
            project_id=project_id,
            activity_sequence=0,
            category=category.value,
            title=title[:256],
            summary=summary[:512],
            subject_id=task_id[:128],
            read_at=None,
            archived_at=None,
            created_at=created_at,
        )
        if connection.dialect.name == "postgresql":
            dialect_insert = sa_pg_insert(NOTIFICATIONS)
        else:
            dialect_insert = sa_sqlite_insert(NOTIFICATIONS)
        connection.execute(
            dialect_insert.values(**values).on_conflict_do_nothing(
                index_elements=[
                    "account_id",
                    "project_id",
                    "activity_sequence",
                    "category",
                    "subject_id",
                ]
            )
        )
        existing = (
            connection.execute(
                select(NOTIFICATIONS).where(
                    and_(
                        NOTIFICATIONS.c.account_id == account_id,
                        NOTIFICATIONS.c.project_id == project_id,
                        NOTIFICATIONS.c.activity_sequence == 0,
                        NOTIFICATIONS.c.category == category.value,
                        NOTIFICATIONS.c.subject_id == task_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:  # pragma: no cover - insert above succeeded
            return
        if existing["title"] == values["title"] and existing["summary"] == values["summary"]:
            return
        updates = {
            "title": values["title"],
            "summary": values["summary"],
            "created_at": created_at,
        }
        if existing["archived_at"] is None:
            updates["read_at"] = None
        connection.execute(
            update(NOTIFICATIONS)
            .where(NOTIFICATIONS.c.notification_id == existing["notification_id"])
            .values(**updates)
        )

    @staticmethod
    def _preference(row) -> NotificationPreference:
        return NotificationPreference(
            account_id=row["account_id"],
            notify_tasks=bool(row["notify_tasks"]),
            notify_messages=bool(row["notify_messages"]),
            notify_resources=bool(row["notify_resources"]),
            notify_topics=bool(row["notify_topics"]),
            notify_agent_events=bool(row["notify_agent_events"]),
            notify_due_soon=bool(row["notify_due_soon"]),
            notify_overdue=bool(row["notify_overdue"]),
            due_soon_hours=row["due_soon_hours"],
            time_zone=row["time_zone"],
            quiet_start_minute=row["quiet_start_minute"],
            quiet_end_minute=row["quiet_end_minute"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _notification(row) -> Notification:
        return Notification(
            notification_id=row["notification_id"],
            account_id=row["account_id"],
            project_id=row["project_id"],
            activity_sequence=row["activity_sequence"],
            category=NotificationCategory(row["category"]),
            title=row["title"],
            summary=row["summary"],
            subject_id=row["subject_id"],
            read_at=row["read_at"],
            archived_at=row["archived_at"],
            created_at=row["created_at"],
        )
