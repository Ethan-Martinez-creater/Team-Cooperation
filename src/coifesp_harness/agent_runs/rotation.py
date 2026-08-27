from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import and_, select, text, update
from sqlalchemy.engine import Engine

from ..audit import AuditEvent
from ..postgres_audit import SQLAlchemyAuditLog
from .crypto import AgentCheckpointKeyring
from .repository import AGENT_RUNS, AgentRunPersistenceError


@dataclass(frozen=True, slots=True)
class AgentCheckpointRotationBatch:
    tenant_id: str
    source_key_id: str
    target_key_id: str
    rotated: int
    last_run_id: str | None
    complete: bool


class AgentCheckpointKeyRotationService:
    """Re-encrypt a bounded checkpoint batch without changing logical Run versions."""

    def __init__(
        self,
        *,
        engine: Engine,
        keyring: AgentCheckpointKeyring,
        audit: SQLAlchemyAuditLog,
    ) -> None:
        if audit.engine is not engine:
            raise ValueError("checkpoint rotation and audit must share one database engine")
        self.engine = engine
        self.keyring = keyring
        self.audit = audit

    def rotate_batch(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        source_key_id: str,
        after_run_id: str | None = None,
        limit: int = 100,
    ) -> AgentCheckpointRotationBatch:
        if not tenant_id or not actor_id or not source_key_id:
            raise ValueError("tenant, actor, and source key are required")
        if source_key_id == self.keyring.key_id:
            raise ValueError("source key must differ from the active target key")
        if source_key_id not in self.keyring.available_key_ids:
            raise ValueError("source checkpoint key is unavailable")
        if not 1 <= limit <= 500:
            raise ValueError("checkpoint rotation batch limit must be between 1 and 500")

        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            elif connection.dialect.name != "sqlite":
                raise AgentRunPersistenceError(
                    "checkpoint rotation supports PostgreSQL and SQLite only"
                )
            criteria = [
                AGENT_RUNS.c.tenant_id == tenant_id,
                AGENT_RUNS.c.checkpoint_key_id == source_key_id,
            ]
            if after_run_id is not None:
                criteria.append(AGENT_RUNS.c.run_id > after_run_id)
            statement = (
                select(AGENT_RUNS).where(and_(*criteria)).order_by(AGENT_RUNS.c.run_id).limit(limit)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = connection.execute(statement).mappings().all()

            rotated_ids: list[str] = []
            for row in rows:
                version = int(row["version"])
                checkpoint = self.keyring.decrypt(
                    tenant_id=row["tenant_id"],
                    run_id=row["run_id"],
                    version=version,
                    ciphertext=bytes(row["checkpoint_ciphertext"]),
                    nonce=bytes(row["checkpoint_nonce"]),
                    fingerprint=row["checkpoint_fingerprint"],
                    key_id=row["checkpoint_key_id"],
                )
                encrypted = self.keyring.encrypt(
                    tenant_id=tenant_id,
                    run_id=row["run_id"],
                    version=version,
                    checkpoint=checkpoint,
                )
                result = connection.execute(
                    update(AGENT_RUNS)
                    .where(
                        and_(
                            AGENT_RUNS.c.tenant_id == tenant_id,
                            AGENT_RUNS.c.run_id == row["run_id"],
                            AGENT_RUNS.c.version == version,
                            AGENT_RUNS.c.checkpoint_key_id == source_key_id,
                            AGENT_RUNS.c.checkpoint_fingerprint == row["checkpoint_fingerprint"],
                        )
                    )
                    .values(
                        checkpoint_ciphertext=encrypted.ciphertext,
                        checkpoint_nonce=encrypted.nonce,
                        checkpoint_fingerprint=encrypted.fingerprint,
                        checkpoint_key_id=encrypted.key_id,
                    )
                )
                if result.rowcount != 1:
                    raise AgentRunPersistenceError("agent checkpoint changed during key rotation")
                rotated_ids.append(row["run_id"])

            last_id = rotated_ids[-1] if rotated_ids else after_run_id
            self.audit.append_in_transaction(
                connection,
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="agent_checkpoint.key_rotation",
                    actor_id=actor_id,
                    outcome="complete" if len(rows) < limit else "batch_rotated",
                    details={
                        "source_key_id": source_key_id,
                        "target_key_id": self.keyring.key_id,
                        "rotated": len(rotated_ids),
                        "run_ids_digest": hashlib.sha256(
                            "\n".join(rotated_ids).encode("utf-8")
                        ).hexdigest(),
                    },
                    correlation_id=f"checkpoint-rotation:{tenant_id}:{source_key_id}",
                ),
            )
        return AgentCheckpointRotationBatch(
            tenant_id=tenant_id,
            source_key_id=source_key_id,
            target_key_id=self.keyring.key_id,
            rotated=len(rotated_ids),
            last_run_id=last_id,
            complete=len(rows) < limit,
        )
