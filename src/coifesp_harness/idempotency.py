from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class IdempotencyStore(Protocol):
    def claim(
        self,
        *,
        namespace: str,
        tenant_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ClaimStatus: ...


class SQLiteIdempotencyStore:
    """Durable atomic claims for at-most-once side-effect admission."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_claims (
                    namespace TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, tenant_id, idempotency_key)
                )
                """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def claim(
        self,
        *,
        namespace: str,
        tenant_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ClaimStatus:
        _validate_claim(namespace, tenant_id, idempotency_key, request_digest)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_digest FROM idempotency_claims "
                "WHERE namespace = ? AND tenant_id = ? AND idempotency_key = ?",
                (namespace, tenant_id, idempotency_key),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if row["request_digest"] == request_digest:
                    return ClaimStatus.DUPLICATE
                return ClaimStatus.CONFLICT
            connection.execute(
                "INSERT INTO idempotency_claims "
                "(namespace, tenant_id, idempotency_key, request_digest, claimed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    namespace,
                    tenant_id,
                    idempotency_key,
                    request_digest,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
            return ClaimStatus.CLAIMED


class InMemoryIdempotencyStore:
    """Deterministic test adapter; production composition uses a durable store."""

    def __init__(self) -> None:
        self._claims: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def claim(
        self,
        *,
        namespace: str,
        tenant_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ClaimStatus:
        _validate_claim(namespace, tenant_id, idempotency_key, request_digest)
        key = (namespace, tenant_id, idempotency_key)
        with self._lock:
            previous = self._claims.get(key)
            if previous is None:
                self._claims[key] = request_digest
                return ClaimStatus.CLAIMED
            if previous == request_digest:
                return ClaimStatus.DUPLICATE
            return ClaimStatus.CONFLICT


def _validate_claim(
    namespace: str, tenant_id: str, idempotency_key: str, request_digest: str
) -> None:
    if not namespace or not tenant_id or not idempotency_key or not request_digest:
        raise ValueError("all idempotency claim fields are required")
