from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import IntegrityError


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class AuditEvent:
    tenant_id: str
    event_type: str
    actor_id: str
    outcome: str
    details: dict[str, Any]
    correlation_id: str
    event_id: str = ""
    occurred_at: str = ""

    def normalized(self) -> "AuditEvent":
        return AuditEvent(
            tenant_id=self.tenant_id,
            event_type=self.event_type,
            actor_id=self.actor_id,
            outcome=self.outcome,
            details=self.details,
            correlation_id=self.correlation_id,
            event_id=self.event_id or str(uuid.uuid4()),
            occurred_at=self.occurred_at or datetime.now(UTC).isoformat(),
        )


class AuditSink(Protocol):
    def append(self, event: AuditEvent) -> str: ...


class SQLiteAuditLog:
    """Append-only per-tenant hash chain with HMAC signatures."""

    def __init__(self, database_path: Path, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("audit signing key must contain at least 32 bytes")
        self.database_path = database_path
        self.signing_key = signing_key
        self._lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    signature TEXT NOT NULL
                )
                """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_audit_tenant_sequence "
                "ON audit_events(tenant_id, sequence)"
            )

    def append(self, event: AuditEvent) -> str:
        normalized = event.normalized()
        payload = _canonical_json(asdict(normalized))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_hash FROM audit_events WHERE tenant_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (normalized.tenant_id,),
            ).fetchone()
            previous_hash = row["event_hash"] if row else "0" * 64
            event_hash = hashlib.sha256(
                (previous_hash + "\n" + payload).encode("utf-8")
            ).hexdigest()
            signature = hmac.new(
                self.signing_key, event_hash.encode("ascii"), hashlib.sha256
            ).hexdigest()
            connection.execute(
                "INSERT INTO audit_events "
                "(event_id, tenant_id, payload, previous_hash, event_hash, signature) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    normalized.event_id,
                    normalized.tenant_id,
                    payload,
                    previous_hash,
                    event_hash,
                    signature,
                ),
            )
            connection.commit()
        return normalized.event_id

    def verify_tenant_chain(self, tenant_id: str) -> int:
        previous_hash = "0" * 64
        count = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload, previous_hash, event_hash, signature "
                "FROM audit_events WHERE tenant_id = ? ORDER BY sequence",
                (tenant_id,),
            ).fetchall()
        for row in rows:
            if row["previous_hash"] != previous_hash:
                raise IntegrityError("audit previous-hash link is invalid")
            expected_hash = hashlib.sha256(
                (previous_hash + "\n" + row["payload"]).encode("utf-8")
            ).hexdigest()
            expected_signature = hmac.new(
                self.signing_key, expected_hash.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(row["event_hash"], expected_hash):
                raise IntegrityError("audit event hash is invalid")
            if not hmac.compare_digest(row["signature"], expected_signature):
                raise IntegrityError("audit event signature is invalid")
            previous_hash = expected_hash
            count += 1
        return count


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> str:
        normalized = event.normalized()
        self.events.append(normalized)
        return normalized.event_id
