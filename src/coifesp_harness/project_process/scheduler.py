"""Durable wakeups and fenced leases for the project orchestrator.

This module is a reliability boundary, not an orchestrator. It does not make
decisions, call a model, or create AgentRuns. A committed process event can
enqueue one wakeup and a worker can claim and finish it. Retry scheduling is
represented by available_at; no method sleeps.

retry_budget is the number of retries after the first attempt. A budget of two
therefore permits attempts 1, 2, and 3.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ..errors import GovernanceConflictError, ResourceNotFound


class ProjectProcessWakeupStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # Compatibility spellings; these are aliases, not additional states.
    QUEUED = "PENDING"
    RETRY = "RETRY_WAIT"
    SUCCEEDED = "COMPLETED"


WAKEUP_TERMINAL_STATUSES = frozenset(
    {ProjectProcessWakeupStatus.COMPLETED, ProjectProcessWakeupStatus.FAILED}
)
PROJECT_PROCESS_WAKEUP_METADATA = MetaData()
_OBJECT = JSON().with_variant(JSONB(), "postgresql")


PROJECT_PROCESS_WAKEUPS = Table(
    "project_process_wakeups",
    PROJECT_PROCESS_WAKEUP_METADATA,
    Column("wakeup_id", String(160), primary_key=True),
    # The migration adds the FK.  Runtime metadata is intentionally standalone
    # so create_schema() can be used with an already-created process schema.
    Column("process_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=True),
    Column("source_event_id", String(128), nullable=False),
    Column("source_event_type", String(128), nullable=False),
    Column("payload_json", _OBJECT, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("retry_budget", Integer, nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("fencing_token", Integer, nullable=False),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("terminal_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "process_id", "source_event_id", name="uq_project_process_wakeup_source"
    ),
    CheckConstraint(
        "status IN ('PENDING','LEASED','RETRY_WAIT','COMPLETED','FAILED')",
        name="project_wakeup_status",
    ),
    CheckConstraint("attempt >= 0", name="project_wakeup_attempt"),
    CheckConstraint("retry_budget >= 0", name="project_wakeup_retry_budget"),
    CheckConstraint("fencing_token >= 0", name="project_wakeup_fencing_token"),
    CheckConstraint(
        "(status = 'LEASED' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status <> 'LEASED' AND lease_owner IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL)",
        name="project_wakeup_lease_state",
    ),
    CheckConstraint(
        "(status IN ('COMPLETED','FAILED') AND terminal_at IS NOT NULL) OR "
        "(status NOT IN ('COMPLETED','FAILED') AND terminal_at IS NULL)",
        name="project_wakeup_terminal_time",
    ),
    CheckConstraint("length(payload_sha256) = 64", name="project_wakeup_payload_digest"),
)
Index(
    "ix_project_wakeup_claim",
    PROJECT_PROCESS_WAKEUPS.c.status,
    PROJECT_PROCESS_WAKEUPS.c.available_at,
    PROJECT_PROCESS_WAKEUPS.c.created_at,
)
Index(
    "ix_project_wakeup_process_status",
    PROJECT_PROCESS_WAKEUPS.c.process_id,
    PROJECT_PROCESS_WAKEUPS.c.status,
)
Index(
    "ix_project_wakeup_lease_expiry",
    PROJECT_PROCESS_WAKEUPS.c.status,
    PROJECT_PROCESS_WAKEUPS.c.lease_expires_at,
)


@dataclass(frozen=True, slots=True)
class ProjectProcessWakeup:
    wakeup_id: str
    process_id: str
    project_id: str | None
    source_event_id: str
    source_event_type: str
    payload: dict
    payload_sha256: str
    status: ProjectProcessWakeupStatus
    available_at: datetime
    attempt: int
    retry_budget: int
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    fencing_token: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None

    @property
    def attempt_count(self) -> int:
        return self.attempt

    @property
    def retry_count(self) -> int:
        return max(0, self.attempt - 1)

    @property
    def max_retries(self) -> int:
        return self.retry_budget

    @property
    def token(self) -> str | None:
        return self.lease_token

    @property
    def owner(self) -> str | None:
        return self.lease_owner

    @property
    def lease_fencing_token(self) -> int:
        return self.fencing_token


class SQLAlchemyProjectProcessWakeupRepository:
    """Connection-scoped persistence primitives for project wakeups."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_schema(self) -> None:
        PROJECT_PROCESS_WAKEUP_METADATA.create_all(self.engine)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            # pysqlite defers the physical BEGIN until the first write.  If
            # the first write is a SAVEPOINT (used below to contain a
            # duplicate-key race), releasing that savepoint can otherwise
            # commit the write before the caller's outer transaction exits.
            # Start the physical transaction explicitly so
            # enqueue_in_transaction remains atomic with the caller's
            # business mutation on SQLite as well as PostgreSQL.
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection

    @staticmethod
    def canonical_payload(payload: dict) -> tuple[dict, str]:
        if not isinstance(payload, dict):
            raise TypeError("project process wakeup payload must be an object")
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("project process wakeup payload is too large")
        return payload, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def wakeup(connection: Connection, wakeup_id: str) -> ProjectProcessWakeup | None:
        row = (
            connection.execute(
                select(PROJECT_PROCESS_WAKEUPS).where(
                    PROJECT_PROCESS_WAKEUPS.c.wakeup_id == wakeup_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return SQLAlchemyProjectProcessWakeupRepository._model(row) if row else None

    get = wakeup

    @staticmethod
    def by_source(connection: Connection, process_id: str, source_event_id: str):
        row = (
            connection.execute(
                select(PROJECT_PROCESS_WAKEUPS).where(
                    and_(
                        PROJECT_PROCESS_WAKEUPS.c.process_id == process_id,
                        PROJECT_PROCESS_WAKEUPS.c.source_event_id == source_event_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return SQLAlchemyProjectProcessWakeupRepository._model(row) if row else None

    find_by_source = by_source

    @staticmethod
    def list_for_process(
        connection: Connection, process_id: str
    ) -> tuple[ProjectProcessWakeup, ...]:
        rows = (
            connection.execute(
                select(PROJECT_PROCESS_WAKEUPS)
                .where(PROJECT_PROCESS_WAKEUPS.c.process_id == process_id)
                .order_by(PROJECT_PROCESS_WAKEUPS.c.created_at, PROJECT_PROCESS_WAKEUPS.c.wakeup_id)
            )
            .mappings()
            .all()
        )
        return tuple(SQLAlchemyProjectProcessWakeupRepository._model(row) for row in rows)

    @staticmethod
    def enqueue(connection: Connection, values: dict) -> ProjectProcessWakeup:
        existing = SQLAlchemyProjectProcessWakeupRepository.by_source(
            connection, values["process_id"], values["source_event_id"]
        )
        if existing is not None:
            SQLAlchemyProjectProcessWakeupRepository._assert_same_identity(existing, values)
            return existing
        existing = SQLAlchemyProjectProcessWakeupRepository.wakeup(
            connection, values["wakeup_id"]
        )
        if existing is not None:
            SQLAlchemyProjectProcessWakeupRepository._assert_same_identity(existing, values)
            return existing
        try:
            with connection.begin_nested():
                connection.execute(PROJECT_PROCESS_WAKEUPS.insert().values(**values))
        except IntegrityError:
            existing = SQLAlchemyProjectProcessWakeupRepository.by_source(
                connection, values["process_id"], values["source_event_id"]
            )
            if existing is not None:
                SQLAlchemyProjectProcessWakeupRepository._assert_same_identity(existing, values)
                return existing
            existing = SQLAlchemyProjectProcessWakeupRepository.wakeup(
                connection, values["wakeup_id"]
            )
            if existing is not None:
                SQLAlchemyProjectProcessWakeupRepository._assert_same_identity(existing, values)
                return existing
            raise
        result = SQLAlchemyProjectProcessWakeupRepository.wakeup(connection, values["wakeup_id"])
        if result is None:
            raise ResourceNotFound("project process wakeup insert was not visible")
        return result

    @staticmethod
    def _assert_same_identity(existing: ProjectProcessWakeup, values: dict) -> None:
        comparable = (
            (existing.wakeup_id, values["wakeup_id"]),
            (existing.process_id, values["process_id"]),
            (existing.project_id, values.get("project_id")),
            (existing.source_event_id, values["source_event_id"]),
            (existing.source_event_type, values["source_event_type"]),
            (existing.payload_sha256, values["payload_sha256"]),
            (existing.retry_budget, values["retry_budget"]),
        )
        if any(left != right for left, right in comparable):
            raise GovernanceConflictError(
                "project process wakeup key was reused with different content"
            )

    @staticmethod
    def _model(row) -> ProjectProcessWakeup:
        def aware(value):
            return (
                value.replace(tzinfo=UTC)
                if value is not None and value.tzinfo is None
                else value
            )

        return ProjectProcessWakeup(
            row["wakeup_id"],
            row["process_id"],
            row["project_id"],
            row["source_event_id"],
            row["source_event_type"],
            dict(row["payload_json"]),
            row["payload_sha256"],
            ProjectProcessWakeupStatus(row["status"]),
            aware(row["available_at"]),
            row["attempt"],
            row["retry_budget"],
            row["lease_owner"],
            row["lease_token"],
            aware(row["lease_expires_at"]),
            row["fencing_token"],
            row["last_error"],
            aware(row["created_at"]),
            aware(row["updated_at"]),
            aware(row["terminal_at"]),
        )


class ProjectProcessScheduler:
    """Transactional wakeup lifecycle service for independent workers."""

    def __init__(
        self,
        repository: SQLAlchemyProjectProcessWakeupRepository,
        *,
        clock=None,
        token_factory=None,
        retry_delay=None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))
        self.token_factory = token_factory or (lambda: uuid4().hex)
        self.retry_delay = retry_delay

    def enqueue(
        self,
        *,
        process_id: str,
        source_event_id: str | None = None,
        source_event_type: str | None = None,
        payload: dict,
        project_id: str | None = None,
        wakeup_id: str | None = None,
        available_at: datetime | None = None,
        retry_budget: int = 3,
        event_id: str | None = None,
        event_type: str | None = None,
    ) -> ProjectProcessWakeup:
        source_event_id = source_event_id or event_id
        source_event_type = source_event_type or event_type
        self._require_text(process_id, "process_id")
        self._require_text(source_event_id, "source_event_id")
        self._require_text(source_event_type, "source_event_type")
        self._validate_budget(retry_budget)
        normalized, digest = self.repository.canonical_payload(payload)
        now = self._now()
        wakeup_id = wakeup_id or self._default_id(process_id, source_event_id)
        if len(wakeup_id) > 160:
            raise ValueError("project process wakeup id is too long")
        values = self._values(
            wakeup_id=wakeup_id,
            process_id=process_id,
            project_id=project_id,
            source_event_id=source_event_id,
            source_event_type=source_event_type,
            payload=normalized,
            payload_sha256=digest,
            available_at=available_at or now,
            retry_budget=retry_budget,
            now=now,
        )
        with self.repository.transaction() as connection:
            return self.repository.enqueue(connection, values)

    def enqueue_in_transaction(
        self,
        connection: Connection,
        *,
        process_id: str,
        source_event_id: str | None = None,
        source_event_type: str | None = None,
        payload: dict,
        project_id: str | None = None,
        wakeup_id: str | None = None,
        available_at: datetime | None = None,
        retry_budget: int = 3,
        event_id: str | None = None,
        event_type: str | None = None,
    ) -> ProjectProcessWakeup:
        source_event_id = source_event_id or event_id
        source_event_type = source_event_type or event_type
        self._require_text(process_id, "process_id")
        self._require_text(source_event_id, "source_event_id")
        self._require_text(source_event_type, "source_event_type")
        self._validate_budget(retry_budget)
        normalized, digest = self.repository.canonical_payload(payload)
        now = self._now()
        wakeup_id = wakeup_id or self._default_id(process_id, source_event_id)
        if len(wakeup_id) > 160:
            raise ValueError("project process wakeup id is too long")
        return self.repository.enqueue(
            connection,
            self._values(
                wakeup_id=wakeup_id,
                process_id=process_id,
                project_id=project_id,
                source_event_id=source_event_id,
                source_event_type=source_event_type,
                payload=normalized,
                payload_sha256=digest,
                available_at=available_at or now,
                retry_budget=retry_budget,
                now=now,
            ),
        )

    def claim(
        self,
        *,
        owner: str | None = None,
        worker_id: str | None = None,
        process_id: str | None = None,
        lease_seconds: int = 30,
        lease_ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> ProjectProcessWakeup | None:
        owner = owner or worker_id
        if lease_ttl_seconds is not None:
            lease_seconds = lease_ttl_seconds
        self._require_text(owner, "owner")
        self._validate_lease(lease_seconds)
        current = now or self._now()
        token = self.token_factory()
        self._require_text(token, "lease token")
        expires = current + timedelta(seconds=lease_seconds)
        with self.repository.transaction(immediate=True) as connection:
            eligible = or_(
                PROJECT_PROCESS_WAKEUPS.c.status.in_(
                    [
                        ProjectProcessWakeupStatus.PENDING.value,
                        ProjectProcessWakeupStatus.RETRY_WAIT.value,
                    ]
                ),
                and_(
                    PROJECT_PROCESS_WAKEUPS.c.status == ProjectProcessWakeupStatus.LEASED.value,
                    PROJECT_PROCESS_WAKEUPS.c.lease_expires_at <= current,
                ),
            )
            eligible = and_(eligible, PROJECT_PROCESS_WAKEUPS.c.available_at <= current)
            if process_id is not None:
                eligible = and_(eligible, PROJECT_PROCESS_WAKEUPS.c.process_id == process_id)
            row = (
                connection.execute(
                    select(PROJECT_PROCESS_WAKEUPS)
                    .where(eligible)
                    .order_by(
                        PROJECT_PROCESS_WAKEUPS.c.available_at,
                        PROJECT_PROCESS_WAKEUPS.c.created_at,
                        PROJECT_PROCESS_WAKEUPS.c.wakeup_id,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            changed = connection.execute(
                PROJECT_PROCESS_WAKEUPS.update()
                .where(
                    and_(
                        PROJECT_PROCESS_WAKEUPS.c.wakeup_id == row["wakeup_id"],
                        eligible,
                    )
                )
                .values(
                    status=ProjectProcessWakeupStatus.LEASED.value,
                    attempt=PROJECT_PROCESS_WAKEUPS.c.attempt + 1,
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=expires,
                    fencing_token=PROJECT_PROCESS_WAKEUPS.c.fencing_token + 1,
                    last_error=None,
                    updated_at=current,
                    terminal_at=None,
                )
            ).rowcount
            if changed != 1:
                return None
            return self.repository.wakeup(connection, row["wakeup_id"])

    claim_next = claim

    def heartbeat(
        self,
        *,
        wakeup_id: str,
        owner: str,
        fencing_token: int,
        lease_token: str | None = None,
        token: str | None = None,
        lease_seconds: int = 30,
        lease_ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> ProjectProcessWakeup:
        self._require_text(owner, "owner")
        if lease_ttl_seconds is not None:
            lease_seconds = lease_ttl_seconds
        self._validate_lease(lease_seconds)
        lease_token = self._resolve_lease_token(lease_token, token)
        current = now or self._now()
        with self.repository.transaction() as connection:
            changed = connection.execute(
                PROJECT_PROCESS_WAKEUPS.update()
                .where(
                    self._lease_condition(
                        wakeup_id=wakeup_id,
                        owner=owner,
                        fencing_token=fencing_token,
                        lease_token=lease_token,
                        now=current,
                    )
                )
                .values(
                    lease_expires_at=current + timedelta(seconds=lease_seconds),
                    updated_at=current,
                )
            ).rowcount
            if changed != 1:
                raise GovernanceConflictError("project process wakeup fencing token is stale")
            return self.repository.wakeup(connection, wakeup_id)

    def complete(
        self,
        *,
        wakeup_id: str,
        owner: str,
        fencing_token: int,
        lease_token: str | None = None,
        token: str | None = None,
        now: datetime | None = None,
    ) -> ProjectProcessWakeup:
        self._require_text(owner, "owner")
        current = now or self._now()
        lease_token = self._resolve_lease_token(lease_token, token)
        with self.repository.transaction() as connection:
            changed = connection.execute(
                PROJECT_PROCESS_WAKEUPS.update()
                .where(
                    self._lease_condition(
                        wakeup_id=wakeup_id,
                        owner=owner,
                        fencing_token=fencing_token,
                        lease_token=lease_token,
                        now=current,
                    )
                )
                .values(
                    status=ProjectProcessWakeupStatus.COMPLETED.value,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    terminal_at=current,
                    updated_at=current,
                    last_error=None,
                )
            ).rowcount
            if changed != 1:
                raise GovernanceConflictError("project process wakeup fencing token is stale")
            return self.repository.wakeup(connection, wakeup_id)

    complete_wakeup = complete

    def retry(
        self,
        *,
        wakeup_id: str,
        owner: str,
        fencing_token: int,
        error: str,
        lease_token: str | None = None,
        token: str | None = None,
        retry_after_seconds: float | None = None,
        delay_seconds: float | None = None,
        retry_after: float | None = None,
        now: datetime | None = None,
    ) -> ProjectProcessWakeup:
        self._require_text(owner, "owner")
        self._require_text(error, "retry error")
        current = now or self._now()
        lease_token = self._resolve_lease_token(lease_token, token)
        selected_delay = retry_after_seconds
        if selected_delay is None:
            selected_delay = delay_seconds if delay_seconds is not None else retry_after
        with self.repository.transaction() as connection:
            row = self.repository.wakeup(connection, wakeup_id)
            if row is None:
                raise ResourceNotFound("project process wakeup is unavailable")
            if not self._lease_matches(
                row,
                owner=owner,
                fencing_token=fencing_token,
                lease_token=lease_token,
                now=current,
            ):
                raise GovernanceConflictError("project process wakeup fencing token is stale")
            exhausted = row.attempt >= row.retry_budget + 1
            terminal = exhausted
            values = {
                "status": (
                    ProjectProcessWakeupStatus.FAILED.value
                    if terminal
                    else ProjectProcessWakeupStatus.RETRY_WAIT.value
                ),
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error": error[:2000],
                "updated_at": current,
                "terminal_at": current if terminal else None,
            }
            if not terminal:
                delay = self._retry_delay(
                    attempt=row.attempt, error=error, explicit=selected_delay
                )
                values["available_at"] = current + timedelta(seconds=delay)
            changed = connection.execute(
                PROJECT_PROCESS_WAKEUPS.update()
                .where(
                    self._lease_condition(
                        wakeup_id=wakeup_id,
                        owner=owner,
                        fencing_token=fencing_token,
                        lease_token=lease_token,
                        now=current,
                    )
                )
                .values(**values)
            ).rowcount
            if changed != 1:
                raise GovernanceConflictError("project process wakeup fencing token is stale")
            return self.repository.wakeup(connection, wakeup_id)

    retry_or_fail = retry

    def fail(
        self,
        *,
        wakeup_id: str,
        owner: str,
        fencing_token: int,
        error: str,
        lease_token: str | None = None,
        token: str | None = None,
        now: datetime | None = None,
    ) -> ProjectProcessWakeup:
        self._require_text(owner, "owner")
        self._require_text(error, "failure error")
        current = now or self._now()
        lease_token = self._resolve_lease_token(lease_token, token)
        with self.repository.transaction() as connection:
            changed = connection.execute(
                PROJECT_PROCESS_WAKEUPS.update()
                .where(
                    self._lease_condition(
                        wakeup_id=wakeup_id,
                        owner=owner,
                        fencing_token=fencing_token,
                        lease_token=lease_token,
                        now=current,
                    )
                )
                .values(
                    status=ProjectProcessWakeupStatus.FAILED.value,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=error[:2000],
                    updated_at=current,
                    terminal_at=current,
                )
            ).rowcount
            if changed != 1:
                raise GovernanceConflictError("project process wakeup fencing token is stale")
            return self.repository.wakeup(connection, wakeup_id)

    fail_wakeup = fail

    def assert_fence_in_transaction(
        self,
        connection: Connection,
        *,
        wakeup_id: str,
        owner: str,
        fencing_token: int,
        lease_token: str | None = None,
        token: str | None = None,
        now: datetime | None = None,
    ) -> ProjectProcessWakeup:
        """Validate and lock a lease inside the caller's transaction.

        An orchestrator calls this before mutating process state so the
        ownership and fencing check is part of that same transaction.  The
        random lease token is required even when a fencing token is supplied;
        omitting it is never a weaker compatibility mode.
        """
        self._require_text(owner, "owner")
        lease_token = self._resolve_lease_token(lease_token, token)
        current = now or self._now()
        row = (
            connection.execute(
                select(PROJECT_PROCESS_WAKEUPS)
                .where(PROJECT_PROCESS_WAKEUPS.c.wakeup_id == wakeup_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project process wakeup is unavailable")
        wakeup = self.repository._model(row)
        if not self._lease_matches(
            wakeup,
            owner=owner,
            fencing_token=fencing_token,
            lease_token=lease_token,
            now=current,
        ):
            raise GovernanceConflictError("project process wakeup fencing token is stale")
        return wakeup

    def _lease_condition(
        self,
        *,
        wakeup_id: str,
        owner: str,
        fencing_token: int,
        lease_token: str,
        now: datetime,
    ):
        condition = and_(
            PROJECT_PROCESS_WAKEUPS.c.wakeup_id == wakeup_id,
            PROJECT_PROCESS_WAKEUPS.c.status == ProjectProcessWakeupStatus.LEASED.value,
            PROJECT_PROCESS_WAKEUPS.c.lease_owner == owner,
            PROJECT_PROCESS_WAKEUPS.c.fencing_token == fencing_token,
            PROJECT_PROCESS_WAKEUPS.c.lease_expires_at > now,
        )
        return and_(condition, PROJECT_PROCESS_WAKEUPS.c.lease_token == lease_token)

    @staticmethod
    def _lease_matches(
        row: ProjectProcessWakeup,
        *,
        owner: str,
        fencing_token: int,
        lease_token: str,
        now: datetime,
    ) -> bool:
        return bool(
            row.status is ProjectProcessWakeupStatus.LEASED
            and row.lease_owner == owner
            and row.fencing_token == fencing_token
            and row.lease_token == lease_token
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
        )

    def _retry_delay(self, *, attempt: int, error: str, explicit) -> float:
        value = explicit
        if value is None:
            if self.retry_delay is None:
                value = min(300, 2 ** max(0, attempt - 1))
            elif callable(self.retry_delay):
                try:
                    parameters = inspect.signature(self.retry_delay).parameters
                except (TypeError, ValueError):
                    parameters = {}
                value = (
                    self.retry_delay(attempt, error)
                    if len(parameters) >= 2
                    else self.retry_delay(attempt)
                )
            else:
                value = self.retry_delay
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("project process wakeup retry delay is invalid") from exc
        if value < 0:
            raise ValueError("project process wakeup retry delay is invalid")
        return value

    @classmethod
    def _resolve_lease_token(cls, lease_token: str | None, token: str | None) -> str:
        selected = lease_token if lease_token is not None else token
        cls._require_text(selected, "lease token")
        return selected

    @staticmethod
    def _values(**values) -> dict:
        return {
            "wakeup_id": values["wakeup_id"],
            "process_id": values["process_id"],
            "project_id": values["project_id"],
            "source_event_id": values["source_event_id"],
            "source_event_type": values["source_event_type"],
            "payload_json": values["payload"],
            "payload_sha256": values["payload_sha256"],
            "status": ProjectProcessWakeupStatus.PENDING.value,
            "available_at": values["available_at"],
            "attempt": 0,
            "retry_budget": values["retry_budget"],
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "fencing_token": 0,
            "last_error": None,
            "created_at": values["now"],
            "updated_at": values["now"],
            "terminal_at": None,
        }

    def _now(self) -> datetime:
        value = self.clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @staticmethod
    def _default_id(process_id: str, source_event_id: str) -> str:
        value = f"wakeup:{process_id}:{source_event_id}"
        if len(value) <= 160:
            return value
        return f"wakeup:{hashlib.sha256(value.encode()).hexdigest()}"

    @staticmethod
    def _require_text(value: str | None, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"project process wakeup {name} is invalid")

    @staticmethod
    def _validate_budget(value: int) -> None:
        if not isinstance(value, int) or value < 0:
            raise ValueError("project process wakeup retry budget is invalid")

    @staticmethod
    def _validate_lease(value: int) -> None:
        if not isinstance(value, int) or value < 1 or value > 3600:
            raise ValueError("project process wakeup lease ttl is invalid")


ProjectOrchestratorWakeup = ProjectProcessWakeup
ProjectOrchestratorWakeupStatus = ProjectProcessWakeupStatus
ProjectOrchestratorWakeupRepository = SQLAlchemyProjectProcessWakeupRepository
ProjectProcessWakeupRepository = SQLAlchemyProjectProcessWakeupRepository
ProjectOrchestratorScheduler = ProjectProcessScheduler
ProjectProcessWakeupService = ProjectProcessScheduler


__all__ = [
    "PROJECT_PROCESS_WAKEUPS",
    "PROJECT_PROCESS_WAKEUP_METADATA",
    "WAKEUP_TERMINAL_STATUSES",
    "ProjectOrchestratorScheduler",
    "ProjectOrchestratorWakeup",
    "ProjectOrchestratorWakeupRepository",
    "ProjectOrchestratorWakeupStatus",
    "ProjectProcessScheduler",
    "ProjectProcessWakeup",
    "ProjectProcessWakeupRepository",
    "ProjectProcessWakeupService",
    "ProjectProcessWakeupStatus",
    "SQLAlchemyProjectProcessWakeupRepository",
]
