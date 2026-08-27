from __future__ import annotations
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator, Protocol

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    ForeignKeyConstraint,
    and_,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ..errors import MemoryConflictError, MemoryError
from ..idempotency import ClaimStatus
from ..security.models import Classification, ResourceLabel
from .models import (
    EncryptedMemoryRecord,
    MemoryKind,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    SourceType,
    TrustLevel,
)

MEMORY_METADATA = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "pk": "pk_%(table_name)s",
    }
)

MEMORY_RECORDS = Table(
    "memory_records",
    MEMORY_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("memory_id", String(64), primary_key=True),
    Column("scope", String(32), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("classification", Integer, nullable=False),
    Column("compartments", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    Column("resource_id", String(256), nullable=False),
    Column("source_type", String(32), nullable=False),
    Column("source_id", String(256), nullable=False),
    Column("source_uri", Text, nullable=True),
    Column("trust_level", Integer, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("owner_principal_id", String(128), nullable=True),
    Column("project_id", String(128), nullable=True),
    Column("session_id", String(128), nullable=True),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("content_fingerprint", String(64), nullable=False),
    Column("key_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    CheckConstraint(
        "scope IN ('session', 'user_private', 'team_project', 'organization')",
        name="scope",
    ),
    CheckConstraint(
        "kind IN ('fact', 'decision', 'procedure', 'task_summary')",
        name="kind",
    ),
    CheckConstraint(
        "status IN ('active', 'quarantined', 'revoked')",
        name="status",
    ),
    CheckConstraint("classification BETWEEN 0 AND 3", name="classification"),
    CheckConstraint(
        "source_type IN ('user', 'agent', 'tool', 'document', 'a2a', 'system')",
        name="source_type",
    ),
    CheckConstraint("trust_level BETWEEN 0 AND 3", name="trust_level"),
    CheckConstraint("length(nonce) = 12", name="nonce_length"),
    CheckConstraint("length(content_fingerprint) = 64", name="fingerprint_length"),
    CheckConstraint("version > 0", name="positive_version"),
    CheckConstraint(
        "(scope <> 'user_private' OR owner_principal_id IS NOT NULL) "
        "AND (scope <> 'session' OR session_id IS NOT NULL) "
        "AND (scope <> 'team_project' OR project_id IS NOT NULL)",
        name="scope_owner",
    ),
)
Index(
    "ix_memory_tenant_status_scope_created",
    MEMORY_RECORDS.c.tenant_id,
    MEMORY_RECORDS.c.status,
    MEMORY_RECORDS.c.scope,
    MEMORY_RECORDS.c.created_at,
)
Index(
    "ix_memory_tenant_project",
    MEMORY_RECORDS.c.tenant_id,
    MEMORY_RECORDS.c.project_id,
)
Index(
    "ix_memory_tenant_owner",
    MEMORY_RECORDS.c.tenant_id,
    MEMORY_RECORDS.c.owner_principal_id,
)
Index(
    "ix_memory_tenant_session",
    MEMORY_RECORDS.c.tenant_id,
    MEMORY_RECORDS.c.session_id,
)

MEMORY_IDEMPOTENCY_CLAIMS = Table(
    "memory_idempotency_claims",
    MEMORY_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("namespace", String(64), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("request_digest", String(64), nullable=False),
    Column("memory_id", String(64), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "memory_id"],
        ["memory_records.tenant_id", "memory_records.memory_id"],
        name="fk_memory_claim_record",
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    ),
    CheckConstraint("length(namespace) > 0", name="namespace"),
    CheckConstraint("length(idempotency_key) > 0", name="idempotency_key"),
    CheckConstraint("length(request_digest) = 64", name="request_digest"),
)

MEMORY_SEARCH_TERMS = Table(
    "memory_search_terms", MEMORY_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("memory_id", String(64), primary_key=True),
    Column("key_id", String(128), primary_key=True),
    Column("term_digest", String(64), primary_key=True),
    ForeignKeyConstraint(
        ["tenant_id", "memory_id"],
        ["memory_records.tenant_id", "memory_records.memory_id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("length(term_digest)=64", name="term_digest"),
)
Index("ix_memory_search_tenant_key_term", MEMORY_SEARCH_TERMS.c.tenant_id,
      MEMORY_SEARCH_TERMS.c.key_id, MEMORY_SEARCH_TERMS.c.term_digest)
Index(
    "ix_memory_claim_tenant_record",
    MEMORY_IDEMPOTENCY_CLAIMS.c.tenant_id,
    MEMORY_IDEMPOTENCY_CLAIMS.c.memory_id,
)


class MemoryRepository(Protocol):
    def add(self, record: EncryptedMemoryRecord) -> None: ...

    def add_idempotently(
        self,
        *,
        record: EncryptedMemoryRecord,
        namespace: str,
        idempotency_key: str,
        request_digest: str,
        acceptable_request_digests: frozenset[str] | None = None,
        search_terms: dict[str, tuple[str, ...]] | None = None,
    ) -> ClaimStatus: ...

    def get(self, tenant_id: str, memory_id: str) -> EncryptedMemoryRecord | None: ...

    def list_recallable(
        self,
        *,
        tenant_id: str,
        scope: MemoryScope | None = None,
        owner_principal_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[EncryptedMemoryRecord, ...]: ...

    def transition_status(
        self,
        *,
        tenant_id: str,
        memory_id: str,
        expected_version: int,
        expected_status: MemoryStatus,
        new_status: MemoryStatus,
    ) -> EncryptedMemoryRecord: ...

    def search_candidates(self, *, tenant_id: str,
        search_terms: dict[str, tuple[str, ...]], scope: MemoryScope,
        owner_principal_id: str | None = None, project_id: str | None = None,
        session_id: str | None = None, limit: int = 100,
        now: datetime | None = None) -> tuple[tuple[EncryptedMemoryRecord, int], ...]: ...

    def replace_search_terms(self, *, tenant_id: str, memory_id: str,
                             search_terms: dict[str, tuple[str, ...]]) -> None: ...


class SQLAlchemyMemoryRepository:
    """Encrypted memory repository with tenant predicates on every operation."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.metadata = MEMORY_METADATA
        self.records = MEMORY_RECORDS
        self.claims = MEMORY_IDEMPOTENCY_CLAIMS
        self.search_terms = MEMORY_SEARCH_TERMS

    def create_schema(self) -> None:
        """Development bootstrap. Production uses reviewed versioned migrations."""
        self.metadata.create_all(
            self.engine,
            tables=[self.records, self.claims, MEMORY_SEARCH_TERMS],
        )

    def add(self, record: EncryptedMemoryRecord) -> None:
        values = self._record_to_values(record)
        try:
            with self._tenant_transaction(record.tenant_id) as connection:
                connection.execute(insert(self.records).values(**values))
        except IntegrityError as exc:
            raise MemoryError("memory record already exists") from exc

    def add_idempotently(
        self,
        *,
        record: EncryptedMemoryRecord,
        namespace: str,
        idempotency_key: str,
        request_digest: str,
        acceptable_request_digests: frozenset[str] | None = None,
        search_terms: dict[str, tuple[str, ...]] | None = None,
    ) -> ClaimStatus:
        if not namespace or not idempotency_key:
            raise ValueError("memory idempotency namespace and key are required")
        acceptable = acceptable_request_digests or frozenset({request_digest})
        if request_digest not in acceptable or any(len(value) != 64 for value in acceptable):
            raise ValueError("memory request digest must contain 64 hexadecimal characters")
        claim_values = {
            "tenant_id": record.tenant_id,
            "namespace": namespace,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
            "memory_id": record.memory_id,
            "claimed_at": datetime.now(UTC),
        }
        try:
            with self._tenant_transaction(record.tenant_id) as connection:
                inserted = self._insert_claim_if_absent(connection, claim_values)
                if not inserted:
                    existing = (
                        connection.execute(
                            select(MEMORY_IDEMPOTENCY_CLAIMS).where(
                                and_(
                                    MEMORY_IDEMPOTENCY_CLAIMS.c.tenant_id == record.tenant_id,
                                    MEMORY_IDEMPOTENCY_CLAIMS.c.namespace == namespace,
                                    MEMORY_IDEMPOTENCY_CLAIMS.c.idempotency_key == idempotency_key,
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if existing is None:
                        raise MemoryError(
                            "idempotency claim disappeared during conflict resolution"
                        )
                    if (
                        existing["request_digest"] not in acceptable
                        or existing["memory_id"] != record.memory_id
                    ):
                        return ClaimStatus.CONFLICT
                    record_exists = connection.execute(
                        select(self.records.c.memory_id).where(
                            and_(
                                self.records.c.tenant_id == record.tenant_id,
                                self.records.c.memory_id == record.memory_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if record_exists is None:
                        raise MemoryError(
                            "atomic idempotency invariant is broken: "
                            "claim exists without its memory record"
                        )
                    return ClaimStatus.DUPLICATE

                connection.execute(insert(self.records).values(**self._record_to_values(record)))
                self._insert_search_terms(connection, record, search_terms or {})
                return ClaimStatus.CLAIMED
        except IntegrityError as exc:
            raise MemoryError("memory atomic write violated a database constraint") from exc

    @staticmethod
    def _insert_claim_if_absent(connection: Connection, values: dict) -> bool:
        key_columns = [
            "tenant_id",
            "namespace",
            "idempotency_key",
        ]
        if connection.dialect.name == "postgresql":
            statement = (
                postgresql_insert(MEMORY_IDEMPOTENCY_CLAIMS)
                .values(**values)
                .on_conflict_do_nothing(index_elements=key_columns)
                .returning(MEMORY_IDEMPOTENCY_CLAIMS.c.idempotency_key)
            )
        elif connection.dialect.name == "sqlite":
            statement = (
                sqlite_insert(MEMORY_IDEMPOTENCY_CLAIMS)
                .values(**values)
                .on_conflict_do_nothing(index_elements=key_columns)
                .returning(MEMORY_IDEMPOTENCY_CLAIMS.c.idempotency_key)
            )
        else:
            raise MemoryError("atomic Memory idempotency supports PostgreSQL and SQLite only")
        return connection.execute(statement).scalar_one_or_none() is not None

    def get(self, tenant_id: str, memory_id: str) -> EncryptedMemoryRecord | None:
        statement = select(self.records).where(
            and_(
                self.records.c.tenant_id == tenant_id,
                self.records.c.memory_id == memory_id,
            )
        )
        with self._tenant_transaction(tenant_id) as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return self._row_to_record(row) if row is not None else None

    def list_recallable(
        self,
        *,
        tenant_id: str,
        scope: MemoryScope | None = None,
        owner_principal_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[EncryptedMemoryRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("memory recall limit must be between 1 and 100")
        current = now or datetime.now(UTC)
        predicates = [
            self.records.c.tenant_id == tenant_id,
            self.records.c.status == MemoryStatus.ACTIVE.value,
            or_(
                self.records.c.expires_at.is_(None),
                self.records.c.expires_at > current,
            ),
        ]
        if scope is not None:
            predicates.append(self.records.c.scope == scope.value)
        if owner_principal_id is not None:
            predicates.append(self.records.c.owner_principal_id == owner_principal_id)
        if project_id is not None:
            predicates.append(self.records.c.project_id == project_id)
        if session_id is not None:
            predicates.append(self.records.c.session_id == session_id)
        statement = (
            select(self.records)
            .where(and_(*predicates))
            .order_by(self.records.c.created_at.desc())
            .limit(limit)
        )
        with self._tenant_transaction(tenant_id) as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(self._row_to_record(row) for row in rows)

    def transition_status(
        self,
        *,
        tenant_id: str,
        memory_id: str,
        expected_version: int,
        expected_status: MemoryStatus,
        new_status: MemoryStatus,
    ) -> EncryptedMemoryRecord:
        statement = (
            update(self.records)
            .where(
                and_(
                    self.records.c.tenant_id == tenant_id,
                    self.records.c.memory_id == memory_id,
                    self.records.c.version == expected_version,
                    self.records.c.status == expected_status.value,
                )
            )
            .values(status=new_status.value, version=expected_version + 1)
        )
        with self._tenant_transaction(tenant_id) as connection:
            result = connection.execute(statement)
            if result.rowcount != 1:
                raise MemoryConflictError("memory review conflict or invalid lifecycle state")
            row = (
                connection.execute(
                    select(self.records).where(
                        and_(
                            self.records.c.tenant_id == tenant_id,
                            self.records.c.memory_id == memory_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise MemoryError("memory disappeared after lifecycle transition")
            return self._row_to_record(row)

    def search_candidates(self, *, tenant_id: str,
        search_terms: dict[str, tuple[str, ...]], scope: MemoryScope,
        owner_principal_id: str | None = None, project_id: str | None = None,
        session_id: str | None = None, limit: int = 100,
        now: datetime | None = None) -> tuple[tuple[EncryptedMemoryRecord, int], ...]:
        if not search_terms or not 1 <= limit <= 500:
            raise ValueError("memory search candidate request is invalid")
        from sqlalchemy import func, tuple_
        pairs = [(key_id, digest) for key_id, digests in search_terms.items() for digest in digests]
        if not pairs or len(pairs) > 1024:
            raise ValueError("memory search token count is invalid")
        current = now or datetime.now(UTC)
        predicates = [self.records.c.tenant_id == tenant_id,
            self.records.c.status == MemoryStatus.ACTIVE.value,
            self.records.c.scope == scope.value,
            or_(self.records.c.expires_at.is_(None), self.records.c.expires_at > current),
            tuple_(MEMORY_SEARCH_TERMS.c.key_id, MEMORY_SEARCH_TERMS.c.term_digest).in_(pairs)]
        if owner_principal_id is not None:
            predicates.append(self.records.c.owner_principal_id == owner_principal_id)
        if project_id is not None:
            predicates.append(self.records.c.project_id == project_id)
        if session_id is not None:
            predicates.append(self.records.c.session_id == session_id)
        hits = func.count(MEMORY_SEARCH_TERMS.c.term_digest).label("term_hits")
        statement = (select(self.records, hits)
            .join(MEMORY_SEARCH_TERMS, and_(
                self.records.c.tenant_id == MEMORY_SEARCH_TERMS.c.tenant_id,
                self.records.c.memory_id == MEMORY_SEARCH_TERMS.c.memory_id))
            .where(and_(*predicates)).group_by(*self.records.c)
            .order_by(hits.desc(), self.records.c.created_at.desc()).limit(limit))
        with self._tenant_transaction(tenant_id) as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple((self._row_to_record(row), int(row["term_hits"])) for row in rows)

    def replace_search_terms(self, *, tenant_id: str, memory_id: str,
                             search_terms: dict[str, tuple[str, ...]]) -> None:
        from sqlalchemy import delete
        with self._tenant_transaction(tenant_id) as connection:
            exists = connection.execute(select(self.records.c.memory_id).where(and_(
                self.records.c.tenant_id == tenant_id,
                self.records.c.memory_id == memory_id,
            )).with_for_update()).scalar_one_or_none()
            if exists is None:
                raise MemoryError("memory is not available")
            connection.execute(delete(MEMORY_SEARCH_TERMS).where(and_(
                MEMORY_SEARCH_TERMS.c.tenant_id == tenant_id,
                MEMORY_SEARCH_TERMS.c.memory_id == memory_id,
            )))
            record = self._row_to_record(connection.execute(select(self.records).where(and_(
                self.records.c.tenant_id == tenant_id,
                self.records.c.memory_id == memory_id,
            ))).mappings().one())
            self._insert_search_terms(connection, record, search_terms)

    @staticmethod
    def _insert_search_terms(connection: Connection, record: EncryptedMemoryRecord,
                             search_terms: dict[str, tuple[str, ...]]) -> None:
        values = [{"tenant_id": record.tenant_id, "memory_id": record.memory_id,
                   "key_id": key_id, "term_digest": digest}
                  for key_id, digests in search_terms.items() for digest in set(digests)]
        if values:
            connection.execute(insert(MEMORY_SEARCH_TERMS), values)

    @contextmanager
    def _tenant_transaction(self, tenant_id: str) -> Iterator[Connection]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
                    {"tenant_id": tenant_id},
                )
            yield connection

    @staticmethod
    def _record_to_values(record: EncryptedMemoryRecord) -> dict:
        return {
            "tenant_id": record.tenant_id,
            "memory_id": record.memory_id,
            "scope": record.scope.value,
            "kind": record.kind.value,
            "status": record.status.value,
            "classification": int(record.label.classification),
            "compartments": sorted(record.label.compartments),
            "resource_id": record.label.resource_id,
            "source_type": record.source.source_type.value,
            "source_id": record.source.source_id,
            "source_uri": record.source.source_uri,
            "trust_level": int(record.source.trust_level),
            "created_by": record.created_by,
            "owner_principal_id": record.owner_principal_id,
            "project_id": record.project_id,
            "session_id": record.session_id,
            "ciphertext": record.ciphertext,
            "nonce": record.nonce,
            "content_fingerprint": record.content_fingerprint,
            "key_id": record.key_id,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "version": record.version,
        }

    @staticmethod
    def _row_to_record(row) -> EncryptedMemoryRecord:
        return EncryptedMemoryRecord(
            memory_id=row["memory_id"],
            tenant_id=row["tenant_id"],
            scope=MemoryScope(row["scope"]),
            kind=MemoryKind(row["kind"]),
            status=MemoryStatus(row["status"]),
            label=ResourceLabel(
                owner_tenant_id=row["tenant_id"],
                classification=Classification(row["classification"]),
                compartments=frozenset(row["compartments"]),
                resource_id=row["resource_id"],
            ),
            source=MemorySource(
                source_type=SourceType(row["source_type"]),
                source_id=row["source_id"],
                source_uri=row["source_uri"],
                trust_level=TrustLevel(row["trust_level"]),
            ),
            created_by=row["created_by"],
            owner_principal_id=row["owner_principal_id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            content_fingerprint=row["content_fingerprint"],
            key_id=row["key_id"],
            created_at=_as_utc(row["created_at"]),
            expires_at=_as_utc(row["expires_at"]) if row["expires_at"] else None,
            version=row["version"],
        )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
