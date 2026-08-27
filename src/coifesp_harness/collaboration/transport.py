from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator

from sqlalchemy import and_, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..errors import IntegrityError, PolicyDenied
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification, Principal
from ..security.redaction import SecretRedactor
from .gateway import SignedEnvelopeCodec
from .models import CollaborationEnvelope
from .repository import (
    COLLABORATION_INBOX,
    GOVERNANCE_EVENTS,
    GOVERNANCE_OUTBOX,
    GOVERNANCE_PROGRAMS,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class OutboxLease:
    message_id: str
    lease_token: str
    lease_expires_at: datetime
    program_id: str
    event_sequence: int
    producer_tenant_id: str
    recipient_tenant_id: str
    actor_id: str
    event_type: str
    subject_id: str
    payload: dict
    classification: Classification
    compartments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InboxLease:
    message_id: str
    lease_token: str
    lease_expires_at: datetime
    sender_tenant_id: str
    envelope: CollaborationEnvelope


@dataclass(frozen=True, slots=True)
class InboxReceipt:
    message_id: str
    duplicate: bool


class DurableCollaborationTransport:
    """Leased outbox/inbox transport with signed envelopes and replay-safe receipt."""

    def __init__(
        self,
        *,
        engine: Engine,
        audit_log: SQLAlchemyAuditLog,
        codec: SignedEnvelopeCodec,
        redactor: SecretRedactor | None = None,
        _bound_connection: Connection | None = None,
    ) -> None:
        if audit_log.engine is not engine:
            raise ValueError("collaboration transport and audit must share one engine")
        self.engine = engine
        self.audit_log = audit_log
        self.codec = codec
        self.redactor = redactor or SecretRedactor()
        self._bound_connection = _bound_connection

    def using_connection(self, connection: Connection) -> "DurableCollaborationTransport":
        """Bind operations to a caller-owned transaction for smoke tests/composition."""
        if connection.engine is not self.engine:
            raise ValueError("bound connection belongs to a different engine")
        return DurableCollaborationTransport(
            engine=self.engine,
            audit_log=self.audit_log,
            codec=self.codec,
            redactor=self.redactor,
            _bound_connection=connection,
        )

    def claim_outbound(
        self,
        *,
        relay: Principal,
        lease_seconds: int = 60,
    ) -> OutboxLease | None:
        self._require_service(relay, "collaboration_relay")
        self._validate_lease_seconds(lease_seconds)
        now = datetime.now(UTC)
        with self._transaction(relay.tenant_id) as connection:
            self._recover_outbox(
                connection,
                relay.tenant_id,
                relay.principal_id,
                now,
            )
            statement = (
                select(
                    GOVERNANCE_OUTBOX,
                    GOVERNANCE_EVENTS.c.actor_id,
                    GOVERNANCE_EVENTS.c.event_type,
                    GOVERNANCE_EVENTS.c.subject_id,
                    GOVERNANCE_EVENTS.c.payload,
                    GOVERNANCE_PROGRAMS.c.classification,
                    GOVERNANCE_PROGRAMS.c.compartments,
                )
                .select_from(
                    GOVERNANCE_OUTBOX.join(
                        GOVERNANCE_EVENTS,
                        and_(
                            GOVERNANCE_OUTBOX.c.program_id == GOVERNANCE_EVENTS.c.program_id,
                            GOVERNANCE_OUTBOX.c.event_sequence == GOVERNANCE_EVENTS.c.sequence,
                        ),
                    ).join(
                        GOVERNANCE_PROGRAMS,
                        GOVERNANCE_OUTBOX.c.program_id == GOVERNANCE_PROGRAMS.c.program_id,
                    )
                )
                .where(
                    and_(
                        GOVERNANCE_OUTBOX.c.producer_tenant_id == relay.tenant_id,
                        GOVERNANCE_OUTBOX.c.status == "pending",
                        GOVERNANCE_OUTBOX.c.available_at <= now,
                        GOVERNANCE_OUTBOX.c.attempt_count < GOVERNANCE_OUTBOX.c.max_attempts,
                    )
                )
                .order_by(
                    GOVERNANCE_OUTBOX.c.available_at,
                    GOVERNANCE_OUTBOX.c.created_at,
                    GOVERNANCE_OUTBOX.c.message_id,
                )
                .limit(1)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(
                    skip_locked=True,
                    of=GOVERNANCE_OUTBOX,
                )
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            lease_token = secrets.token_urlsafe(32)
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            connection.execute(
                update(GOVERNANCE_OUTBOX)
                .where(GOVERNANCE_OUTBOX.c.message_id == row["message_id"])
                .values(
                    status="claimed",
                    attempt_count=int(row["attempt_count"]) + 1,
                    lease_owner=relay.principal_id,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    last_error_code=None,
                )
            )
            self._audit(
                connection,
                tenant_id=relay.tenant_id,
                actor_id=relay.principal_id,
                message_id=row["message_id"],
                event_type="collaboration.outbox_claimed",
                outcome="claimed",
                details={"recipient_tenant_id": row["recipient_tenant_id"]},
            )
            return OutboxLease(
                message_id=row["message_id"],
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                program_id=row["program_id"],
                event_sequence=int(row["event_sequence"]),
                producer_tenant_id=row["producer_tenant_id"],
                recipient_tenant_id=row["recipient_tenant_id"],
                actor_id=row["actor_id"],
                event_type=row["event_type"],
                subject_id=row["subject_id"],
                payload=dict(row["payload"]),
                classification=Classification(int(row["classification"])),
                compartments=tuple(sorted(row["compartments"])),
            )

    def heartbeat_outbound(
        self,
        *,
        relay: Principal,
        message_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> datetime:
        self._require_service(relay, "collaboration_relay")
        self._validate_lease_seconds(lease_seconds)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(relay.tenant_id) as connection:
            self._require_outbox_lease(
                connection,
                relay=relay,
                message_id=message_id,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                update(GOVERNANCE_OUTBOX)
                .where(GOVERNANCE_OUTBOX.c.message_id == message_id)
                .values(lease_expires_at=expires_at)
            )
        return expires_at

    def build_envelope(self, lease: OutboxLease) -> CollaborationEnvelope:
        raw_content = json.dumps(
            {
                "schema": "coifesp.governance-event.v1",
                "program_id": lease.program_id,
                "event_sequence": lease.event_sequence,
                "event_type": lease.event_type,
                "subject_id": lease.subject_id,
                "payload": lease.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        redacted = self.redactor.redact(raw_content)
        return self.codec.issue(
            message_id=lease.message_id,
            idempotency_key=lease.message_id,
            correlation_id=lease.program_id,
            sender_tenant_id=lease.producer_tenant_id,
            sender_principal_id=lease.actor_id,
            recipient_tenant_id=lease.recipient_tenant_id,
            purpose="governance-event",
            classification=lease.classification.name,
            compartments=lease.compartments,
            content=redacted.text,
            redaction_findings=redacted.findings,
        )

    def mark_published(
        self,
        *,
        relay: Principal,
        message_id: str,
        lease_token: str,
        envelope: CollaborationEnvelope,
    ) -> None:
        self._require_service(relay, "collaboration_relay")
        self.codec.verify(envelope)
        now = datetime.now(UTC)
        envelope_values = asdict(envelope)
        envelope_digest = self._digest(envelope_values)
        with self._transaction(relay.tenant_id) as connection:
            row = self._require_outbox_lease(
                connection,
                relay=relay,
                message_id=message_id,
                lease_token=lease_token,
                now=now,
            )
            if (
                envelope.message_id != message_id
                or envelope.sender_tenant_id != relay.tenant_id
                or envelope.recipient_tenant_id != row["recipient_tenant_id"]
            ):
                raise IntegrityError("published envelope does not match its outbox lease")
            connection.execute(
                update(GOVERNANCE_OUTBOX)
                .where(GOVERNANCE_OUTBOX.c.message_id == message_id)
                .values(
                    status="published",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    envelope=envelope_values,
                    envelope_digest=envelope_digest,
                    published_at=now,
                )
            )
            self._audit(
                connection,
                tenant_id=relay.tenant_id,
                actor_id=relay.principal_id,
                message_id=message_id,
                event_type="collaboration.outbox_published",
                outcome="published",
                details={
                    "recipient_tenant_id": envelope.recipient_tenant_id,
                    "envelope_digest": envelope_digest,
                },
            )

    def fail_outbound(
        self,
        *,
        relay: Principal,
        message_id: str,
        lease_token: str,
        error_code: str,
        retry_delay_seconds: int = 30,
    ) -> str:
        self._require_service(relay, "collaboration_relay")
        self._validate_error(error_code, retry_delay_seconds)
        now = datetime.now(UTC)
        with self._transaction(relay.tenant_id) as connection:
            row = self._require_outbox_lease(
                connection,
                relay=relay,
                message_id=message_id,
                lease_token=lease_token,
                now=now,
            )
            target = (
                "pending" if int(row["attempt_count"]) < int(row["max_attempts"]) else "dead_letter"
            )
            connection.execute(
                update(GOVERNANCE_OUTBOX)
                .where(GOVERNANCE_OUTBOX.c.message_id == message_id)
                .values(
                    status=target,
                    available_at=now + timedelta(seconds=retry_delay_seconds),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code=error_code,
                )
            )
            self._audit(
                connection,
                tenant_id=relay.tenant_id,
                actor_id=relay.principal_id,
                message_id=message_id,
                event_type=f"collaboration.outbox_{target}",
                outcome=target,
                details={"error_code": error_code},
            )
            return target

    def receive(
        self,
        *,
        consumer: Principal,
        envelope: CollaborationEnvelope,
        max_attempts: int = 10,
    ) -> InboxReceipt:
        self._require_service(consumer, "collaboration_consumer")
        if not 1 <= max_attempts <= 100:
            raise ValueError("inbox attempt limit is invalid")
        self.codec.verify(
            envelope,
            expected_recipient_tenant_id=consumer.tenant_id,
        )
        values = asdict(envelope)
        digest = self._digest(values)
        now = datetime.now(UTC)
        with self._transaction(consumer.tenant_id) as connection:
            insert_values = {
                "recipient_tenant_id": consumer.tenant_id,
                "message_id": envelope.message_id,
                "sender_tenant_id": envelope.sender_tenant_id,
                "envelope": values,
                "envelope_digest": digest,
                "status": "received",
                "attempt_count": 0,
                "max_attempts": max_attempts,
                "available_at": now,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "handler_key": None,
                "result_digest": None,
                "last_error_code": None,
                "received_at": now,
                "completed_at": None,
            }
            if connection.dialect.name == "postgresql":
                statement = (
                    postgresql_insert(COLLABORATION_INBOX)
                    .values(**insert_values)
                    .on_conflict_do_nothing(index_elements=["recipient_tenant_id", "message_id"])
                    .returning(COLLABORATION_INBOX.c.message_id)
                )
            else:
                statement = (
                    sqlite_insert(COLLABORATION_INBOX)
                    .values(**insert_values)
                    .on_conflict_do_nothing(index_elements=["recipient_tenant_id", "message_id"])
                    .returning(COLLABORATION_INBOX.c.message_id)
                )
            inserted = connection.execute(statement).scalar_one_or_none()
            if inserted is None:
                existing = (
                    connection.execute(
                        select(COLLABORATION_INBOX).where(
                            and_(
                                COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                                COLLABORATION_INBOX.c.message_id == envelope.message_id,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if not secrets.compare_digest(existing["envelope_digest"], digest):
                    raise IntegrityError("inbox message id was reused with a different envelope")
                return InboxReceipt(envelope.message_id, True)
            self._audit(
                connection,
                tenant_id=consumer.tenant_id,
                actor_id=consumer.principal_id,
                message_id=envelope.message_id,
                event_type="collaboration.inbox_received",
                outcome="received",
                details={
                    "sender_tenant_id": envelope.sender_tenant_id,
                    "envelope_digest": digest,
                },
            )
            return InboxReceipt(envelope.message_id, False)

    def claim_inbound(
        self,
        *,
        consumer: Principal,
        handler_key: str,
        lease_seconds: int = 60,
    ) -> InboxLease | None:
        self._require_service(consumer, "collaboration_consumer")
        self._validate_identifier("handler_key", handler_key)
        self._validate_lease_seconds(lease_seconds)
        now = datetime.now(UTC)
        with self._transaction(consumer.tenant_id) as connection:
            self._recover_inbox(
                connection,
                consumer.tenant_id,
                consumer.principal_id,
                now,
            )
            statement = (
                select(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                        COLLABORATION_INBOX.c.status == "received",
                        COLLABORATION_INBOX.c.available_at <= now,
                        COLLABORATION_INBOX.c.attempt_count < COLLABORATION_INBOX.c.max_attempts,
                    )
                )
                .order_by(
                    COLLABORATION_INBOX.c.available_at,
                    COLLABORATION_INBOX.c.received_at,
                )
                .limit(1)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            lease_token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(seconds=lease_seconds)
            connection.execute(
                update(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                        COLLABORATION_INBOX.c.message_id == row["message_id"],
                    )
                )
                .values(
                    status="claimed",
                    attempt_count=int(row["attempt_count"]) + 1,
                    lease_owner=consumer.principal_id,
                    lease_token=lease_token,
                    lease_expires_at=expires_at,
                    handler_key=handler_key,
                    last_error_code=None,
                )
            )
            return InboxLease(
                message_id=row["message_id"],
                lease_token=lease_token,
                lease_expires_at=expires_at,
                sender_tenant_id=row["sender_tenant_id"],
                envelope=CollaborationEnvelope(**row["envelope"]),
            )

    def heartbeat_inbound(
        self,
        *,
        consumer: Principal,
        message_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> datetime:
        self._require_service(consumer, "collaboration_consumer")
        self._validate_lease_seconds(lease_seconds)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction(consumer.tenant_id) as connection:
            self._require_inbox_lease(
                connection,
                consumer=consumer,
                message_id=message_id,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                update(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                        COLLABORATION_INBOX.c.message_id == message_id,
                    )
                )
                .values(lease_expires_at=expires_at)
            )
        return expires_at

    def complete_inbound(
        self,
        *,
        consumer: Principal,
        message_id: str,
        lease_token: str,
        result_digest: str,
    ) -> None:
        self._require_service(consumer, "collaboration_consumer")
        if not re.fullmatch(r"[0-9a-f]{64}", result_digest):
            raise ValueError("inbox result digest is invalid")
        now = datetime.now(UTC)
        with self._transaction(consumer.tenant_id) as connection:
            self._require_inbox_lease(
                connection,
                consumer=consumer,
                message_id=message_id,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                update(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                        COLLABORATION_INBOX.c.message_id == message_id,
                    )
                )
                .values(
                    status="processed",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    result_digest=result_digest,
                    completed_at=now,
                )
            )
            self._audit(
                connection,
                tenant_id=consumer.tenant_id,
                actor_id=consumer.principal_id,
                message_id=message_id,
                event_type="collaboration.inbox_processed",
                outcome="processed",
                details={"result_digest": result_digest},
            )

    def fail_inbound(
        self,
        *,
        consumer: Principal,
        message_id: str,
        lease_token: str,
        error_code: str,
        retry_delay_seconds: int = 30,
    ) -> str:
        self._require_service(consumer, "collaboration_consumer")
        self._validate_error(error_code, retry_delay_seconds)
        now = datetime.now(UTC)
        with self._transaction(consumer.tenant_id) as connection:
            row = self._require_inbox_lease(
                connection,
                consumer=consumer,
                message_id=message_id,
                lease_token=lease_token,
                now=now,
            )
            target = (
                "received" if int(row["attempt_count"]) < int(row["max_attempts"]) else "rejected"
            )
            connection.execute(
                update(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                        COLLABORATION_INBOX.c.message_id == message_id,
                    )
                )
                .values(
                    status=target,
                    available_at=now + timedelta(seconds=retry_delay_seconds),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code=error_code,
                    completed_at=now if target == "rejected" else None,
                )
            )
            return target

    def _recover_outbox(
        self,
        connection: Connection,
        tenant_id: str,
        actor_id: str,
        now: datetime,
    ) -> None:
        rows = (
            connection.execute(
                select(GOVERNANCE_OUTBOX)
                .where(
                    and_(
                        GOVERNANCE_OUTBOX.c.producer_tenant_id == tenant_id,
                        GOVERNANCE_OUTBOX.c.status == "claimed",
                        GOVERNANCE_OUTBOX.c.lease_expires_at < now,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        for row in rows:
            target = (
                "pending" if int(row["attempt_count"]) < int(row["max_attempts"]) else "dead_letter"
            )
            connection.execute(
                update(GOVERNANCE_OUTBOX)
                .where(GOVERNANCE_OUTBOX.c.message_id == row["message_id"])
                .values(
                    status=target,
                    available_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="lease_expired",
                )
            )
            self._audit(
                connection,
                tenant_id=tenant_id,
                actor_id=actor_id,
                message_id=row["message_id"],
                event_type="collaboration.outbox_lease_recovered",
                outcome=target,
                details={},
            )

    def _recover_inbox(
        self,
        connection: Connection,
        tenant_id: str,
        actor_id: str,
        now: datetime,
    ) -> None:
        rows = (
            connection.execute(
                select(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == tenant_id,
                        COLLABORATION_INBOX.c.status == "claimed",
                        COLLABORATION_INBOX.c.lease_expires_at < now,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        for row in rows:
            target = (
                "received" if int(row["attempt_count"]) < int(row["max_attempts"]) else "rejected"
            )
            connection.execute(
                update(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == tenant_id,
                        COLLABORATION_INBOX.c.message_id == row["message_id"],
                    )
                )
                .values(
                    status=target,
                    available_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="lease_expired",
                    completed_at=now if target == "rejected" else None,
                )
            )
            self._audit(
                connection,
                tenant_id=tenant_id,
                actor_id=actor_id,
                message_id=row["message_id"],
                event_type="collaboration.inbox_lease_recovered",
                outcome=target,
                details={},
            )

    def _require_outbox_lease(
        self,
        connection: Connection,
        *,
        relay: Principal,
        message_id: str,
        lease_token: str,
        now: datetime,
    ):
        row = (
            connection.execute(
                select(GOVERNANCE_OUTBOX)
                .where(
                    and_(
                        GOVERNANCE_OUTBOX.c.message_id == message_id,
                        GOVERNANCE_OUTBOX.c.producer_tenant_id == relay.tenant_id,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["status"] != "claimed"
            or row["lease_owner"] != relay.principal_id
            or not secrets.compare_digest(row["lease_token"] or "", lease_token)
            or self._aware(row["lease_expires_at"]) <= now
        ):
            raise IntegrityError("outbox lease is invalid or expired")
        return row

    def _require_inbox_lease(
        self,
        connection: Connection,
        *,
        consumer: Principal,
        message_id: str,
        lease_token: str,
        now: datetime,
    ):
        row = (
            connection.execute(
                select(COLLABORATION_INBOX)
                .where(
                    and_(
                        COLLABORATION_INBOX.c.recipient_tenant_id == consumer.tenant_id,
                        COLLABORATION_INBOX.c.message_id == message_id,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["status"] != "claimed"
            or row["lease_owner"] != consumer.principal_id
            or not secrets.compare_digest(row["lease_token"] or "", lease_token)
            or self._aware(row["lease_expires_at"]) <= now
        ):
            raise IntegrityError("inbox lease is invalid or expired")
        return row

    def _audit(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        actor_id: str,
        message_id: str,
        event_type: str,
        outcome: str,
        details: dict,
    ) -> None:
        self.audit_log.append_in_transaction(
            connection,
            AuditEvent(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                outcome=outcome,
                details={"message_id": message_id, **details},
                correlation_id=message_id,
                event_id=str(uuid.uuid4()),
            ),
        )

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[Connection]:
        self._validate_identifier("tenant_id", tenant_id)
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
            raise RuntimeError("collaboration transport supports PostgreSQL and SQLite")

    @staticmethod
    def _require_service(principal: Principal, role: str) -> None:
        if not principal.is_service or role not in principal.roles:
            raise PolicyDenied(f"collaboration transport requires service role: {role}")

    @staticmethod
    def _validate_identifier(name: str, value: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{name} is invalid")

    @classmethod
    def _validate_error(cls, error_code: str, delay: int) -> None:
        cls._validate_identifier("error_code", error_code)
        if not 0 <= delay <= 86_400:
            raise ValueError("retry delay is invalid")

    @staticmethod
    def _validate_lease_seconds(value: int) -> None:
        if not 5 <= value <= 3600:
            raise ValueError("lease duration must be between 5 and 3600 seconds")

    @staticmethod
    def _digest(value: dict) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _aware(value: datetime | None) -> datetime:
        if value is None:
            raise IntegrityError("lease expiry is missing")
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
