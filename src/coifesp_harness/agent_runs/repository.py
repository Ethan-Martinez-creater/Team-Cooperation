from __future__ import annotations

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
    Text,
    UniqueConstraint,
    and_,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..errors import HarnessError, IdempotencyConflict, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security.redaction import SecretRedactor
from .control_crypto import AgentControlKeyring
from .control_models import AgentControlCommand, AgentControlStatus, AgentControlType
from .crypto import AgentCheckpointKeyring
from .models import (
    TERMINAL_RUN_STATES,
    AgentRunLease,
    DurableAgentEvent,
    DurableAgentRun,
    DurableRunStatus,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JSON = JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

AGENT_RUN_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
)
AGENT_RUNS = Table(
    "agent_runs",
    AGENT_RUN_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("owner_principal_id", String(128), nullable=False),
    Column("correlation_id", String(128), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("turns", Integer, nullable=False),
    Column("tool_calls", Integer, nullable=False),
    Column("total_tokens", BigInteger, nullable=False),
    Column("model_cost_microusd", BigInteger, nullable=False),
    Column("pending_call_id", String(128), nullable=True),
    Column("pending_approval_id", String(128), nullable=True),
    Column("failure_count", Integer, nullable=False),
    Column("max_failures", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("last_error_code", String(64), nullable=True),
    Column("checkpoint_ciphertext", LargeBinary, nullable=False),
    Column("checkpoint_nonce", LargeBinary, nullable=False),
    Column("checkpoint_fingerprint", String(64), nullable=False),
    Column("checkpoint_key_id", String(128), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_agent_run_idempotency"),
    CheckConstraint("length(request_digest) = 64", name="request_digest"),
    CheckConstraint("length(checkpoint_fingerprint) = 64", name="checkpoint_fingerprint"),
    CheckConstraint("version > 0", name="version"),
    CheckConstraint(
        "failure_count >= 0 AND max_failures BETWEEN 1 AND 20 " "AND failure_count <= max_failures",
        name="failure_budget",
    ),
    CheckConstraint(
        "turns >= 0 AND tool_calls >= 0 AND total_tokens >= 0 " "AND model_cost_microusd >= 0",
        name="usage_nonnegative",
    ),
    CheckConstraint(
        "status IN ('queued','leased','running','awaiting_approval','awaiting_tool',"
        "'completed','failed','cancelled')",
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
        "(status = 'awaiting_approval') = "
        "(pending_call_id IS NOT NULL AND pending_approval_id IS NOT NULL)",
        name="pending_approval",
    ),
    CheckConstraint(
        "(status IN ('completed','failed','cancelled')) = (completed_at IS NOT NULL)",
        name="completion",
    ),
)
Index(
    "ix_agent_runs_claim",
    AGENT_RUNS.c.tenant_id,
    AGENT_RUNS.c.status,
    AGENT_RUNS.c.next_attempt_at,
    AGENT_RUNS.c.updated_at,
)
AGENT_RUN_COMMANDS = Table(
    "agent_run_commands",
    AGENT_RUN_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("command_id", String(128), nullable=False),
    Column("command_type", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("content_ciphertext", LargeBinary, nullable=False),
    Column("content_nonce", LargeBinary, nullable=False),
    Column("content_fingerprint", String(64), nullable=False),
    Column("content_key_id", String(128), nullable=False),
    Column("submitted_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    Column("applied_run_version", Integer, nullable=True),
    Column("rejected_at", DateTime(timezone=True), nullable=True),
    Column("rejection_code", String(64), nullable=True),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.run_id"],
        name="fk_agent_run_command_run",
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "tenant_id",
        "run_id",
        "command_id",
        name="uq_agent_run_command_id",
    ),
    CheckConstraint("sequence > 0", name="sequence"),
    CheckConstraint("length(request_digest) = 64", name="request_digest"),
    CheckConstraint("length(content_fingerprint) = 64", name="content_fingerprint"),
    CheckConstraint("command_type IN ('steer','follow_up')", name="type"),
    CheckConstraint("status IN ('pending','applied','rejected')", name="status"),
    CheckConstraint(
        "(status = 'pending' AND applied_at IS NULL AND applied_run_version IS NULL "
        "AND rejected_at IS NULL AND rejection_code IS NULL) OR "
        "(status = 'applied' AND applied_at IS NOT NULL AND applied_run_version IS NOT NULL "
        "AND rejected_at IS NULL AND rejection_code IS NULL) OR "
        "(status = 'rejected' AND applied_at IS NULL AND applied_run_version IS NULL "
        "AND rejected_at IS NOT NULL AND rejection_code IS NOT NULL)",
        name="lifecycle_state",
    ),
)
Index(
    "ix_agent_run_commands_pending",
    AGENT_RUN_COMMANDS.c.tenant_id,
    AGENT_RUN_COMMANDS.c.run_id,
    AGENT_RUN_COMMANDS.c.status,
    AGENT_RUN_COMMANDS.c.sequence,
)
AGENT_RUN_EVENTS = Table(
    "agent_run_events",
    AGENT_RUN_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("data", _JSON, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.run_id"],
        name="fk_agent_run_event_run",
        ondelete="RESTRICT",
    ),
    UniqueConstraint("tenant_id", "event_id", name="uq_agent_run_event_id"),
    CheckConstraint("sequence > 0", name="sequence"),
)
Index(
    "ix_agent_run_events_stream",
    AGENT_RUN_EVENTS.c.tenant_id,
    AGENT_RUN_EVENTS.c.run_id,
    AGENT_RUN_EVENTS.c.sequence,
)


class AgentRunPersistenceError(HarnessError):
    """A durable Agent run violated state, lease, or checkpoint invariants."""


class SQLAlchemyAgentRunRepository:
    def __init__(
        self,
        *,
        engine: Engine,
        keyring: AgentCheckpointKeyring,
        control_keyring: AgentControlKeyring | None = None,
        audit_log: SQLAlchemyAuditLog | None = None,
        _bound_connection: Connection | None = None,
    ) -> None:
        self.engine = engine
        self.keyring = keyring
        self.control_keyring = control_keyring
        self.audit_log = audit_log
        self._bound_connection = _bound_connection
        self._redactor = SecretRedactor()

    def create_schema(self) -> None:
        AGENT_RUN_METADATA.create_all(self.engine)

    def using_connection(self, connection: Connection) -> SQLAlchemyAgentRunRepository:
        if connection.engine is not self.engine:
            raise AgentRunPersistenceError("bound connection belongs to a different engine")
        return SQLAlchemyAgentRunRepository(
            engine=self.engine,
            keyring=self.keyring,
            control_keyring=self.control_keyring,
            audit_log=self.audit_log,
            _bound_connection=connection,
        )

    def create(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        correlation_id: str,
        idempotency_key: str,
        checkpoint: dict[str, Any],
        max_failures: int = 3,
    ) -> DurableAgentRun:
        for name, value in (
            ("tenant_id", tenant_id),
            ("owner_principal_id", owner_principal_id),
            ("run_id", run_id),
            ("correlation_id", correlation_id),
            ("idempotency_key", idempotency_key),
        ):
            self._identifier(name, value)
        if not 1 <= max_failures <= 20:
            raise AgentRunPersistenceError("agent run failure budget is invalid")
        digest = self.keyring.request_digest(
            tenant_id,
            {"checkpoint": checkpoint, "max_failures": max_failures},
        )
        acceptable_digests = self.keyring.request_digests(
            tenant_id, {"checkpoint": checkpoint, "max_failures": max_failures}
        )
        encrypted = self.keyring.encrypt(
            tenant_id=tenant_id,
            run_id=run_id,
            version=1,
            checkpoint=checkpoint,
        )
        now = datetime.now(UTC)
        values = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "owner_principal_id": owner_principal_id,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "request_digest": digest,
            "status": DurableRunStatus.QUEUED.value,
            "version": 1,
            "turns": 0,
            "tool_calls": 0,
            "total_tokens": 0,
            "model_cost_microusd": 0,
            "pending_call_id": None,
            "pending_approval_id": None,
            "failure_count": 0,
            "max_failures": max_failures,
            "next_attempt_at": None,
            "last_error_code": None,
            "checkpoint_ciphertext": encrypted.ciphertext,
            "checkpoint_nonce": encrypted.nonce,
            "checkpoint_fingerprint": encrypted.fingerprint,
            "checkpoint_key_id": encrypted.key_id,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        with self._transaction(tenant_id) as connection:
            dialect_insert = (
                postgresql_insert(AGENT_RUNS)
                if connection.dialect.name == "postgresql"
                else sqlite_insert(AGENT_RUNS)
            )
            inserted = connection.execute(
                dialect_insert.values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(AGENT_RUNS.c.run_id)
            ).scalar_one_or_none()
            if inserted is None:
                existing = (
                    connection.execute(
                        select(AGENT_RUNS).where(
                            and_(
                                AGENT_RUNS.c.tenant_id == tenant_id,
                                AGENT_RUNS.c.idempotency_key == idempotency_key,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    existing["request_digest"] not in acceptable_digests
                    or existing["run_id"] != run_id
                ):
                    raise IdempotencyConflict(
                        "agent run idempotency key was reused with different content"
                    )
                return self._record(existing)
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=owner_principal_id,
                event_type="agent_run.queued",
                data={"status": "queued", "version": 1},
            )
            return self._load(connection, tenant_id, run_id)

    def submit_control(
        self,
        *,
        tenant_id: str,
        run_id: str,
        actor_id: str,
        command_id: str,
        command_type: AgentControlType,
        content: str,
        expected_run_version: int,
    ) -> AgentControlCommand:
        if self.control_keyring is None:
            raise AgentRunPersistenceError("agent control encryption is not configured")
        for name, value in (
            ("actor_id", actor_id),
            ("command_id", command_id),
        ):
            self._identifier(name, value)
        if not isinstance(command_type, AgentControlType):
            raise AgentRunPersistenceError("agent control type is invalid")
        digest = self.control_keyring.request_digest(
            tenant_id=tenant_id,
            run_id=run_id,
            command_id=command_id,
            command_type=command_type.value,
            content=content,
        )
        acceptable_digests = self.control_keyring.request_digests(
            tenant_id=tenant_id,
            run_id=run_id,
            command_id=command_id,
            command_type=command_type.value,
            content=content,
        )
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            run = self._row(connection, tenant_id, run_id, lock=True)
            existing = (
                connection.execute(
                    select(AGENT_RUN_COMMANDS).where(
                        and_(
                            AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                            AGENT_RUN_COMMANDS.c.run_id == run_id,
                            AGENT_RUN_COMMANDS.c.command_id == command_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["request_digest"] not in acceptable_digests:
                    raise IdempotencyConflict(
                        "agent control command ID was reused with different content"
                    )
                return self._control_record(existing)
            if expected_run_version <= 0 or int(run["version"]) != expected_run_version:
                raise AgentRunPersistenceError("agent run version conflict")
            if run["status"] in {
                DurableRunStatus.FAILED.value,
                DurableRunStatus.CANCELLED.value,
            }:
                raise AgentRunPersistenceError("terminal failed run cannot accept control")
            if (
                run["status"] == DurableRunStatus.COMPLETED.value
                and command_type is AgentControlType.STEER
            ):
                raise AgentRunPersistenceError("completed run only accepts follow-up control")
            pending_count = connection.execute(
                select(func.count())
                .select_from(AGENT_RUN_COMMANDS)
                .where(
                    and_(
                        AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                        AGENT_RUN_COMMANDS.c.run_id == run_id,
                        AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value,
                    )
                )
            ).scalar_one()
            if int(pending_count) >= 1000:
                raise AgentRunPersistenceError("agent control pending limit exceeded")
            sequence = (
                int(
                    connection.execute(
                        select(func.coalesce(func.max(AGENT_RUN_COMMANDS.c.sequence), 0)).where(
                            and_(
                                AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                                AGENT_RUN_COMMANDS.c.run_id == run_id,
                            )
                        )
                    ).scalar_one()
                )
                + 1
            )
            if sequence > 100_000:
                raise AgentRunPersistenceError("agent control history limit exceeded")
            encrypted = self.control_keyring.encrypt(
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=sequence,
                command_id=command_id,
                command_type=command_type.value,
                content=content,
            )
            connection.execute(
                insert(AGENT_RUN_COMMANDS).values(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    sequence=sequence,
                    command_id=command_id,
                    command_type=command_type.value,
                    status=AgentControlStatus.PENDING.value,
                    request_digest=digest,
                    content_ciphertext=encrypted.ciphertext,
                    content_nonce=encrypted.nonce,
                    content_fingerprint=encrypted.fingerprint,
                    content_key_id=encrypted.key_id,
                    submitted_by=actor_id,
                    created_at=now,
                    applied_at=None,
                    applied_run_version=None,
                    rejected_at=None,
                    rejection_code=None,
                )
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=actor_id,
                event_type="agent_run.control_submitted",
                data={
                    "status": "pending",
                    "sequence": sequence,
                    "command_type": command_type.value,
                },
            )
            if run["status"] == DurableRunStatus.COMPLETED.value:
                self._reencrypt_status(
                    connection,
                    run,
                    target=DurableRunStatus.QUEUED,
                    now=now,
                    clear_lease=True,
                    extra_values={
                        "completed_at": None,
                        "next_attempt_at": None,
                    },
                )
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    actor_id=actor_id,
                    event_type="agent_run.follow_up_requeued",
                    data={
                        "status": "queued",
                        "version": int(run["version"]) + 1,
                        "sequence": sequence,
                    },
                )
            row = (
                connection.execute(
                    select(AGENT_RUN_COMMANDS).where(
                        and_(
                            AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                            AGENT_RUN_COMMANDS.c.run_id == run_id,
                            AGENT_RUN_COMMANDS.c.sequence == sequence,
                        )
                    )
                )
                .mappings()
                .one()
            )
            return self._control_record(row)

    def list_control(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
        pending_only: bool = False,
    ) -> tuple[AgentControlCommand, ...]:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise AgentRunPersistenceError("agent control cursor or limit is invalid")
        with self._transaction(tenant_id) as connection:
            self._row(connection, tenant_id, run_id, lock=False)
            filters = [
                AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                AGENT_RUN_COMMANDS.c.run_id == run_id,
                AGENT_RUN_COMMANDS.c.sequence > after_sequence,
            ]
            if pending_only:
                filters.append(AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value)
            rows = (
                connection.execute(
                    select(AGENT_RUN_COMMANDS)
                    .where(and_(*filters))
                    .order_by(AGENT_RUN_COMMANDS.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return tuple(self._control_record(row) for row in rows)

    def claim_next(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> AgentRunLease | None:
        self._identifier("worker_id", worker_id)
        if not 5 <= lease_seconds <= 3600:
            raise AgentRunPersistenceError("lease duration is invalid")
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            statement = (
                select(AGENT_RUNS)
                .where(
                    and_(
                        AGENT_RUNS.c.tenant_id == tenant_id,
                        AGENT_RUNS.c.status == DurableRunStatus.QUEUED.value,
                        or_(
                            AGENT_RUNS.c.next_attempt_at.is_(None),
                            AGENT_RUNS.c.next_attempt_at <= now,
                        ),
                    )
                )
                .order_by(AGENT_RUNS.c.updated_at)
                .limit(1)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            checkpoint = self._decrypt(row)
            version = int(row["version"]) + 1
            encrypted = self.keyring.encrypt(
                tenant_id=tenant_id,
                run_id=row["run_id"],
                version=version,
                checkpoint=checkpoint,
            )
            token = secrets.token_urlsafe(32)
            expiry = now + timedelta(seconds=lease_seconds)
            connection.execute(
                update(AGENT_RUNS)
                .where(
                    and_(
                        AGENT_RUNS.c.tenant_id == tenant_id,
                        AGENT_RUNS.c.run_id == row["run_id"],
                    )
                )
                .values(
                    status=DurableRunStatus.LEASED.value,
                    version=version,
                    checkpoint_ciphertext=encrypted.ciphertext,
                    checkpoint_nonce=encrypted.nonce,
                    checkpoint_fingerprint=encrypted.fingerprint,
                    checkpoint_key_id=encrypted.key_id,
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_expires_at=expiry,
                    next_attempt_at=None,
                    updated_at=now,
                )
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=row["run_id"],
                actor_id=worker_id,
                event_type="agent_run.leased",
                data={
                    "status": "leased",
                    "version": version,
                    "failure_count": int(row["failure_count"]),
                },
            )
            run = self._load(connection, tenant_id, row["run_id"])
            return AgentRunLease(run, worker_id, token, expiry, checkpoint)

    def start(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
    ) -> DurableAgentRun:
        return self._lease_checkpoint_transition(
            tenant_id=tenant_id,
            run_id=run_id,
            worker_id=worker_id,
            lease_token=lease_token,
            target=DurableRunStatus.RUNNING,
            checkpoint=None,
            turns=None,
            tool_calls=None,
            total_tokens=None,
            model_cost_microusd=None,
        )

    def heartbeat(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> datetime:
        if not 5 <= lease_seconds <= 3600:
            raise AgentRunPersistenceError("lease duration is invalid")
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=lease_seconds)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            if (
                row["status"] not in {DurableRunStatus.LEASED.value, DurableRunStatus.RUNNING.value}
                or row["lease_owner"] != worker_id
                or not secrets.compare_digest(row["lease_token"] or "", lease_token)
                or row["lease_expires_at"] is None
                or self._aware(row["lease_expires_at"]) <= now
            ):
                raise AgentRunPersistenceError("agent run lease is invalid or expired")
            connection.execute(
                update(AGENT_RUNS)
                .where(
                    and_(
                        AGENT_RUNS.c.tenant_id == tenant_id,
                        AGENT_RUNS.c.run_id == run_id,
                    )
                )
                .values(lease_expires_at=expiry, updated_at=now)
            )
        return expiry

    def checkpoint(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        target: DurableRunStatus,
        checkpoint: dict[str, Any],
        turns: int,
        tool_calls: int,
        total_tokens: int,
        model_cost_microusd: int = 0,
        pending_call_id: str | None = None,
        pending_approval_id: str | None = None,
        failure_code: str | None = None,
        applied_control_sequences: tuple[int, ...] = (),
    ) -> DurableAgentRun:
        if target not in {
            DurableRunStatus.RUNNING,
            DurableRunStatus.AWAITING_APPROVAL,
            DurableRunStatus.AWAITING_TOOL,
            DurableRunStatus.COMPLETED,
            DurableRunStatus.FAILED,
        }:
            raise AgentRunPersistenceError("checkpoint target status is invalid")
        if target is DurableRunStatus.FAILED:
            self._error_code(failure_code)
        elif failure_code is not None:
            raise AgentRunPersistenceError("failure code is only valid for failed runs")
        return self._lease_checkpoint_transition(
            tenant_id=tenant_id,
            run_id=run_id,
            worker_id=worker_id,
            lease_token=lease_token,
            target=target,
            checkpoint=checkpoint,
            turns=turns,
            tool_calls=tool_calls,
            total_tokens=total_tokens,
            model_cost_microusd=model_cost_microusd,
            pending_call_id=pending_call_id,
            pending_approval_id=pending_approval_id,
            failure_code=failure_code,
            applied_control_sequences=applied_control_sequences,
        )

    def retry(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        error_code: str,
        delay_seconds: int,
    ) -> DurableAgentRun:
        self._error_code(error_code)
        if not 0 <= delay_seconds <= 3600:
            raise AgentRunPersistenceError("agent retry delay is invalid")
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            self._validate_lease(row, worker_id, lease_token, now)
            failure_count = int(row["failure_count"]) + 1
            terminal = failure_count >= int(row["max_failures"])
            target = DurableRunStatus.FAILED if terminal else DurableRunStatus.QUEUED
            self._reencrypt_status(
                connection,
                row,
                target=target,
                now=now,
                clear_lease=True,
                extra_values={
                    "failure_count": failure_count,
                    "next_attempt_at": (
                        None if terminal else now + timedelta(seconds=delay_seconds)
                    ),
                    "last_error_code": error_code,
                    "completed_at": now if terminal else None,
                },
            )
            if terminal:
                self._reject_pending_controls(
                    connection,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    now=now,
                    rejection_code="run_failed",
                )
            version = int(row["version"]) + 1
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=worker_id,
                event_type=(
                    "agent_run.retry_exhausted" if terminal else "agent_run.retry_scheduled"
                ),
                data={
                    "status": target.value,
                    "version": version,
                    "failure_count": failure_count,
                    "error_code": error_code,
                    "retry_delay_seconds": 0 if terminal else delay_seconds,
                },
            )
            return self._load(connection, tenant_id, run_id)

    def abort(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        error_code: str,
    ) -> DurableAgentRun:
        self._error_code(error_code)
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            self._validate_lease(row, worker_id, lease_token, now)
            self._reencrypt_status(
                connection,
                row,
                target=DurableRunStatus.FAILED,
                now=now,
                clear_lease=True,
                extra_values={
                    "next_attempt_at": None,
                    "last_error_code": error_code,
                    "completed_at": now,
                },
            )
            self._reject_pending_controls(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                now=now,
                rejection_code="run_failed",
            )
            version = int(row["version"]) + 1
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=worker_id,
                event_type="agent_run.failed",
                data={
                    "status": "failed",
                    "version": version,
                    "error_code": error_code,
                },
            )
            return self._load(connection, tenant_id, run_id)

    def cancel_queued(self, *, tenant_id: str, run_id: str, owner_principal_id: str) -> DurableAgentRun:
        """Owner-bound cancellation before execution; never revoke an active lease.

        Internal Harness lifecycle operation. Leased/running work must first
        finish or expire so its actual usage is not discarded by cancellation.
        """
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            if row["owner_principal_id"] != owner_principal_id:
                raise AgentRunPersistenceError("queued cancellation owner mismatch")
            if row["status"] != DurableRunStatus.QUEUED.value:
                return self._record(row)
            self._reencrypt_status(connection, row, target=DurableRunStatus.CANCELLED,
                                   now=now, clear_lease=True,
                                   extra_values={"next_attempt_at": None, "completed_at": now,
                                                 "last_error_code": "submission_changed"})
            self._reject_pending_controls(connection, tenant_id=tenant_id, run_id=run_id,
                                          now=now, rejection_code="run_cancelled")
            self._event(connection, tenant_id=tenant_id, run_id=run_id,
                        actor_id=owner_principal_id, event_type="agent_run.cancelled",
                        data={"status": "cancelled", "version": int(row["version"]) + 1,
                              "error_code": "submission_changed"})
            return self._load(connection, tenant_id, run_id)

    def get(self, *, tenant_id: str, run_id: str) -> DurableAgentRun:
        with self._transaction(tenant_id) as connection:
            return self._load(connection, tenant_id, run_id)

    def list_runs(self, *, tenant_id: str, owner_principal_id: str | None,
                  limit: int = 100) -> tuple[DurableAgentRun, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("agent run list limit is invalid")
        with self._transaction(tenant_id) as connection:
            statement = select(AGENT_RUNS).where(AGENT_RUNS.c.tenant_id == tenant_id)
            if owner_principal_id is not None:
                statement = statement.where(AGENT_RUNS.c.owner_principal_id == owner_principal_id)
            rows = connection.execute(
                statement.order_by(AGENT_RUNS.c.updated_at.desc(), AGENT_RUNS.c.run_id).limit(limit)
            ).mappings().all()
            return tuple(self._record(row) for row in rows)

    def requeue_after_approval(
        self,
        *,
        tenant_id: str,
        run_id: str,
        actor_id: str,
        approval_id: str,
        expected_version: int,
        checkpoint: dict[str, Any],
    ) -> DurableAgentRun:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            if (
                row["status"] != DurableRunStatus.AWAITING_APPROVAL.value
                or row["pending_approval_id"] != approval_id
            ):
                raise AgentRunPersistenceError("run is not waiting for this approval")
            if int(row["version"]) != expected_version:
                raise AgentRunPersistenceError("agent run version conflict")
            version = expected_version + 1
            encrypted = self.keyring.encrypt(
                tenant_id=tenant_id,
                run_id=run_id,
                version=version,
                checkpoint=checkpoint,
            )
            connection.execute(
                update(AGENT_RUNS)
                .where(
                    and_(
                        AGENT_RUNS.c.tenant_id == tenant_id,
                        AGENT_RUNS.c.run_id == run_id,
                        AGENT_RUNS.c.version == expected_version,
                    )
                )
                .values(
                    status=DurableRunStatus.QUEUED.value,
                    version=version,
                    pending_call_id=None,
                    pending_approval_id=None,
                    checkpoint_ciphertext=encrypted.ciphertext,
                    checkpoint_nonce=encrypted.nonce,
                    checkpoint_fingerprint=encrypted.fingerprint,
                    checkpoint_key_id=encrypted.key_id,
                    updated_at=now,
                )
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=actor_id,
                event_type="agent_run.approval_resumed",
                data={"status": "queued", "version": version},
            )
            return self._load(connection, tenant_id, run_id)

    def requeue_after_tools(
        self,
        *,
        tenant_id: str,
        run_id: str,
        actor_id: str,
        expected_version: int,
        checkpoint: dict[str, Any],
    ) -> DurableAgentRun:
        """Atomically make a tool-complete checkpoint claimable again."""
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            if row["status"] != DurableRunStatus.AWAITING_TOOL.value:
                raise AgentRunPersistenceError("run is not waiting for tools")
            if int(row["version"]) != expected_version:
                raise AgentRunPersistenceError("agent run version conflict")
            version = expected_version + 1
            encrypted = self.keyring.encrypt(
                tenant_id=tenant_id,
                run_id=run_id,
                version=version,
                checkpoint=checkpoint,
            )
            changed = connection.execute(
                update(AGENT_RUNS)
                .where(
                    and_(
                        AGENT_RUNS.c.tenant_id == tenant_id,
                        AGENT_RUNS.c.run_id == run_id,
                        AGENT_RUNS.c.version == expected_version,
                        AGENT_RUNS.c.status == DurableRunStatus.AWAITING_TOOL.value,
                    )
                )
                .values(
                    status=DurableRunStatus.QUEUED.value,
                    version=version,
                    checkpoint_ciphertext=encrypted.ciphertext,
                    checkpoint_nonce=encrypted.nonce,
                    checkpoint_fingerprint=encrypted.fingerprint,
                    checkpoint_key_id=encrypted.key_id,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise AgentRunPersistenceError("agent run tool wake-up conflict")
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=actor_id,
                event_type="agent_run.tools_completed",
                data={"status": "queued", "version": version},
            )
            return self._load(connection, tenant_id, run_id)

    def load_checkpoint(self, *, tenant_id: str, run_id: str) -> dict[str, Any]:
        with self._transaction(tenant_id) as connection:
            return self._decrypt(self._row(connection, tenant_id, run_id, lock=False))

    def list_events(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> tuple[DurableAgentEvent, ...]:
        if after_sequence < 0 or not 1 <= limit <= 1000:
            raise AgentRunPersistenceError("event cursor or limit is invalid")
        with self._transaction(tenant_id) as connection:
            self._row(connection, tenant_id, run_id, lock=False)
            rows = (
                connection.execute(
                    select(AGENT_RUN_EVENTS)
                    .where(
                        and_(
                            AGENT_RUN_EVENTS.c.tenant_id == tenant_id,
                            AGENT_RUN_EVENTS.c.run_id == run_id,
                            AGENT_RUN_EVENTS.c.sequence > after_sequence,
                        )
                    )
                    .order_by(AGENT_RUN_EVENTS.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return tuple(
                DurableAgentEvent(
                    run_id=row["run_id"],
                    sequence=int(row["sequence"]),
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    data=dict(row["data"]),
                    occurred_at=self._aware(row["occurred_at"]),
                )
                for row in rows
            )

    def recover_expired(self, *, tenant_id: str, actor_id: str = "scheduler") -> int:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            rows = (
                connection.execute(
                    select(AGENT_RUNS)
                    .where(
                        and_(
                            AGENT_RUNS.c.tenant_id == tenant_id,
                            AGENT_RUNS.c.status.in_(
                                [
                                    DurableRunStatus.LEASED.value,
                                    DurableRunStatus.RUNNING.value,
                                ]
                            ),
                            AGENT_RUNS.c.lease_expires_at < now,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for row in rows:
                failure_count = int(row["failure_count"]) + 1
                terminal = failure_count >= int(row["max_failures"])
                target = DurableRunStatus.FAILED if terminal else DurableRunStatus.QUEUED
                delay_seconds = min(300, 5 * (2 ** max(0, failure_count - 1)))
                self._reencrypt_status(
                    connection,
                    row,
                    target=target,
                    now=now,
                    clear_lease=True,
                    extra_values={
                        "failure_count": failure_count,
                        "next_attempt_at": (
                            None if terminal else now + timedelta(seconds=delay_seconds)
                        ),
                        "last_error_code": "lease_expired",
                        "completed_at": now if terminal else None,
                    },
                )
                if terminal:
                    self._reject_pending_controls(
                        connection,
                        tenant_id=tenant_id,
                        run_id=row["run_id"],
                        now=now,
                        rejection_code="run_failed",
                    )
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    run_id=row["run_id"],
                    actor_id=actor_id,
                    event_type=(
                        "agent_run.retry_exhausted" if terminal else "agent_run.lease_recovered"
                    ),
                    data={
                        "status": target.value,
                        "version": int(row["version"]) + 1,
                        "failure_count": failure_count,
                        "error_code": "lease_expired",
                        "retry_delay_seconds": 0 if terminal else delay_seconds,
                    },
                )
            return len(rows)

    def _lease_checkpoint_transition(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        target: DurableRunStatus,
        checkpoint: dict[str, Any] | None,
        turns: int | None,
        tool_calls: int | None,
        total_tokens: int | None,
        model_cost_microusd: int | None,
        pending_call_id: str | None = None,
        pending_approval_id: str | None = None,
        failure_code: str | None = None,
        applied_control_sequences: tuple[int, ...] = (),
    ) -> DurableAgentRun:
        now = datetime.now(UTC)
        with self._transaction(tenant_id) as connection:
            row = self._row(connection, tenant_id, run_id, lock=True)
            if (
                row["lease_owner"] != worker_id
                or not secrets.compare_digest(row["lease_token"] or "", lease_token)
                or row["lease_expires_at"] is None
                or self._aware(row["lease_expires_at"]) <= now
            ):
                raise AgentRunPersistenceError("agent run lease is invalid or expired")
            if target is DurableRunStatus.RUNNING and row["status"] not in {
                DurableRunStatus.LEASED.value,
                DurableRunStatus.RUNNING.value,
            }:
                raise AgentRunPersistenceError("agent run cannot enter running state")
            if target is DurableRunStatus.AWAITING_APPROVAL and (
                not pending_call_id or not pending_approval_id
            ):
                raise AgentRunPersistenceError("approval checkpoint identifiers are required")
            if (
                min(
                    turns if turns is not None else int(row["turns"]),
                    tool_calls if tool_calls is not None else int(row["tool_calls"]),
                    total_tokens if total_tokens is not None else int(row["total_tokens"]),
                    (
                        model_cost_microusd
                        if model_cost_microusd is not None
                        else int(row["model_cost_microusd"])
                    ),
                )
                < 0
            ):
                raise AgentRunPersistenceError("agent usage counters are invalid")
            previous_checkpoint = self._decrypt(row)
            effective_checkpoint = checkpoint if checkpoint is not None else previous_checkpoint
            if len(set(applied_control_sequences)) != len(applied_control_sequences) or any(
                sequence <= 0 for sequence in applied_control_sequences
            ):
                raise AgentRunPersistenceError("applied control sequences are invalid")
            if applied_control_sequences:
                control_rows = (
                    connection.execute(
                        select(AGENT_RUN_COMMANDS)
                        .where(
                            and_(
                                AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                                AGENT_RUN_COMMANDS.c.run_id == run_id,
                                AGENT_RUN_COMMANDS.c.sequence.in_(list(applied_control_sequences)),
                                AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .all()
                )
                if len(control_rows) != len(applied_control_sequences):
                    raise AgentRunPersistenceError(
                        "applied control commands are absent or no longer pending"
                    )
            control_cursor = effective_checkpoint.get("control_cursor", 0)
            if (
                isinstance(control_cursor, bool)
                or not isinstance(control_cursor, int)
                or control_cursor < 0
            ):
                raise AgentRunPersistenceError("checkpoint control cursor is invalid")
            previous_control_cursor = previous_checkpoint.get("control_cursor", 0)
            if (
                isinstance(previous_control_cursor, bool)
                or not isinstance(previous_control_cursor, int)
                or previous_control_cursor < 0
                or control_cursor < previous_control_cursor
            ):
                raise AgentRunPersistenceError("checkpoint control cursor regressed")
            if tuple(sorted(applied_control_sequences)) != applied_control_sequences or any(
                sequence <= previous_control_cursor or sequence > control_cursor
                for sequence in applied_control_sequences
            ):
                raise AgentRunPersistenceError("applied control cursor binding is invalid")
            pending_through_cursor = (
                connection.execute(
                    select(AGENT_RUN_COMMANDS)
                    .where(
                        and_(
                            AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                            AGENT_RUN_COMMANDS.c.run_id == run_id,
                            AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value,
                            AGENT_RUN_COMMANDS.c.sequence <= control_cursor,
                        )
                    )
                    .order_by(AGENT_RUN_COMMANDS.c.sequence)
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            if tuple(int(item["sequence"]) for item in pending_through_cursor) != (
                applied_control_sequences
            ):
                raise AgentRunPersistenceError(
                    "checkpoint skipped or falsely applied a pending control command"
                )
            self._validate_control_checkpoint(
                checkpoint=effective_checkpoint,
                rows=pending_through_cursor,
            )
            requested_target = target
            if target is DurableRunStatus.COMPLETED:
                pending_after_cursor = connection.execute(
                    select(func.count())
                    .select_from(AGENT_RUN_COMMANDS)
                    .where(
                        and_(
                            AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                            AGENT_RUN_COMMANDS.c.run_id == run_id,
                            AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value,
                            AGENT_RUN_COMMANDS.c.sequence > control_cursor,
                        )
                    )
                ).scalar_one()
                if int(pending_after_cursor) > 0:
                    target = DurableRunStatus.QUEUED
            version = int(row["version"]) + 1
            encrypted = self.keyring.encrypt(
                tenant_id=tenant_id,
                run_id=run_id,
                version=version,
                checkpoint=effective_checkpoint,
            )
            terminal = target in TERMINAL_RUN_STATES
            clear_lease = target is not DurableRunStatus.RUNNING
            connection.execute(
                update(AGENT_RUNS)
                .where(
                    and_(
                        AGENT_RUNS.c.tenant_id == tenant_id,
                        AGENT_RUNS.c.run_id == run_id,
                    )
                )
                .values(
                    status=target.value,
                    version=version,
                    turns=turns if turns is not None else row["turns"],
                    tool_calls=tool_calls if tool_calls is not None else row["tool_calls"],
                    total_tokens=(
                        total_tokens if total_tokens is not None else row["total_tokens"]
                    ),
                    model_cost_microusd=(
                        model_cost_microusd
                        if model_cost_microusd is not None
                        else row["model_cost_microusd"]
                    ),
                    pending_call_id=(
                        pending_call_id if target is DurableRunStatus.AWAITING_APPROVAL else None
                    ),
                    pending_approval_id=(
                        pending_approval_id
                        if target is DurableRunStatus.AWAITING_APPROVAL
                        else None
                    ),
                    checkpoint_ciphertext=encrypted.ciphertext,
                    checkpoint_nonce=encrypted.nonce,
                    checkpoint_fingerprint=encrypted.fingerprint,
                    checkpoint_key_id=encrypted.key_id,
                    lease_owner=None if clear_lease else row["lease_owner"],
                    lease_token=None if clear_lease else row["lease_token"],
                    lease_expires_at=None if clear_lease else row["lease_expires_at"],
                    updated_at=now,
                    completed_at=now if terminal else None,
                    next_attempt_at=None,
                    last_error_code=(
                        failure_code
                        if target is DurableRunStatus.FAILED
                        else row["last_error_code"]
                    ),
                )
            )
            if applied_control_sequences:
                connection.execute(
                    update(AGENT_RUN_COMMANDS)
                    .where(
                        and_(
                            AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                            AGENT_RUN_COMMANDS.c.run_id == run_id,
                            AGENT_RUN_COMMANDS.c.sequence.in_(list(applied_control_sequences)),
                            AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value,
                        )
                    )
                    .values(
                        status=AgentControlStatus.APPLIED.value,
                        applied_at=now,
                        applied_run_version=version,
                    )
                )
                for control_row in control_rows:
                    self._event(
                        connection,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        actor_id=worker_id,
                        event_type="agent_run.control_applied",
                        data={
                            "status": "applied",
                            "sequence": int(control_row["sequence"]),
                            "command_type": control_row["command_type"],
                            "run_version": version,
                        },
                    )
            if target is DurableRunStatus.FAILED:
                self._reject_pending_controls(
                    connection,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    now=now,
                    rejection_code="run_failed",
                )
            self._event(
                connection,
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id=worker_id,
                event_type=(
                    "agent_run.control_requeued"
                    if requested_target is DurableRunStatus.COMPLETED
                    and target is DurableRunStatus.QUEUED
                    else f"agent_run.{target.value}"
                ),
                data={
                    "status": target.value,
                    "version": version,
                    "applied_control_count": len(applied_control_sequences),
                },
            )
            return self._load(connection, tenant_id, run_id)

    def _reencrypt_status(
        self,
        connection: Connection,
        row,
        *,
        target: DurableRunStatus,
        now: datetime,
        clear_lease: bool,
        extra_values: dict[str, Any] | None = None,
    ) -> None:
        checkpoint = self._decrypt(row)
        version = int(row["version"]) + 1
        encrypted = self.keyring.encrypt(
            tenant_id=row["tenant_id"],
            run_id=row["run_id"],
            version=version,
            checkpoint=checkpoint,
        )
        values = {
            "status": target.value,
            "version": version,
            "checkpoint_ciphertext": encrypted.ciphertext,
            "checkpoint_nonce": encrypted.nonce,
            "checkpoint_fingerprint": encrypted.fingerprint,
            "checkpoint_key_id": encrypted.key_id,
            "lease_owner": None if clear_lease else row["lease_owner"],
            "lease_token": None if clear_lease else row["lease_token"],
            "lease_expires_at": None if clear_lease else row["lease_expires_at"],
            "updated_at": now,
        }
        if extra_values:
            values.update(extra_values)
        connection.execute(
            update(AGENT_RUNS)
            .where(
                and_(
                    AGENT_RUNS.c.tenant_id == row["tenant_id"],
                    AGENT_RUNS.c.run_id == row["run_id"],
                )
            )
            .values(**values)
        )

    def _decrypt(self, row) -> dict[str, Any]:
        return self.keyring.decrypt(
            tenant_id=row["tenant_id"],
            run_id=row["run_id"],
            version=int(row["version"]),
            ciphertext=bytes(row["checkpoint_ciphertext"]),
            nonce=bytes(row["checkpoint_nonce"]),
            fingerprint=row["checkpoint_fingerprint"],
            key_id=row["checkpoint_key_id"],
        )

    def _event(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        run_id: str,
        actor_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        canonical = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(canonical.encode("utf-8")) > 32_768:
            raise AgentRunPersistenceError("agent run event exceeds its size limit")
        if self._redactor.redact(canonical).findings:
            raise AgentRunPersistenceError("agent run lifecycle event contains a secret")
        sequence = (
            int(
                connection.execute(
                    select(func.coalesce(func.max(AGENT_RUN_EVENTS.c.sequence), 0)).where(
                        and_(
                            AGENT_RUN_EVENTS.c.tenant_id == tenant_id,
                            AGENT_RUN_EVENTS.c.run_id == run_id,
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        connection.execute(
            insert(AGENT_RUN_EVENTS).values(
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=sequence,
                event_id=event_id,
                event_type=event_type,
                data=data,
                occurred_at=now,
            )
        )
        if self.audit_log is not None:
            self.audit_log.append_in_transaction(
                connection,
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type=event_type,
                    actor_id=actor_id,
                    outcome=str(data.get("status", "recorded")),
                    details={"run_id": run_id, "sequence": sequence},
                    correlation_id=run_id,
                    event_id=event_id,
                    occurred_at=now.isoformat(),
                ),
            )

    def _load(self, connection: Connection, tenant_id: str, run_id: str) -> DurableAgentRun:
        return self._record(self._row(connection, tenant_id, run_id, lock=False))

    def _control_record(self, row) -> AgentControlCommand:
        if self.control_keyring is None:
            raise AgentRunPersistenceError("agent control encryption is not configured")
        command_type = AgentControlType(row["command_type"])
        return AgentControlCommand(
            run_id=row["run_id"],
            sequence=int(row["sequence"]),
            command_id=row["command_id"],
            command_type=command_type,
            status=AgentControlStatus(row["status"]),
            content=self.control_keyring.decrypt(
                tenant_id=row["tenant_id"],
                run_id=row["run_id"],
                sequence=int(row["sequence"]),
                command_id=row["command_id"],
                command_type=command_type.value,
                ciphertext=bytes(row["content_ciphertext"]),
                nonce=bytes(row["content_nonce"]),
                fingerprint=row["content_fingerprint"],
                key_id=row["content_key_id"],
            ),
            submitted_by=row["submitted_by"],
            created_at=self._aware(row["created_at"]),
            applied_at=(self._aware(row["applied_at"]) if row["applied_at"] else None),
            applied_run_version=row["applied_run_version"],
            rejected_at=(self._aware(row["rejected_at"]) if row["rejected_at"] else None),
            rejection_code=row["rejection_code"],
        )

    def _validate_control_checkpoint(self, *, checkpoint: dict[str, Any], rows) -> None:
        expected = {int(row["sequence"]): row for row in rows}
        if not expected:
            return
        found: dict[int, dict[str, Any]] = {}
        messages = checkpoint.get("messages")
        if not isinstance(messages, list):
            raise AgentRunPersistenceError("checkpoint messages are invalid")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                value = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict) or value.get("schema") != "coifesp.agent-control.v1":
                continue
            sequence = value.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence in found:
                raise AgentRunPersistenceError("checkpoint control messages are ambiguous")
            found[sequence] = value
        for sequence, row in expected.items():
            value = found.get(sequence)
            command_type = AgentControlType(row["command_type"])
            command = self._control_record(row)
            if value != {
                "schema": "coifesp.agent-control.v1",
                "instruction_trust": "user_instruction",
                "sequence": sequence,
                "command_id": row["command_id"],
                "command_type": command_type.value,
                "content": command.content,
            }:
                raise AgentRunPersistenceError(
                    "checkpoint does not contain the applied control command"
                )

    @staticmethod
    def _reject_pending_controls(
        connection: Connection,
        *,
        tenant_id: str,
        run_id: str,
        now: datetime,
        rejection_code: str,
    ) -> None:
        connection.execute(
            update(AGENT_RUN_COMMANDS)
            .where(
                and_(
                    AGENT_RUN_COMMANDS.c.tenant_id == tenant_id,
                    AGENT_RUN_COMMANDS.c.run_id == run_id,
                    AGENT_RUN_COMMANDS.c.status == AgentControlStatus.PENDING.value,
                )
            )
            .values(
                status=AgentControlStatus.REJECTED.value,
                rejected_at=now,
                rejection_code=rejection_code,
            )
        )

    @staticmethod
    def _record(row) -> DurableAgentRun:
        return DurableAgentRun(
            run_id=row["run_id"],
            tenant_id=row["tenant_id"],
            owner_principal_id=row["owner_principal_id"],
            correlation_id=row["correlation_id"],
            status=DurableRunStatus(row["status"]),
            version=int(row["version"]),
            turns=int(row["turns"]),
            tool_calls=int(row["tool_calls"]),
            total_tokens=int(row["total_tokens"]),
            model_cost_microusd=int(row["model_cost_microusd"]),
            pending_call_id=row["pending_call_id"],
            pending_approval_id=row["pending_approval_id"],
            failure_count=int(row["failure_count"]),
            max_failures=int(row["max_failures"]),
            next_attempt_at=(
                SQLAlchemyAgentRunRepository._aware(row["next_attempt_at"])
                if row["next_attempt_at"]
                else None
            ),
            last_error_code=row["last_error_code"],
            created_at=SQLAlchemyAgentRunRepository._aware(row["created_at"]),
            updated_at=SQLAlchemyAgentRunRepository._aware(row["updated_at"]),
            completed_at=(
                SQLAlchemyAgentRunRepository._aware(row["completed_at"])
                if row["completed_at"]
                else None
            ),
        )

    @staticmethod
    def _row(connection: Connection, tenant_id: str, run_id: str, *, lock: bool):
        statement = select(AGENT_RUNS).where(
            and_(AGENT_RUNS.c.tenant_id == tenant_id, AGENT_RUNS.c.run_id == run_id)
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("agent run is absent or hidden")
        return row

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[Connection]:
        self._identifier("tenant_id", tenant_id)
        if self._bound_connection is not None:
            self._set_tenant(self._bound_connection, tenant_id)
            yield self._bound_connection
            return
        with self.engine.begin() as connection:
            self._set_tenant(connection, tenant_id)
            yield connection

    @staticmethod
    def _set_tenant(connection: Connection, tenant_id: str) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
        elif connection.dialect.name != "sqlite":
            raise AgentRunPersistenceError("agent runs support PostgreSQL and SQLite only")

    @staticmethod
    def _identifier(name: str, value: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise AgentRunPersistenceError(f"{name} is invalid")

    @staticmethod
    def _error_code(value: str | None) -> None:
        if value is None or not _ERROR_CODE.fullmatch(value):
            raise AgentRunPersistenceError("agent run failure code is invalid")

    @staticmethod
    def _validate_lease(row, worker_id: str, lease_token: str, now: datetime) -> None:
        if (
            row["status"] not in {DurableRunStatus.LEASED.value, DurableRunStatus.RUNNING.value}
            or row["lease_owner"] != worker_id
            or not secrets.compare_digest(row["lease_token"] or "", lease_token)
            or row["lease_expires_at"] is None
            or SQLAlchemyAgentRunRepository._aware(row["lease_expires_at"]) <= now
        ):
            raise AgentRunPersistenceError("agent run lease is invalid or expired")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
