from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import and_, exists, select, text
from sqlalchemy.engine import Engine

from ..audit import AuditEvent
from ..errors import MemoryError
from ..postgres_audit import SQLAlchemyAuditLog
from .crypto import TenantMemoryKeyring
from .repository import MEMORY_RECORDS, MEMORY_SEARCH_TERMS, SQLAlchemyMemoryRepository
from .search import tokenize


@dataclass(frozen=True, slots=True)
class MemoryIndexBatch:
    indexed: int
    last_memory_id: str | None
    complete: bool


class MemorySearchIndexService:
    def __init__(self, *, engine: Engine, keyring: TenantMemoryKeyring,
                 audit: SQLAlchemyAuditLog) -> None:
        if audit.engine is not engine:
            raise ValueError("memory indexing and audit must share one engine")
        self.engine, self.keyring, self.audit = engine, keyring, audit

    def backfill_batch(self, *, tenant_id: str, actor_id: str,
                       after_memory_id: str | None = None, limit: int = 100) -> MemoryIndexBatch:
        if not tenant_id or not actor_id or not 1 <= limit <= 500:
            raise ValueError("memory indexing batch is invalid")
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant": tenant_id})
            elif connection.dialect.name != "sqlite":
                raise MemoryError("memory indexing supports PostgreSQL and SQLite only")
            criteria = [MEMORY_RECORDS.c.tenant_id == tenant_id,
                ~exists(select(MEMORY_SEARCH_TERMS.c.memory_id).where(and_(
                    MEMORY_SEARCH_TERMS.c.tenant_id == MEMORY_RECORDS.c.tenant_id,
                    MEMORY_SEARCH_TERMS.c.memory_id == MEMORY_RECORDS.c.memory_id,
                    MEMORY_SEARCH_TERMS.c.key_id == self.keyring.key_id)))]
            if after_memory_id:
                criteria.append(MEMORY_RECORDS.c.memory_id > after_memory_id)
            statement = select(MEMORY_RECORDS).where(and_(*criteria)).order_by(
                MEMORY_RECORDS.c.memory_id).limit(limit)
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = connection.execute(statement).mappings().all()
            ids = []
            for row in rows:
                record = SQLAlchemyMemoryRepository._row_to_record(row)
                content = self.keyring.decrypt(record)
                SQLAlchemyMemoryRepository._insert_search_terms(connection, record,
                    self.keyring.search_tokens(tenant_id, tokenize(content)))
                ids.append(record.memory_id)
            self.audit.append_in_transaction(connection, AuditEvent(
                tenant_id=tenant_id, event_type="memory.search_index_backfill",
                actor_id=actor_id, outcome="complete" if len(rows) < limit else "batch_indexed",
                details={"indexed": len(ids), "record_ids_digest": hashlib.sha256(
                    "\n".join(ids).encode()).hexdigest(), "key_id": self.keyring.key_id},
                correlation_id=f"memory-index:{tenant_id}:{self.keyring.key_id}"))
        return MemoryIndexBatch(len(ids), ids[-1] if ids else after_memory_id, len(rows) < limit)
