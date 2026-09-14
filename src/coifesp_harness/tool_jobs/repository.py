from __future__ import annotations

import re
import secrets
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..errors import HarnessError, IdempotencyConflict, IntegrityError
from ..postgres_audit import SQLAlchemyAuditLog
from .crypto import ToolJobKeyring
from .models import ToolJob, ToolJobLease, ToolJobStatus

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOOL_JOB_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
)

TOOL_JOBS = Table(
    "tool_jobs",
    TOOL_JOB_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("job_id", String(128), primary_key=True),
    Column("run_id", String(128), nullable=False),
    Column("call_id", String(128), nullable=False),
    Column("tool_name", String(128), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("arguments_ciphertext", LargeBinary, nullable=False),
    Column("arguments_nonce", LargeBinary, nullable=False),
    Column("arguments_fingerprint", String(64), nullable=False),
    Column("arguments_key_id", String(128), nullable=False),
    Column("result_ciphertext", LargeBinary, nullable=True),
    Column("result_nonce", LargeBinary, nullable=True),
    Column("result_fingerprint", String(64), nullable=True),
    Column("result_key_id", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_tool_jobs_idempotency"),
    UniqueConstraint("tenant_id", "run_id", "call_id", name="uq_tool_jobs_run_call"),
    CheckConstraint("length(request_digest)=64", name="request_digest"),
    CheckConstraint(
        "attempt_count BETWEEN 0 AND max_attempts AND max_attempts BETWEEN 1 AND 10",
        name="attempts",
    ),
    CheckConstraint(
        "status IN ('queued','leased','running','retry_wait','awaiting_specialist',"
        "'succeeded','failed','cancelled')",
        name="status",
    ),
    CheckConstraint(
        "(status IN ('leased','running') AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status NOT IN ('leased','running') AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)",
        name="lease_state",
    ),
    CheckConstraint(
        "(result_ciphertext IS NULL AND result_nonce IS NULL AND result_fingerprint IS NULL AND result_key_id IS NULL) OR (result_ciphertext IS NOT NULL AND result_nonce IS NOT NULL AND result_fingerprint IS NOT NULL AND result_key_id IS NOT NULL)",
        name="result_crypto",
    ),
    CheckConstraint(
        "(status IN ('succeeded','failed','cancelled'))=(completed_at IS NOT NULL)", name="terminal"
    ),
)
Index(
    "ix_tool_jobs_claim",
    TOOL_JOBS.c.tenant_id,
    TOOL_JOBS.c.status,
    TOOL_JOBS.c.available_at,
    TOOL_JOBS.c.created_at,
)

TOOL_JOB_EVENTS = Table(
    "tool_job_events",
    TOOL_JOB_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("job_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("from_status", String(32), nullable=True),
    Column("to_status", String(32), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"],
        ["tool_jobs.tenant_id", "tool_jobs.job_id"],
        name="fk_tool_job_event_job",
        ondelete="RESTRICT",
    ),
    UniqueConstraint("tenant_id", "event_id", name="uq_tool_job_event_id"),
    CheckConstraint("sequence > 0", name="sequence"),
)


class ToolJobError(HarnessError):
    pass


class SQLAlchemyToolJobRepository:
    def __init__(
        self,
        *,
        engine: Engine,
        keyring: ToolJobKeyring,
        audit_log: SQLAlchemyAuditLog | None = None,
        _bound_connection: Connection | None = None,
    ) -> None:
        self.engine, self.keyring, self.audit_log = engine, keyring, audit_log
        self._bound_connection = _bound_connection

    def using_connection(self, connection: Connection) -> "SQLAlchemyToolJobRepository":
        """Bind operations to a caller-owned transaction (tests/unit-of-work)."""
        return SQLAlchemyToolJobRepository(
            engine=self.engine,
            keyring=self.keyring,
            audit_log=self.audit_log,
            _bound_connection=connection,
        )

    def create_schema(self) -> None:
        TOOL_JOB_METADATA.create_all(self.engine)

    def enqueue(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        job_id: str,
        run_id: str,
        call_id: str,
        tool_name: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        max_attempts: int = 3,
    ) -> ToolJob:
        for name, value in (
            ("tenant", tenant_id),
            ("actor", actor_id),
            ("job", job_id),
            ("run", run_id),
            ("call", call_id),
            ("tool", tool_name),
            ("idempotency", idempotency_key),
        ):
            self._identifier(name, value)
        if not 1 <= max_attempts <= 10:
            raise ToolJobError("tool job attempt limit is invalid")
        digest = self.keyring.digest(tenant_id=tenant_id, tool_name=tool_name, arguments=arguments)
        acceptable_digests = self.keyring.digests(
            tenant_id=tenant_id, tool_name=tool_name, arguments=arguments
        )
        encrypted = self.keyring.encrypt(
            tenant_id=tenant_id, job_id=job_id, purpose="arguments", value=arguments
        )
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            existing = (
                connection.execute(
                    select(TOOL_JOBS).where(
                        and_(
                            TOOL_JOBS.c.tenant_id == tenant_id,
                            TOOL_JOBS.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["request_digest"] not in acceptable_digests
                    or existing["job_id"] != job_id
                ):
                    raise IdempotencyConflict("tool job idempotency key was reused")
                return self._row(existing, include_arguments=False)
            values = dict(
                tenant_id=tenant_id,
                job_id=job_id,
                run_id=run_id,
                call_id=call_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                request_digest=digest,
                arguments_ciphertext=encrypted.ciphertext,
                arguments_nonce=encrypted.nonce,
                arguments_fingerprint=encrypted.fingerprint,
                arguments_key_id=encrypted.key_id,
                result_ciphertext=None,
                result_nonce=None,
                result_fingerprint=None,
                result_key_id=None,
                status=ToolJobStatus.QUEUED.value,
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                error_code=None,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            statement = (
                (pg_insert if connection.dialect.name == "postgresql" else sqlite_insert)(TOOL_JOBS)
                .values(**values)
                # There are two identity constraints: the caller supplied
                # idempotency key and the model run/call pair.  Treat a race
                # on either constraint as a conflict that must be inspected,
                # rather than allowing a raw database exception to escape.
                .on_conflict_do_nothing()
                .returning(TOOL_JOBS.c.job_id)
            )
            inserted = connection.execute(statement).scalar_one_or_none()
            if inserted is None:
                raced = (
                    connection.execute(
                        select(TOOL_JOBS).where(
                            and_(
                                TOOL_JOBS.c.tenant_id == tenant_id,
                                TOOL_JOBS.c.idempotency_key == idempotency_key,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if raced is None:
                    raced = (
                        connection.execute(
                            select(TOOL_JOBS).where(
                                and_(
                                    TOOL_JOBS.c.tenant_id == tenant_id,
                                    TOOL_JOBS.c.run_id == run_id,
                                    TOOL_JOBS.c.call_id == call_id,
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                if (
                    raced is None
                    or raced["request_digest"] not in acceptable_digests
                    or raced["job_id"] != job_id
                    or raced["idempotency_key"] != idempotency_key
                ):
                    raise IdempotencyConflict("tool job idempotency key was reused")
                return self._row(raced, include_arguments=False)
            self._event(
                connection, tenant_id, job_id, actor_id, "queued", None, ToolJobStatus.QUEUED
            )
            return self.get(
                tenant_id=tenant_id, job_id=job_id, include_payloads=False, connection=connection
            )

    def claim_next(
        self, *, tenant_id: str, worker_id: str, lease_seconds: int = 60
    ) -> ToolJobLease | None:
        self._identifier("worker", worker_id)
        if not 5 <= lease_seconds <= 3600:
            raise ToolJobError("tool job lease duration is invalid")
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as c:
            q = (
                select(TOOL_JOBS)
                .where(
                    and_(
                        TOOL_JOBS.c.tenant_id == tenant_id,
                        TOOL_JOBS.c.status.in_(["queued", "retry_wait"]),
                        TOOL_JOBS.c.available_at <= now,
                    )
                )
                .order_by(TOOL_JOBS.c.available_at, TOOL_JOBS.c.created_at)
                .limit(1)
            )
            if c.dialect.name == "postgresql":
                q = q.with_for_update(skip_locked=True)
            row = c.execute(q).mappings().one_or_none()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            expiry = now + timedelta(seconds=lease_seconds)
            c.execute(
                update(TOOL_JOBS)
                .where(
                    and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == row["job_id"])
                )
                .values(
                    status="leased",
                    attempt_count=row["attempt_count"] + 1,
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_expires_at=expiry,
                    updated_at=now,
                )
            )
            self._event(
                c,
                tenant_id,
                row["job_id"],
                worker_id,
                "leased",
                ToolJobStatus(row["status"]),
                ToolJobStatus.LEASED,
            )
            job = self.get(
                tenant_id=tenant_id, job_id=row["job_id"], include_payloads=True, connection=c
            )
            return ToolJobLease(job, worker_id, token, expiry)

    def claim_next_allowed(
        self,
        *,
        allowed_tenant_ids: tuple[str, ...],
        worker_id: str,
        lease_seconds: int = 60,
    ) -> ToolJobLease | None:
        """Claim across a bounded tenant set without disabling tenant transactions."""
        if (
            not allowed_tenant_ids
            or len(allowed_tenant_ids) > 64
            or len(set(allowed_tenant_ids)) != len(allowed_tenant_ids)
        ):
            raise ToolJobError("allowed tenant set is invalid")
        for tenant_id in allowed_tenant_ids:
            self._identifier("tenant", tenant_id)
            lease = self.claim_next(
                tenant_id=tenant_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if lease is not None:
                return lease
        return None

    def start(self, *, tenant_id: str, job_id: str, worker_id: str, lease_token: str) -> None:
        self._transition(
            tenant_id,
            job_id,
            worker_id,
            lease_token,
            ToolJobStatus.LEASED,
            ToolJobStatus.RUNNING,
            "started",
        )

    def heartbeat(
        self,
        *,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> datetime:
        if not 5 <= lease_seconds <= 3600:
            raise ToolJobError("tool job lease duration is invalid")
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=lease_seconds)
        with self._transaction(tenant_id) as c:
            self._lease(c, tenant_id, job_id, worker_id, lease_token, now)
            c.execute(
                update(TOOL_JOBS)
                .where(and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == job_id))
                .values(lease_expires_at=expiry, updated_at=now)
            )
        return expiry

    def succeed(
        self, *, tenant_id: str, job_id: str, worker_id: str, lease_token: str, result: Any
    ) -> None:
        encrypted = self.keyring.encrypt(
            tenant_id=tenant_id, job_id=job_id, purpose="result", value=result
        )
        self._finish(
            tenant_id, job_id, worker_id, lease_token, ToolJobStatus.SUCCEEDED, encrypted, None
        )

    def fail(
        self,
        *,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: int = 30,
    ) -> ToolJobStatus:
        self._identifier("error_code", error_code)
        if not 1 <= retry_delay_seconds <= 3600:
            raise ToolJobError("tool job retry delay is invalid")
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as c:
            row = self._lease(c, tenant_id, job_id, worker_id, lease_token, now)
            if row["status"] != "running":
                raise ToolJobError("only running tool job may fail")
            retry = retryable and row["attempt_count"] < row["max_attempts"]
            target = ToolJobStatus.RETRY_WAIT if retry else ToolJobStatus.FAILED
            c.execute(
                update(TOOL_JOBS)
                .where(and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == job_id))
                .values(
                    status=target.value,
                    available_at=(
                        now + timedelta(seconds=retry_delay_seconds)
                        if retry
                        else row["available_at"]
                    ),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code=error_code,
                    updated_at=now,
                    completed_at=None if retry else now,
                )
            )
            self._event(
                c, tenant_id, job_id, worker_id, target.value, ToolJobStatus.RUNNING, target
            )
            return target

    def await_specialist(
        self,
        *,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: str,
        delegation_id: str,
    ) -> None:
        """Release a Tool Worker lease while a durable child AgentRun executes."""

        self._identifier("delegation", delegation_id)
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as c:
            row = self._lease(c, tenant_id, job_id, worker_id, lease_token, now)
            if row["status"] != ToolJobStatus.RUNNING.value:
                raise ToolJobError("only a running tool job may await a specialist")
            if row["tool_name"] != "specialist.delegate":
                raise ToolJobError("only the specialist delegation tool may await a child run")
            c.execute(
                update(TOOL_JOBS)
                .where(
                    and_(
                        TOOL_JOBS.c.tenant_id == tenant_id,
                        TOOL_JOBS.c.job_id == job_id,
                    )
                )
                .values(
                    status=ToolJobStatus.AWAITING_SPECIALIST.value,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code=delegation_id,
                    updated_at=now,
                    completed_at=None,
                )
            )
            self._event(
                c,
                tenant_id,
                job_id,
                worker_id,
                "awaiting_specialist",
                ToolJobStatus.RUNNING,
                ToolJobStatus.AWAITING_SPECIALIST,
            )

    def complete_specialist(
        self,
        *,
        tenant_id: str,
        job_id: str,
        delegation_id: str,
        actor_id: str,
        result: Any | None = None,
        error_code: str | None = None,
    ) -> None:
        """Complete a suspended specialist ToolJob from its trusted projector."""

        self._identifier("delegation", delegation_id)
        self._identifier("actor", actor_id)
        succeeded = result is not None and error_code is None
        failed = result is None and error_code is not None
        if not (succeeded or failed):
            raise ToolJobError("specialist completion must contain exactly one result or error")
        if error_code is not None:
            self._identifier("error_code", error_code)
        encrypted = (
            self.keyring.encrypt(
                tenant_id=tenant_id,
                job_id=job_id,
                purpose="result",
                value=result,
            )
            if succeeded
            else None
        )
        target = ToolJobStatus.SUCCEEDED if succeeded else ToolJobStatus.FAILED
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as c:
            row = (
                c.execute(
                    select(TOOL_JOBS)
                    .where(
                        and_(
                            TOOL_JOBS.c.tenant_id == tenant_id,
                            TOOL_JOBS.c.job_id == job_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["tool_name"] != "specialist.delegate":
                raise ToolJobError("specialist tool job is unavailable")
            if row["status"] in {
                ToolJobStatus.SUCCEEDED.value,
                ToolJobStatus.FAILED.value,
            }:
                stored = self._row(row, include_result=True)
                exact = (
                    row["status"] == target.value
                    and (
                        (succeeded and stored.result == result and row["error_code"] is None)
                        or (failed and row["error_code"] == error_code)
                    )
                )
                if exact:
                    return
                raise ToolJobError("specialist completion conflicts with the stored terminal result")
            if (
                row["status"] != ToolJobStatus.AWAITING_SPECIALIST.value
                or row["error_code"] != delegation_id
            ):
                raise ToolJobError("tool job is not awaiting this specialist delegation")
            values = {
                "status": target.value,
                "error_code": error_code,
                "updated_at": now,
                "completed_at": now,
            }
            if encrypted is not None:
                values.update(
                    result_ciphertext=encrypted.ciphertext,
                    result_nonce=encrypted.nonce,
                    result_fingerprint=encrypted.fingerprint,
                    result_key_id=encrypted.key_id,
                )
            c.execute(
                update(TOOL_JOBS)
                .where(
                    and_(
                        TOOL_JOBS.c.tenant_id == tenant_id,
                        TOOL_JOBS.c.job_id == job_id,
                    )
                )
                .values(**values)
            )
            self._event(
                c,
                tenant_id,
                job_id,
                actor_id,
                "specialist_completed" if succeeded else "specialist_failed",
                ToolJobStatus.AWAITING_SPECIALIST,
                target,
            )

    def recover_expired(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        retry_delay_seconds: int = 5,
        limit: int = 100,
    ) -> int:
        """Fence expired workers and make their jobs runnable or terminal.

        A reclaimed job receives a fresh lease token on its next claim.  Any
        late completion from the old worker therefore fails closed.
        """
        self._identifier("actor", actor_id)
        if not 1 <= retry_delay_seconds <= 3600:
            raise ToolJobError("tool job retry delay is invalid")
        if not 1 <= limit <= 1000:
            raise ToolJobError("tool job recovery limit is invalid")
        now = datetime.now(UTC)
        recovered = 0
        with self._transaction(tenant_id) as c:
            query = (
                select(TOOL_JOBS)
                .where(
                    and_(
                        TOOL_JOBS.c.tenant_id == tenant_id,
                        TOOL_JOBS.c.status.in_(["leased", "running"]),
                        TOOL_JOBS.c.lease_expires_at <= now,
                    )
                )
                .order_by(TOOL_JOBS.c.lease_expires_at, TOOL_JOBS.c.created_at)
                .limit(limit)
            )
            if c.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            for row in c.execute(query).mappings():
                source = ToolJobStatus(row["status"])
                retry = row["attempt_count"] < row["max_attempts"]
                target = ToolJobStatus.RETRY_WAIT if retry else ToolJobStatus.FAILED
                c.execute(
                    update(TOOL_JOBS)
                    .where(
                        and_(
                            TOOL_JOBS.c.tenant_id == tenant_id,
                            TOOL_JOBS.c.job_id == row["job_id"],
                        )
                    )
                    .values(
                        status=target.value,
                        available_at=(
                            now + timedelta(seconds=retry_delay_seconds)
                            if retry
                            else row["available_at"]
                        ),
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        error_code="lease_expired",
                        updated_at=now,
                        completed_at=None if retry else now,
                    )
                )
                self._event(
                    c,
                    tenant_id,
                    row["job_id"],
                    actor_id,
                    "lease_expired",
                    source,
                    target,
                )
                recovered += 1
        return recovered

    def get(
        self,
        *,
        tenant_id: str,
        job_id: str,
        include_payloads: bool = False,
        connection: Connection | None = None,
    ) -> ToolJob:
        if connection is None:
            with self._transaction(tenant_id) as c:
                return self.get(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    include_payloads=include_payloads,
                    connection=c,
                )
        row = (
            connection.execute(
                select(TOOL_JOBS).where(
                    and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == job_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ToolJobError("tool job is absent or hidden")
        return self._row(row, include_arguments=include_payloads, include_result=include_payloads)

    def list_for_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        include_payloads: bool = False,
        connection: Connection | None = None,
    ) -> tuple[ToolJob, ...]:
        self._identifier("run", run_id)
        if connection is None:
            with self._transaction(tenant_id) as c:
                return self.list_for_run(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    include_payloads=include_payloads,
                    connection=c,
                )
        rows = (
            connection.execute(
                select(TOOL_JOBS)
                .where(and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.run_id == run_id))
                .order_by(TOOL_JOBS.c.created_at, TOOL_JOBS.c.call_id)
            )
            .mappings()
            .all()
        )
        return tuple(
            self._row(row, include_arguments=include_payloads, include_result=include_payloads)
            for row in rows
        )

    def _finish(self, tenant_id, job_id, worker_id, lease_token, target, encrypted, error_code):
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as c:
            row = self._lease(c, tenant_id, job_id, worker_id, lease_token, now)
            if row["status"] != "running":
                raise ToolJobError("only running tool job may complete")
            c.execute(
                update(TOOL_JOBS)
                .where(and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == job_id))
                .values(
                    status=target.value,
                    result_ciphertext=encrypted.ciphertext,
                    result_nonce=encrypted.nonce,
                    result_fingerprint=encrypted.fingerprint,
                    result_key_id=encrypted.key_id,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    error_code=error_code,
                    updated_at=now,
                    completed_at=now,
                )
            )
            self._event(
                c, tenant_id, job_id, worker_id, target.value, ToolJobStatus.RUNNING, target
            )

    def _transition(self, tenant_id, job_id, worker_id, lease_token, source, target, event):
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as c:
            row = self._lease(c, tenant_id, job_id, worker_id, lease_token, now)
            if row["status"] != source.value:
                raise ToolJobError("tool job state transition is invalid")
            c.execute(
                update(TOOL_JOBS)
                .where(and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == job_id))
                .values(status=target.value, updated_at=now)
            )
            self._event(c, tenant_id, job_id, worker_id, event, source, target)

    def _lease(self, c, tenant_id, job_id, worker_id, token, now):
        q = (
            select(TOOL_JOBS)
            .where(and_(TOOL_JOBS.c.tenant_id == tenant_id, TOOL_JOBS.c.job_id == job_id))
            .with_for_update()
        )
        row = c.execute(q).mappings().one_or_none()
        if (
            row is None
            or row["lease_owner"] != worker_id
            or not secrets.compare_digest(row["lease_token"] or "", token)
            or row["lease_expires_at"] is None
            or self._aware(row["lease_expires_at"]) <= now
        ):
            raise ToolJobError("tool job lease is invalid or expired")
        return row

    def _row(self, row, include_arguments=False, include_result=False):
        args = (
            self.keyring.decrypt(
                tenant_id=row["tenant_id"],
                job_id=row["job_id"],
                purpose="arguments",
                ciphertext=row["arguments_ciphertext"],
                nonce=row["arguments_nonce"],
                fingerprint=row["arguments_fingerprint"],
                key_id=row["arguments_key_id"],
            )
            if include_arguments
            else None
        )
        result = (
            self.keyring.decrypt(
                tenant_id=row["tenant_id"],
                job_id=row["job_id"],
                purpose="result",
                ciphertext=row["result_ciphertext"],
                nonce=row["result_nonce"],
                fingerprint=row["result_fingerprint"],
                key_id=row["result_key_id"],
            )
            if include_result and row["result_ciphertext"] is not None
            else None
        )
        if args is not None and not isinstance(args, dict):
            raise IntegrityError("tool arguments plaintext is invalid")
        return ToolJob(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            run_id=row["run_id"],
            call_id=row["call_id"],
            tool_name=row["tool_name"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            status=ToolJobStatus(row["status"]),
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            available_at=self._aware(row["available_at"]),
            created_by=row["created_by"],
            arguments=args,
            result=result,
            error_code=row["error_code"],
        )

    def _event(self, c, tenant_id, job_id, actor, event, before, after):
        seq = (
            int(
                c.execute(
                    select(func.coalesce(func.max(TOOL_JOB_EVENTS.c.sequence), 0)).where(
                        and_(
                            TOOL_JOB_EVENTS.c.tenant_id == tenant_id,
                            TOOL_JOB_EVENTS.c.job_id == job_id,
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        c.execute(
            insert(TOOL_JOB_EVENTS).values(
                tenant_id=tenant_id,
                job_id=job_id,
                sequence=seq,
                event_id=event_id,
                event_type=event,
                actor_id=actor,
                from_status=before.value if before else None,
                to_status=after.value,
                occurred_at=now,
            )
        )
        if self.audit_log:
            self.audit_log.append_in_transaction(
                c,
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type=f"tool_job.{event}",
                    actor_id=actor,
                    outcome=after.value,
                    details={"job_id": job_id},
                    correlation_id=job_id,
                    event_id=event_id,
                    occurred_at=now.isoformat(),
                ),
            )

    @contextmanager
    def _transaction(self, tenant_id) -> Iterator[Connection]:
        self._identifier("tenant", tenant_id)
        if self._bound_connection is not None:
            if self._bound_connection.dialect.name == "postgresql":
                self._bound_connection.execute(
                    text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
                    {"tenant": tenant_id},
                )
            elif self._bound_connection.dialect.name != "sqlite":
                raise ToolJobError("tool jobs support PostgreSQL and SQLite only")
            yield self._bound_connection
            return
        with self.engine.begin() as c:
            if c.dialect.name == "postgresql":
                c.execute(
                    text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
                    {"tenant": tenant_id},
                )
            elif c.dialect.name != "sqlite":
                raise ToolJobError("tool jobs support PostgreSQL and SQLite only")
            yield c

    @staticmethod
    def _identifier(name, value):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ToolJobError(f"{name} identifier is invalid")

    @staticmethod
    def _aware(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
