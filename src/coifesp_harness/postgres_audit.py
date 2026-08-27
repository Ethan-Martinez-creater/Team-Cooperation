from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Iterator, Mapping

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

from .audit import AuditEvent, AuditSink
from .config import Settings
from .errors import IntegrityError
from .key_material import configured_keys

_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ZERO_HASH = "0" * 64
_MAX_PAYLOAD_BYTES = 65_536

AUDIT_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
)

AUDIT_HEADS = Table(
    "audit_heads",
    AUDIT_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("last_sequence", BigInteger, nullable=False),
    Column("last_hash", String(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("last_sequence >= 0", name="sequence"),
    CheckConstraint("length(last_hash) = 64", name="hash_length"),
)

AUDIT_EVENTS = Table(
    "audit_events",
    AUDIT_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("payload", Text, nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False),
    Column("signature", String(64), nullable=False),
    Column("key_id", String(128), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["audit_heads.tenant_id"],
        name="fk_audit_event_head",
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "tenant_id",
        "event_id",
        name="uq_audit_events_tenant_event",
    ),
    CheckConstraint("sequence > 0", name="positive_sequence"),
    CheckConstraint("length(previous_hash) = 64", name="previous_hash"),
    CheckConstraint("length(event_hash) = 64", name="event_hash"),
    CheckConstraint("length(signature) = 64", name="signature"),
)
Index(
    "ix_audit_tenant_occurred",
    AUDIT_EVENTS.c.tenant_id,
    AUDIT_EVENTS.c.occurred_at,
)


class AuditSigningKeyring:
    """Versioned HMAC keys without exposing key material through repr."""

    def __init__(
        self,
        *,
        active_key_id: str,
        verification_keys: Mapping[str, bytes],
    ) -> None:
        if not _KEY_ID.fullmatch(active_key_id):
            raise ValueError("active audit key id is invalid")
        copied = {key_id: bytes(value) for key_id, value in verification_keys.items()}
        if active_key_id not in copied:
            raise ValueError("active audit key is not available")
        if any(not _KEY_ID.fullmatch(key_id) or len(key) < 32 for key_id, key in copied.items()):
            raise ValueError("audit verification keys are invalid")
        self.active_key_id = active_key_id
        self._keys = copied

    @classmethod
    def from_settings(cls, settings: Settings) -> "AuditSigningKeyring":
        active, keys = configured_keys(
            active_key_id=settings.audit_key_id,
            versioned=settings.audit_keys,
            legacy=settings.audit_signing_key,
            legacy_name="COIFESP_AUDIT_SIGNING_KEY",
        )
        return cls(active_key_id=active, verification_keys=keys)

    def sign(self, event_hash: str) -> tuple[str, str]:
        key_id = self.active_key_id
        signature = hmac.new(
            self._keys[key_id],
            f"{key_id}:{event_hash}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return key_id, signature

    def verify(
        self,
        *,
        key_id: str,
        event_hash: str,
        signature: str,
    ) -> bool:
        key = self._keys.get(key_id)
        if key is None:
            return False
        expected = hmac.new(
            key,
            f"{key_id}:{event_hash}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def __repr__(self) -> str:
        return (
            "AuditSigningKeyring("
            f"active_key_id={self.active_key_id!r}, "
            "verification_keys=[REDACTED])"
        )


class SQLAlchemyAuditLog(AuditSink):
    """Per-tenant serialized audit chain backed by PostgreSQL or SQLite."""

    def __init__(
        self,
        *,
        engine: Engine,
        keyring: AuditSigningKeyring,
    ) -> None:
        self.engine = engine
        self.keyring = keyring
        self.metadata = AUDIT_METADATA
        self.heads = AUDIT_HEADS
        self.events = AUDIT_EVENTS

    def create_schema(self) -> None:
        """Development bootstrap. Production uses reviewed migrations."""
        self.metadata.create_all(
            self.engine,
            tables=[self.heads, self.events],
        )

    def append(self, event: AuditEvent) -> str:
        normalized = event.normalized()
        _validate_event(normalized)
        with self._tenant_transaction(normalized.tenant_id) as connection:
            return self.append_in_transaction(connection, normalized)

    def append_in_transaction(
        self,
        connection: Connection,
        event: AuditEvent,
    ) -> str:
        """Append using a caller-owned transaction.

        This is the production composition path when a business mutation and
        its audit evidence must commit or roll back together.
        """
        normalized = event.normalized()
        _validate_event(normalized)
        payload = _canonical_json(asdict(normalized))
        if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise IntegrityError("audit event payload exceeds the size limit")
        occurred_at = _parse_occurred_at(normalized.occurred_at)
        self._set_tenant_context(connection, normalized.tenant_id)

        try:
            self._ensure_head(connection, normalized.tenant_id)
            head = (
                connection.execute(
                    select(self.heads)
                    .where(self.heads.c.tenant_id == normalized.tenant_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            existing = (
                connection.execute(
                    select(
                        self.events.c.payload,
                        self.events.c.event_hash,
                    ).where(
                        and_(
                            self.events.c.tenant_id == normalized.tenant_id,
                            self.events.c.event_id == normalized.event_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["payload"] != payload:
                    raise IntegrityError("audit event id was reused with different content")
                return normalized.event_id

            sequence = int(head["last_sequence"]) + 1
            previous_hash = head["last_hash"]
            event_hash = hashlib.sha256(f"{previous_hash}\n{payload}".encode("utf-8")).hexdigest()
            key_id, signature = self.keyring.sign(event_hash)
            connection.execute(
                insert(self.events).values(
                    tenant_id=normalized.tenant_id,
                    sequence=sequence,
                    event_id=normalized.event_id,
                    event_type=normalized.event_type,
                    payload=payload,
                    previous_hash=previous_hash,
                    event_hash=event_hash,
                    signature=signature,
                    key_id=key_id,
                    occurred_at=occurred_at,
                )
            )
            connection.execute(
                update(self.heads)
                .where(self.heads.c.tenant_id == normalized.tenant_id)
                .values(
                    last_sequence=sequence,
                    last_hash=event_hash,
                    updated_at=datetime.now(UTC),
                )
            )
        except SQLAlchemyIntegrityError as exc:
            raise IntegrityError("audit database constraint failed") from exc
        return normalized.event_id

    def verify_tenant_chain(self, tenant_id: str) -> int:
        with self._tenant_transaction(tenant_id) as connection:
            return self.verify_tenant_chain_in_transaction(connection, tenant_id)

    def verify_tenant_chain_in_transaction(
        self,
        connection: Connection,
        tenant_id: str,
    ) -> int:
        """Verify a tenant chain without committing the caller's transaction."""
        self._set_tenant_context(connection, tenant_id)
        rows = (
            connection.execute(
                select(self.events)
                .where(self.events.c.tenant_id == tenant_id)
                .order_by(self.events.c.sequence)
            )
            .mappings()
            .all()
        )
        head = (
            connection.execute(select(self.heads).where(self.heads.c.tenant_id == tenant_id))
            .mappings()
            .one_or_none()
        )

        previous_hash = _ZERO_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            if row["sequence"] != expected_sequence:
                raise IntegrityError("audit sequence is not contiguous")
            if row["previous_hash"] != previous_hash:
                raise IntegrityError("audit previous-hash link is invalid")
            expected_hash = hashlib.sha256(
                f"{previous_hash}\n{row['payload']}".encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(row["event_hash"], expected_hash):
                raise IntegrityError("audit event hash is invalid")
            if not self.keyring.verify(
                key_id=row["key_id"],
                event_hash=row["event_hash"],
                signature=row["signature"],
            ):
                raise IntegrityError("audit event signature is invalid")
            previous_hash = expected_hash

        if head is None:
            if rows:
                raise IntegrityError("audit head is missing")
            return 0
        if head["last_sequence"] != len(rows) or head["last_hash"] != previous_hash:
            raise IntegrityError("audit head does not match the event chain")
        return len(rows)

    @contextmanager
    def _tenant_transaction(self, tenant_id: str) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            yield connection

    @staticmethod
    def _set_tenant_context(
        connection: Connection,
        tenant_id: str,
    ) -> None:
        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("tenant_id is invalid")
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
                {"tenant_id": tenant_id},
            )
        elif connection.dialect.name != "sqlite":
            raise IntegrityError("durable audit supports PostgreSQL and SQLite only")

    def _ensure_head(
        self,
        connection: Connection,
        tenant_id: str,
    ) -> None:
        values = {
            "tenant_id": tenant_id,
            "last_sequence": 0,
            "last_hash": _ZERO_HASH,
            "updated_at": datetime.now(UTC),
        }
        if connection.dialect.name == "postgresql":
            statement = (
                postgresql_insert(self.heads)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id"])
            )
        elif connection.dialect.name == "sqlite":
            statement = (
                sqlite_insert(self.heads)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id"])
            )
        else:
            raise IntegrityError("durable audit supports PostgreSQL and SQLite only")
        connection.execute(statement)


def _canonical_json(value) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityError("audit event is not JSON serializable") from exc


def _validate_event(event: AuditEvent) -> None:
    for name, value, maximum in (
        ("tenant_id", event.tenant_id, 128),
        ("actor_id", event.actor_id, 128),
        ("correlation_id", event.correlation_id, 128),
        ("event_id", event.event_id, 128),
    ):
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(ord(character) < 32 for character in value)
        ):
            raise IntegrityError(f"audit {name} is invalid")
    if not isinstance(event.event_type, str) or not _EVENT_TYPE.fullmatch(event.event_type):
        raise IntegrityError("audit event_type is invalid")
    if not isinstance(event.outcome, str) or not event.outcome:
        raise IntegrityError("audit outcome is invalid")
    if not isinstance(event.details, dict):
        raise IntegrityError("audit details must be an object")


def _parse_occurred_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IntegrityError("audit occurred_at is invalid") from exc
    if parsed.tzinfo is None:
        raise IntegrityError("audit occurred_at must be timezone-aware")
    return parsed.astimezone(UTC)
