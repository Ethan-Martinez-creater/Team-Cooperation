from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    exists,
    func,
    insert,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

from ..audit import AuditEvent
from ..errors import HarnessError, IdempotencyConflict
from ..postgres_audit import SQLAlchemyAuditLog
from .models import ExecutionTask, TaskLease, TaskStatus

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_PAYLOAD_BYTES = 262_144
_MAX_RESULT_BYTES = 262_144

EXECUTION_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
)
_JSON = JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

EXECUTION_TASKS = Table(
    "execution_tasks",
    EXECUTION_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("program_id", String(128), nullable=True),
    Column("assignment_id", String(128), nullable=True),
    Column("project_id", String(128), nullable=True),
    Column("process_id", String(128), nullable=True),
    Column("team_task_id", String(128), nullable=True),
    Column("work_node_id", String(128), nullable=True),
    Column("contract_version", Integer, nullable=True),
    Column("queue", String(128), nullable=False),
    Column("payload", _JSON, nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("priority", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("cancel_requested", Boolean, nullable=False),
    Column("result", _JSON, nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_execution_task_idempotency"),
    CheckConstraint("length(request_digest) = 64", name="request_digest"),
    CheckConstraint("priority BETWEEN -1000 AND 1000", name="priority"),
    CheckConstraint("max_attempts BETWEEN 1 AND 100", name="max_attempts"),
    CheckConstraint(
        "attempt_count BETWEEN 0 AND max_attempts",
        name="attempt_count",
    ),
    CheckConstraint(
        "status IN ('queued','leased','running','retry_wait','succeeded','failed','cancelled')",
        name="status",
    ),
    CheckConstraint(
        "(status IN ('leased','running') AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status NOT IN ('leased','running') AND lease_owner IS NULL "
        "AND lease_token IS NULL AND lease_expires_at IS NULL)",
        name="lease_state",
    ),
    CheckConstraint(
        "(status IN ('succeeded','failed','cancelled')) = (completed_at IS NOT NULL)",
        name="terminal_completion",
    ),
    CheckConstraint(
        "(project_id IS NULL AND process_id IS NULL AND team_task_id IS NULL "
        "AND work_node_id IS NULL AND contract_version IS NULL) OR "
        "(project_id IS NOT NULL AND process_id IS NOT NULL "
        "AND team_task_id IS NOT NULL AND work_node_id IS NOT NULL "
        "AND contract_version >= 1 AND program_id IS NULL AND assignment_id IS NULL)",
        name="project_work_binding",
    ),
)
Index(
    "ix_execution_tasks_claim",
    EXECUTION_TASKS.c.tenant_id,
    EXECUTION_TASKS.c.queue,
    EXECUTION_TASKS.c.status,
    EXECUTION_TASKS.c.available_at,
    EXECUTION_TASKS.c.priority,
)
Index(
    "uq_execution_tasks_project_contract",
    EXECUTION_TASKS.c.process_id,
    EXECUTION_TASKS.c.team_task_id,
    EXECUTION_TASKS.c.contract_version,
    unique=True,
    sqlite_where=EXECUTION_TASKS.c.process_id.is_not(None),
    postgresql_where=EXECUTION_TASKS.c.process_id.is_not(None),
)
Index(
    "ix_execution_tasks_project_work",
    EXECUTION_TASKS.c.project_id,
    EXECUTION_TASKS.c.process_id,
    EXECUTION_TASKS.c.team_task_id,
)

EXECUTION_DEPENDENCIES = Table(
    "execution_task_dependencies",
    EXECUTION_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("dependency_id", String(128), primary_key=True),
    ForeignKeyConstraint(
        ["tenant_id", "task_id"],
        ["execution_tasks.tenant_id", "execution_tasks.task_id"],
        name="fk_execution_dependency_task",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "dependency_id"],
        ["execution_tasks.tenant_id", "execution_tasks.task_id"],
        name="fk_execution_dependency_target",
        ondelete="RESTRICT",
    ),
    CheckConstraint("task_id <> dependency_id", name="not_self"),
)

EXECUTION_EVENTS = Table(
    "execution_task_events",
    EXECUTION_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("from_status", String(32), nullable=True),
    Column("to_status", String(32), nullable=False),
    Column("details", _JSON, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "task_id"],
        ["execution_tasks.tenant_id", "execution_tasks.task_id"],
        name="fk_execution_event_task",
        ondelete="RESTRICT",
    ),
    UniqueConstraint("tenant_id", "event_id", name="uq_execution_event_id"),
    CheckConstraint("sequence > 0", name="sequence"),
)


class TaskExecutionError(HarnessError):
    """A durable task command violated state, lease, or dependency rules."""


class SQLAlchemyTaskRepository:
    """Transactional durable-task queue with DAG gates and fencing-token leases."""

    def __init__(
        self,
        *,
        engine: Engine,
        audit_log: SQLAlchemyAuditLog | None = None,
        _bound_connection: Connection | None = None,
    ) -> None:
        self.engine = engine
        self.audit_log = audit_log
        self._bound_connection = _bound_connection

    def create_schema(self) -> None:
        EXECUTION_METADATA.create_all(self.engine)

    def using_connection(self, connection: Connection) -> SQLAlchemyTaskRepository:
        """Bind commands to a caller-owned transaction for atomic composition."""
        if connection.engine is not self.engine:
            raise TaskExecutionError("bound connection belongs to a different engine")
        return SQLAlchemyTaskRepository(
            engine=self.engine,
            audit_log=self.audit_log,
            _bound_connection=connection,
        )

    def enqueue(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
        task_id: str,
        queue: str,
        payload: dict[str, Any],
        dependencies: tuple[str, ...] = (),
        priority: int = 0,
        max_attempts: int = 3,
        available_at: datetime | None = None,
        program_id: str | None = None,
        assignment_id: str | None = None,
        project_id: str | None = None,
        process_id: str | None = None,
        team_task_id: str | None = None,
        work_node_id: str | None = None,
        contract_version: int | None = None,
    ) -> ExecutionTask:
        self._validate_identifier("tenant_id", tenant_id)
        self._validate_identifier("actor_id", actor_id)
        self._validate_identifier("idempotency_key", idempotency_key)
        self._validate_identifier("task_id", task_id)
        self._validate_identifier("queue", queue)
        if len(set(dependencies)) != len(dependencies) or task_id in dependencies:
            raise TaskExecutionError("task dependencies are invalid")
        for dependency in dependencies:
            self._validate_identifier("dependency_id", dependency)
        if not -1000 <= priority <= 1000 or not 1 <= max_attempts <= 100:
            raise TaskExecutionError("task priority or attempt limit is invalid")
        project_binding = (
            project_id,
            process_id,
            team_task_id,
            work_node_id,
            contract_version,
        )
        if any(value is not None for value in project_binding):
            if self._bound_connection is None:
                raise TaskExecutionError(
                    "project work must be admitted through the transactional service"
                )
            if any(value is None for value in project_binding):
                raise TaskExecutionError("project work binding must be complete")
            if program_id is not None or assignment_id is not None:
                raise TaskExecutionError(
                    "project work cannot depend on a governance assignment"
                )
            for name, value in (
                ("project_id", project_id),
                ("process_id", process_id),
                ("team_task_id", team_task_id),
                ("work_node_id", work_node_id),
            ):
                self._validate_identifier(name, value)
            if type(contract_version) is not int or contract_version < 1:
                raise TaskExecutionError("project work contract version is invalid")
        now = datetime.now(UTC)
        ready_at = self._aware(available_at or now)
        self._bounded_json(payload, _MAX_PAYLOAD_BYTES, "task payload")
        canonical_request = self._bounded_json(
            {
                "task_id": task_id,
                "queue": queue,
                "payload": payload,
                "dependencies": list(dependencies),
                "priority": priority,
                "max_attempts": max_attempts,
                "available_at": self._aware(available_at).isoformat() if available_at else None,
                "program_id": program_id,
                "assignment_id": assignment_id,
                "project_id": project_id,
                "process_id": process_id,
                "team_task_id": team_task_id,
                "work_node_id": work_node_id,
                "contract_version": contract_version,
            },
            _MAX_PAYLOAD_BYTES,
            "task request",
        )
        digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        with self._transaction(tenant_id) as connection:
            existing = (
                connection.execute(
                    select(EXECUTION_TASKS).where(
                        and_(
                            EXECUTION_TASKS.c.tenant_id == tenant_id,
                            EXECUTION_TASKS.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["request_digest"] != digest or existing["task_id"] != task_id:
                    raise IdempotencyConflict(
                        "task idempotency key was reused with different content"
                    )
                return self._load_in_transaction(connection, tenant_id, task_id)
            found_dependencies = set(
                connection.execute(
                    select(EXECUTION_TASKS.c.task_id).where(
                        and_(
                            EXECUTION_TASKS.c.tenant_id == tenant_id,
                            EXECUTION_TASKS.c.task_id.in_(dependencies),
                        )
                    )
                ).scalars()
            )
            missing = set(dependencies).difference(found_dependencies)
            if missing:
                raise TaskExecutionError("task dependency is absent or hidden")
            values = {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "program_id": program_id,
                "assignment_id": assignment_id,
                "project_id": project_id,
                "process_id": process_id,
                "team_task_id": team_task_id,
                "work_node_id": work_node_id,
                "contract_version": contract_version,
                "queue": queue,
                "payload": payload,
                "request_digest": digest,
                "idempotency_key": idempotency_key,
                "status": TaskStatus.QUEUED.value,
                "priority": priority,
                "max_attempts": max_attempts,
                "attempt_count": 0,
                "available_at": ready_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "cancel_requested": False,
                "result": None,
                "error_code": None,
                "created_by": actor_id,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            if connection.dialect.name == "postgresql":
                task_insert = (
                    postgresql_insert(EXECUTION_TASKS)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EXECUTION_TASKS.c.task_id)
                )
            else:
                task_insert = (
                    sqlite_insert(EXECUTION_TASKS)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EXECUTION_TASKS.c.task_id)
                )
            inserted = connection.execute(task_insert).scalar_one_or_none()
            if inserted is None:
                raced = (
                    connection.execute(
                        select(EXECUTION_TASKS).where(
                            and_(
                                EXECUTION_TASKS.c.tenant_id == tenant_id,
                                EXECUTION_TASKS.c.idempotency_key == idempotency_key,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if raced is None and process_id is not None:
                    raced = (
                        connection.execute(
                            select(EXECUTION_TASKS).where(
                                and_(
                                    EXECUTION_TASKS.c.process_id == process_id,
                                    EXECUTION_TASKS.c.team_task_id == team_task_id,
                                    EXECUTION_TASKS.c.contract_version
                                    == contract_version,
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                if raced is None:
                    raise IdempotencyConflict(
                        "task identity already exists with another request"
                    )
                if raced["request_digest"] != digest or raced["task_id"] != task_id:
                    raise IdempotencyConflict(
                        "task idempotency key was reused with different content"
                    )
                return self._load_in_transaction(connection, tenant_id, raced["task_id"])
            try:
                for dependency in dependencies:
                    connection.execute(
                        insert(EXECUTION_DEPENDENCIES).values(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            dependency_id=dependency,
                        )
                    )
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    actor_id=actor_id,
                    event_type="enqueued",
                    before=None,
                    after=TaskStatus.QUEUED,
                    details={"request_digest": digest, "dependency_count": len(dependencies)},
                )
            except SQLAlchemyIntegrityError as exc:
                raise TaskExecutionError("task database constraint failed") from exc
            return self._load_in_transaction(connection, tenant_id, task_id)

    def claim_next(
        self,
        *,
        tenant_id: str,
        queue: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> TaskLease | None:
        self._validate_identifier("worker_id", worker_id)
        self._validate_identifier("queue", queue)
        if not 5 <= lease_seconds <= 3600:
            raise TaskExecutionError("lease duration must be between 5 and 3600 seconds")
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            self._settle_blocked(connection, tenant_id, queue, now)
            dependencies = EXECUTION_DEPENDENCIES.alias("dependencies")
            dependency_tasks = EXECUTION_TASKS.alias("dependency_tasks")
            incomplete_dependency = exists(
                select(literal(1))
                .select_from(
                    dependencies.join(
                        dependency_tasks,
                        and_(
                            dependencies.c.tenant_id == dependency_tasks.c.tenant_id,
                            dependencies.c.dependency_id == dependency_tasks.c.task_id,
                        ),
                    )
                )
                .where(
                    and_(
                        dependencies.c.tenant_id == EXECUTION_TASKS.c.tenant_id,
                        dependencies.c.task_id == EXECUTION_TASKS.c.task_id,
                        dependency_tasks.c.status != TaskStatus.SUCCEEDED.value,
                    )
                )
            )
            statement = (
                select(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.queue == queue,
                        EXECUTION_TASKS.c.status.in_(
                            [TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value]
                        ),
                        EXECUTION_TASKS.c.available_at <= now,
                        EXECUTION_TASKS.c.cancel_requested.is_(False),
                        ~incomplete_dependency,
                    )
                )
                .order_by(
                    EXECUTION_TASKS.c.priority.desc(),
                    EXECUTION_TASKS.c.available_at,
                    EXECUTION_TASKS.c.created_at,
                )
                .limit(1)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(seconds=lease_seconds)
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == row["task_id"],
                    )
                )
                .values(
                    status=TaskStatus.LEASED.value,
                    attempt_count=int(row["attempt_count"]) + 1,
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_expires_at=expires_at,
                    updated_at=now,
                )
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=row["task_id"],
                actor_id=worker_id,
                event_type="leased",
                before=TaskStatus(row["status"]),
                after=TaskStatus.LEASED,
                details={"attempt": int(row["attempt_count"]) + 1},
            )
            task = self._load_in_transaction(connection, tenant_id, row["task_id"])
            return TaskLease(task, worker_id, token, expires_at)

    def start(self, *, tenant_id: str, task_id: str, worker_id: str, lease_token: str) -> None:
        self._lease_transition(
            tenant_id=tenant_id,
            task_id=task_id,
            worker_id=worker_id,
            lease_token=lease_token,
            allowed=(TaskStatus.LEASED,),
            target=TaskStatus.RUNNING,
            event_type="started",
        )

    def heartbeat(
        self,
        *,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> datetime:
        if not 5 <= lease_seconds <= 3600:
            raise TaskExecutionError("lease duration must be between 5 and 3600 seconds")
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(tenant_id) as connection:
            row = self._require_lease(connection, tenant_id, task_id, worker_id, lease_token, now)
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == task_id,
                    )
                )
                .values(lease_expires_at=expires_at, updated_at=now)
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                actor_id=worker_id,
                event_type="heartbeat",
                before=TaskStatus(row["status"]),
                after=TaskStatus(row["status"]),
                details={},
            )
        return expires_at

    def succeed(
        self,
        *,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> None:
        self._bounded_json(result, _MAX_RESULT_BYTES, "task result")
        self._finish(
            tenant_id=tenant_id,
            task_id=task_id,
            worker_id=worker_id,
            lease_token=lease_token,
            status=TaskStatus.SUCCEEDED,
            result=result,
            error_code=None,
        )

    def fail(
        self,
        *,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: int = 30,
    ) -> TaskStatus:
        self._validate_identifier("error_code", error_code)
        if not 0 <= retry_delay_seconds <= 86_400:
            raise TaskExecutionError("retry delay is invalid")
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._require_lease(connection, tenant_id, task_id, worker_id, lease_token, now)
            if TaskStatus(row["status"]) is not TaskStatus.RUNNING:
                raise TaskExecutionError("only a running task may fail")
            target = (
                TaskStatus.RETRY_WAIT
                if retryable and int(row["attempt_count"]) < int(row["max_attempts"])
                else TaskStatus.FAILED
            )
            values = self._clear_lease(
                status=target,
                now=now,
                completed_at=now if target is TaskStatus.FAILED else None,
                available_at=now + timedelta(seconds=retry_delay_seconds),
                error_code=error_code,
            )
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == task_id,
                    )
                )
                .values(**values)
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                actor_id=worker_id,
                event_type="retry_scheduled" if target is TaskStatus.RETRY_WAIT else "failed",
                before=TaskStatus.RUNNING,
                after=target,
                details={"error_code": error_code},
            )
            return target

    def request_cancel(self, *, tenant_id: str, task_id: str, actor_id: str) -> TaskStatus:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._task_row(connection, tenant_id, task_id, lock=True)
            current = TaskStatus(row["status"])
            if current in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return current
            if current in {TaskStatus.QUEUED, TaskStatus.RETRY_WAIT}:
                target = TaskStatus.CANCELLED
                values = self._clear_lease(status=target, now=now, completed_at=now)
            else:
                target = current
                values = {"cancel_requested": True, "updated_at": now}
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == task_id,
                    )
                )
                .values(**values)
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                actor_id=actor_id,
                event_type="cancelled" if target is TaskStatus.CANCELLED else "cancel_requested",
                before=current,
                after=target,
                details={},
            )
            return target

    def acknowledge_cancel(
        self, *, tenant_id: str, task_id: str, worker_id: str, lease_token: str
    ) -> None:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._require_lease(connection, tenant_id, task_id, worker_id, lease_token, now)
            if not row["cancel_requested"]:
                raise TaskExecutionError("task cancellation was not requested")
            current = TaskStatus(row["status"])
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == task_id,
                    )
                )
                .values(
                    **self._clear_lease(
                        status=TaskStatus.CANCELLED,
                        now=now,
                        completed_at=now,
                    )
                )
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                actor_id=worker_id,
                event_type="cancelled",
                before=current,
                after=TaskStatus.CANCELLED,
                details={},
            )

    def recover_expired(self, *, tenant_id: str, actor_id: str = "scheduler") -> int:
        now = datetime.now(UTC)
        count = 0
        with self._transaction(tenant_id) as connection:
            rows = (
                connection.execute(
                    select(EXECUTION_TASKS)
                    .where(
                        and_(
                            EXECUTION_TASKS.c.tenant_id == tenant_id,
                            EXECUTION_TASKS.c.status.in_(
                                [TaskStatus.LEASED.value, TaskStatus.RUNNING.value]
                            ),
                            EXECUTION_TASKS.c.lease_expires_at < now,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for row in rows:
                current = TaskStatus(row["status"])
                if row["cancel_requested"]:
                    target = TaskStatus.CANCELLED
                elif int(row["attempt_count"]) < int(row["max_attempts"]):
                    target = TaskStatus.RETRY_WAIT
                else:
                    target = TaskStatus.FAILED
                completed_at = now if target in {TaskStatus.CANCELLED, TaskStatus.FAILED} else None
                connection.execute(
                    update(EXECUTION_TASKS)
                    .where(
                        and_(
                            EXECUTION_TASKS.c.tenant_id == tenant_id,
                            EXECUTION_TASKS.c.task_id == row["task_id"],
                        )
                    )
                    .values(
                        **self._clear_lease(
                            status=target,
                            now=now,
                            completed_at=completed_at,
                            available_at=now,
                            error_code="lease_expired" if target is TaskStatus.FAILED else None,
                        )
                    )
                )
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    task_id=row["task_id"],
                    actor_id=actor_id,
                    event_type="lease_recovered",
                    before=current,
                    after=target,
                    details={},
                )
                count += 1
        return count

    def get(self, *, tenant_id: str, task_id: str) -> ExecutionTask:
        with self._transaction(tenant_id) as connection:
            return self._load_in_transaction(connection, tenant_id, task_id)

    def find_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ExecutionTask | None:
        self._validate_identifier("tenant_id", tenant_id)
        self._validate_identifier("idempotency_key", idempotency_key)
        with self._transaction(tenant_id) as connection:
            task_id = connection.execute(
                select(EXECUTION_TASKS.c.task_id).where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if task_id is None:
                return None
            return self._load_in_transaction(connection, tenant_id, task_id)

    def _lease_transition(
        self,
        *,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        allowed: tuple[TaskStatus, ...],
        target: TaskStatus,
        event_type: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._require_lease(connection, tenant_id, task_id, worker_id, lease_token, now)
            current = TaskStatus(row["status"])
            if current not in allowed:
                raise TaskExecutionError("task state does not permit this lease transition")
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == task_id,
                    )
                )
                .values(status=target.value, updated_at=now)
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                actor_id=worker_id,
                event_type=event_type,
                before=current,
                after=target,
                details={},
            )

    def _finish(
        self,
        *,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        status: TaskStatus,
        result: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._require_lease(connection, tenant_id, task_id, worker_id, lease_token, now)
            if TaskStatus(row["status"]) is not TaskStatus.RUNNING:
                raise TaskExecutionError("only a running task may complete")
            if row["cancel_requested"]:
                raise TaskExecutionError("task cancellation must be acknowledged before completion")
            connection.execute(
                update(EXECUTION_TASKS)
                .where(
                    and_(
                        EXECUTION_TASKS.c.tenant_id == tenant_id,
                        EXECUTION_TASKS.c.task_id == task_id,
                    )
                )
                .values(
                    **self._clear_lease(
                        status=status,
                        now=now,
                        completed_at=now,
                        result=result,
                        error_code=error_code,
                    )
                )
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                actor_id=worker_id,
                event_type=status.value,
                before=TaskStatus.RUNNING,
                after=status,
                details={},
            )

    def _settle_blocked(
        self, connection: Connection, tenant_id: str, queue: str, now: datetime
    ) -> None:
        candidate_statement = select(EXECUTION_TASKS).where(
            and_(
                EXECUTION_TASKS.c.tenant_id == tenant_id,
                EXECUTION_TASKS.c.queue == queue,
                EXECUTION_TASKS.c.status.in_(
                    [TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value]
                ),
            )
        )
        if connection.dialect.name == "postgresql":
            candidate_statement = candidate_statement.with_for_update(skip_locked=True)
        candidates = connection.execute(candidate_statement).mappings().all()
        for row in candidates:
            dependency_states = set(
                connection.execute(
                    select(EXECUTION_TASKS.c.status)
                    .select_from(
                        EXECUTION_DEPENDENCIES.join(
                            EXECUTION_TASKS,
                            and_(
                                EXECUTION_DEPENDENCIES.c.tenant_id == EXECUTION_TASKS.c.tenant_id,
                                EXECUTION_DEPENDENCIES.c.dependency_id == EXECUTION_TASKS.c.task_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            EXECUTION_DEPENDENCIES.c.tenant_id == tenant_id,
                            EXECUTION_DEPENDENCIES.c.task_id == row["task_id"],
                        )
                    )
                ).scalars()
            )
            if dependency_states.intersection(
                {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
            ):
                connection.execute(
                    update(EXECUTION_TASKS)
                    .where(
                        and_(
                            EXECUTION_TASKS.c.tenant_id == tenant_id,
                            EXECUTION_TASKS.c.task_id == row["task_id"],
                        )
                    )
                    .values(
                        **self._clear_lease(
                            status=TaskStatus.CANCELLED,
                            now=now,
                            completed_at=now,
                            error_code="dependency_terminal_failure",
                        )
                    )
                )
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    task_id=row["task_id"],
                    actor_id="scheduler",
                    event_type="dependency_cancelled",
                    before=TaskStatus(row["status"]),
                    after=TaskStatus.CANCELLED,
                    details={},
                )

    def _require_lease(
        self,
        connection: Connection,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
    ):
        row = self._task_row(connection, tenant_id, task_id, lock=True)
        if (
            row["lease_owner"] != worker_id
            or not secrets.compare_digest(row["lease_token"] or "", lease_token)
            or row["lease_expires_at"] is None
            or self._aware(row["lease_expires_at"]) <= now
        ):
            raise TaskExecutionError("task lease is invalid or expired")
        return row

    def _task_row(self, connection: Connection, tenant_id: str, task_id: str, *, lock: bool):
        statement = select(EXECUTION_TASKS).where(
            and_(
                EXECUTION_TASKS.c.tenant_id == tenant_id,
                EXECUTION_TASKS.c.task_id == task_id,
            )
        )
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise TaskExecutionError("task is absent or hidden")
        return row

    def _load_in_transaction(
        self, connection: Connection, tenant_id: str, task_id: str
    ) -> ExecutionTask:
        row = self._task_row(connection, tenant_id, task_id, lock=False)
        dependencies = tuple(
            connection.execute(
                select(EXECUTION_DEPENDENCIES.c.dependency_id)
                .where(
                    and_(
                        EXECUTION_DEPENDENCIES.c.tenant_id == tenant_id,
                        EXECUTION_DEPENDENCIES.c.task_id == task_id,
                    )
                )
                .order_by(EXECUTION_DEPENDENCIES.c.dependency_id)
            ).scalars()
        )
        return ExecutionTask(
            task_id=row["task_id"],
            tenant_id=row["tenant_id"],
            queue=row["queue"],
            payload=dict(row["payload"]),
            request_digest=row["request_digest"],
            status=TaskStatus(row["status"]),
            priority=int(row["priority"]),
            max_attempts=int(row["max_attempts"]),
            attempt_count=int(row["attempt_count"]),
            available_at=self._aware(row["available_at"]),
            dependencies=dependencies,
            created_by=row["created_by"],
            program_id=row["program_id"],
            assignment_id=row["assignment_id"],
            project_id=row["project_id"],
            process_id=row["process_id"],
            team_task_id=row["team_task_id"],
            work_node_id=row["work_node_id"],
            contract_version=row["contract_version"],
            cancel_requested=bool(row["cancel_requested"]),
            result=dict(row["result"]) if row["result"] is not None else None,
            error_code=row["error_code"],
        )

    def _event(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        task_id: str,
        actor_id: str,
        event_type: str,
        before: TaskStatus | None,
        after: TaskStatus,
        details: dict[str, Any],
    ) -> None:
        sequence = (
            int(
                connection.execute(
                    select(func.coalesce(func.max(EXECUTION_EVENTS.c.sequence), 0)).where(
                        and_(
                            EXECUTION_EVENTS.c.tenant_id == tenant_id,
                            EXECUTION_EVENTS.c.task_id == task_id,
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        connection.execute(
            insert(EXECUTION_EVENTS).values(
                tenant_id=tenant_id,
                task_id=task_id,
                sequence=sequence,
                event_id=event_id,
                event_type=event_type,
                actor_id=actor_id,
                from_status=before.value if before else None,
                to_status=after.value,
                details=details,
                occurred_at=now,
            )
        )
        if self.audit_log is not None:
            self.audit_log.append_in_transaction(
                connection,
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type=f"execution.{event_type}",
                    actor_id=actor_id,
                    outcome=after.value,
                    details={"task_id": task_id, **details},
                    correlation_id=task_id,
                    event_id=event_id,
                    occurred_at=now.isoformat(),
                ),
            )

    @staticmethod
    def _clear_lease(
        *,
        status: TaskStatus,
        now: datetime,
        completed_at: datetime | None = None,
        available_at: datetime | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "status": status.value,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "updated_at": now,
            "completed_at": completed_at,
            "result": result,
            "error_code": error_code,
        }
        if available_at is not None:
            values["available_at"] = available_at
        return values

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[Connection]:
        self._validate_identifier("tenant_id", tenant_id)
        if self._bound_connection is not None:
            self._set_tenant_context(self._bound_connection, tenant_id)
            yield self._bound_connection
            return
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            yield connection

    @staticmethod
    def _set_tenant_context(connection: Connection, tenant_id: str) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
        elif connection.dialect.name != "sqlite":
            raise TaskExecutionError("durable execution supports PostgreSQL and SQLite only")

    @staticmethod
    def _validate_identifier(name: str, value: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise TaskExecutionError(f"{name} is invalid")

    @staticmethod
    def _bounded_json(value: Any, maximum: int, name: str) -> str:
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise TaskExecutionError(f"{name} must be JSON serializable") from exc
        if len(canonical.encode("utf-8")) > maximum:
            raise TaskExecutionError(f"{name} exceeds its size limit")
        return canonical

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
