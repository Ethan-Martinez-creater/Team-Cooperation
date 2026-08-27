from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import and_, delete, insert, select, text, update
from sqlalchemy.engine import Engine

from ..audit import AuditEvent
from ..errors import MemoryError
from ..postgres_audit import SQLAlchemyAuditLog
from .crypto import TenantMemoryKeyring
from .repository import MEMORY_RECORDS, MEMORY_SEARCH_TERMS, SQLAlchemyMemoryRepository
from .search import tokenize


@dataclass(frozen=True, slots=True)
class MemoryRotationBatch:
    tenant_id: str
    source_key_id: str
    target_key_id: str
    rotated: int
    last_memory_id: str | None
    complete: bool


class MemoryKeyRotationService:
    """Re-encrypts a bounded tenant batch and its audit evidence atomically."""

    def __init__(
        self,
        *,
        engine: Engine,
        keyring: TenantMemoryKeyring,
        audit: SQLAlchemyAuditLog,
    ) -> None:
        if audit.engine is not engine:
            raise ValueError("memory rotation and audit must share one database engine")
        self.engine = engine
        self.keyring = keyring
        self.audit = audit

    def rotate_batch(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        source_key_id: str,
        after_memory_id: str | None = None,
        limit: int = 100,
    ) -> MemoryRotationBatch:
        if not tenant_id or not actor_id or not source_key_id:
            raise ValueError("tenant, actor, and source key are required")
        if source_key_id == self.keyring.key_id:
            raise ValueError("source key must differ from the active target key")
        if source_key_id not in self.keyring.available_key_ids:
            raise ValueError("source memory key is unavailable")
        if not 1 <= limit <= 500:
            raise ValueError("memory rotation batch limit must be between 1 and 500")

        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            elif connection.dialect.name != "sqlite":
                raise MemoryError("memory rotation supports PostgreSQL and SQLite only")

            criteria = [
                MEMORY_RECORDS.c.tenant_id == tenant_id,
                MEMORY_RECORDS.c.key_id == source_key_id,
            ]
            if after_memory_id is not None:
                criteria.append(MEMORY_RECORDS.c.memory_id > after_memory_id)
            statement = (
                select(MEMORY_RECORDS)
                .where(and_(*criteria))
                .order_by(MEMORY_RECORDS.c.memory_id)
                .limit(limit)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = connection.execute(statement).mappings().all()

            rotated_ids: list[str] = []
            for row in rows:
                record = SQLAlchemyMemoryRepository._row_to_record(row)
                plaintext = self.keyring.decrypt(record)
                encrypted = self.keyring.encrypt(
                    memory_id=record.memory_id,
                    tenant_id=record.tenant_id,
                    scope=record.scope,
                    kind=record.kind,
                    label=record.label,
                    plaintext=plaintext,
                )
                result = connection.execute(
                    update(MEMORY_RECORDS)
                    .where(
                        and_(
                            MEMORY_RECORDS.c.tenant_id == tenant_id,
                            MEMORY_RECORDS.c.memory_id == record.memory_id,
                            MEMORY_RECORDS.c.key_id == source_key_id,
                            MEMORY_RECORDS.c.version == record.version,
                        )
                    )
                    .values(
                        ciphertext=encrypted.ciphertext,
                        nonce=encrypted.nonce,
                        content_fingerprint=encrypted.content_fingerprint,
                        key_id=encrypted.key_id,
                        version=record.version + 1,
                    )
                )
                if result.rowcount != 1:
                    raise MemoryError("memory changed during key rotation")
                connection.execute(delete(MEMORY_SEARCH_TERMS).where(and_(
                    MEMORY_SEARCH_TERMS.c.tenant_id == tenant_id,
                    MEMORY_SEARCH_TERMS.c.memory_id == record.memory_id,
                )))
                digests = self.keyring.search_tokens(tenant_id, tokenize(plaintext))
                values = [{"tenant_id": tenant_id, "memory_id": record.memory_id,
                    "key_id": key_id, "term_digest": digest}
                    for key_id, terms in digests.items() for digest in set(terms)]
                if values:
                    connection.execute(insert(MEMORY_SEARCH_TERMS), values)
                rotated_ids.append(record.memory_id)

            last_id = rotated_ids[-1] if rotated_ids else after_memory_id
            self.audit.append_in_transaction(
                connection,
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="memory.key_rotation",
                    actor_id=actor_id,
                    outcome="complete" if len(rows) < limit else "batch_rotated",
                    details={
                        "source_key_id": source_key_id,
                        "target_key_id": self.keyring.key_id,
                        "rotated": len(rotated_ids),
                        "record_ids_digest": hashlib.sha256(
                            "\n".join(rotated_ids).encode("utf-8")
                        ).hexdigest(),
                    },
                    correlation_id=f"memory-rotation:{tenant_id}:{source_key_id}",
                ),
            )
        return MemoryRotationBatch(
            tenant_id=tenant_id,
            source_key_id=source_key_id,
            target_key_id=self.keyring.key_id,
            rotated=len(rotated_ids),
            last_memory_id=last_id,
            complete=len(rows) < limit,
        )
