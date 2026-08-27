from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from ..audit import AuditEvent
from ..errors import GovernanceConflictError, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification, ResourceLabel
from .models import ArtifactKind, ArtifactManifest, ArtifactProvenance

ARTIFACT_METADATA = MetaData()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LIST = JSON().with_variant(ARRAY(String(128)), "postgresql")
OBJECT = JSON().with_variant(JSONB(), "postgresql")

ARTIFACT_MANIFESTS = Table(
    "artifact_manifests",
    ARTIFACT_METADATA,
    Column("owner_tenant_id", String(128), primary_key=True),
    Column("artifact_id", String(128), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("media_type", String(256), nullable=False),
    Column("content_uri", String(2048), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("classification", Integer, nullable=False),
    Column("compartments", LIST, nullable=False),
    Column("producer_principal_id", String(128), nullable=False),
    Column("source_tool", String(128), nullable=False),
    Column("source_version", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("visible_to_tenants", LIST, nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("metadata_schema", String(128), nullable=False),
    CheckConstraint("classification BETWEEN 0 AND 3", name="classification"),
    CheckConstraint(
        "size_bytes >= 0 AND length(sha256)=64 AND length(content_digest)=64", name="integrity"
    ),
)

ARTIFACT_COMMANDS = Table(
    "artifact_manifest_commands",
    ARTIFACT_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("request_digest", String(64), nullable=False),
    Column("artifact_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(request_digest)=64", name="digest"),
)


class SQLAlchemyArtifactRepository:
    def __init__(self, *, engine: Engine, audit_log: SQLAlchemyAuditLog) -> None:
        if audit_log.engine is not engine:
            raise ValueError("artifact registry and audit must share one engine")
        self.engine, self.audit_log = engine, audit_log

    def create_schema(self):
        ARTIFACT_METADATA.create_all(self.engine)

    @contextmanager
    def transaction(self, tenant_id):
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
                    {"tenant": tenant_id},
                )
            elif connection.dialect.name != "sqlite":
                raise RuntimeError("artifact registry supports PostgreSQL and SQLite only")
            yield connection

    def publish(self, *, principal, idempotency_key: str, manifest: ArtifactManifest):
        if _ID.fullmatch(idempotency_key) is None or _ID.fullmatch(manifest.artifact_id) is None:
            raise ValueError("artifact publication identifier is invalid")
        if (
            principal.is_service
            or "artifact_publisher" not in principal.roles
            or manifest.label.owner_tenant_id != principal.tenant_id
            or manifest.provenance.producer_principal_id != principal.principal_id
            or manifest.provenance.producer_tenant_id != principal.tenant_id
        ):
            from ..errors import PolicyDenied

            raise PolicyDenied("artifact publication identity is not authorized")
        canonical = self._canonical(manifest)
        request_digest = hashlib.sha256(canonical).hexdigest()
        with self.transaction(principal.tenant_id) as connection:
            base = (
                pg_insert(ARTIFACT_COMMANDS)
                if connection.dialect.name == "postgresql"
                else sqlite_insert(ARTIFACT_COMMANDS)
            )
            inserted = connection.execute(
                base.values(
                    tenant_id=principal.tenant_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    artifact_id=manifest.artifact_id,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
                .returning(ARTIFACT_COMMANDS.c.idempotency_key)
            ).scalar_one_or_none()
            claim = (
                connection.execute(
                    select(ARTIFACT_COMMANDS).where(
                        and_(
                            ARTIFACT_COMMANDS.c.tenant_id == principal.tenant_id,
                            ARTIFACT_COMMANDS.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .one()
            )
            if claim["request_digest"] != request_digest:
                raise GovernanceConflictError("artifact idempotency key was reused")
            if inserted is None:
                return (
                    self.get(
                        connection,
                        principal=principal,
                        owner_tenant_id=principal.tenant_id,
                        artifact_id=manifest.artifact_id,
                    ),
                    True,
                )
            values = self._values(manifest, request_digest)
            try:
                connection.execute(insert(ARTIFACT_MANIFESTS).values(**values))
            except Exception as exc:
                raise GovernanceConflictError("artifact manifest already exists") from exc
            self.audit_log.append_in_transaction(
                connection,
                AuditEvent(
                    tenant_id=principal.tenant_id,
                    event_type="artifact.published",
                    actor_id=principal.principal_id,
                    outcome="published",
                    details={
                        "artifact_id": manifest.artifact_id,
                        "sha256": manifest.sha256,
                        "content_digest": request_digest,
                    },
                    correlation_id=manifest.artifact_id,
                ),
            )
            return manifest, False

    def read(
        self,
        *,
        principal,
        owner_tenant_id: str,
        artifact_id: str,
        expected_sha256: str | None = None,
    ):
        with self.transaction(principal.tenant_id) as connection:
            value = self.get(
                connection,
                principal=principal,
                owner_tenant_id=owner_tenant_id,
                artifact_id=artifact_id,
            )
        if expected_sha256 is not None and value.sha256 != expected_sha256:
            from ..errors import IntegrityError

            raise IntegrityError("artifact content digest does not match the reference")
        return value

    def list_visible(self, *, principal, limit: int = 100):
        if not 1 <= limit <= 500:
            raise ValueError("artifact list limit is invalid")
        with self.transaction(principal.tenant_id) as connection:
            rows = (
                connection.execute(
                    select(ARTIFACT_MANIFESTS)
                    .order_by(ARTIFACT_MANIFESTS.c.created_at.desc())
                    .limit(500)
                )
                .mappings()
                .all()
            )
            visible = tuple(
                self._record(row)
                for row in rows
                if principal.tenant_id in row["visible_to_tenants"]
                and int(row["classification"]) <= int(principal.clearance)
                and set(row["compartments"]).issubset(principal.compartments)
            )
            return visible[:limit]

    def get(self, connection, *, principal, owner_tenant_id, artifact_id):
        row = (
            connection.execute(
                select(ARTIFACT_MANIFESTS).where(
                    and_(
                        ARTIFACT_MANIFESTS.c.owner_tenant_id == owner_tenant_id,
                        ARTIFACT_MANIFESTS.c.artifact_id == artifact_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is not None
            and principal.tenant_id in row["visible_to_tenants"]
            and int(row["classification"]) <= int(principal.clearance)
            and set(row["compartments"]).issubset(principal.compartments)
        ):
            return self._record(row)
        raise ResourceNotFound("artifact is absent or hidden")

    @staticmethod
    def _canonical(value):
        values = SQLAlchemyArtifactRepository._values(value, None)
        # The publication timestamp is assigned by the server and is not part of the
        # caller's logical request. Excluding it makes a retry with the same
        # idempotency key converge on the first immutable manifest.
        values.pop("created_at")
        return json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()

    @staticmethod
    def _values(value, digest):
        result = dict(
            owner_tenant_id=value.label.owner_tenant_id,
            artifact_id=value.artifact_id,
            kind=value.kind.value,
            media_type=value.media_type,
            content_uri=value.content_uri,
            sha256=value.sha256,
            size_bytes=value.size_bytes,
            classification=int(value.label.classification),
            compartments=sorted(value.label.compartments),
            producer_principal_id=value.provenance.producer_principal_id,
            source_tool=value.provenance.source_tool,
            source_version=value.provenance.source_version,
            created_at=value.provenance.created_at,
            visible_to_tenants=sorted(value.visible_to_tenants),
            metadata_schema=value.metadata_schema,
        )
        if digest is not None:
            result["content_digest"] = digest
        return result

    @staticmethod
    def _record(row):
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return ArtifactManifest(
            row["artifact_id"],
            ArtifactKind(row["kind"]),
            row["media_type"],
            row["content_uri"],
            row["sha256"],
            int(row["size_bytes"]),
            ResourceLabel(
                row["owner_tenant_id"],
                Classification(int(row["classification"])),
                frozenset(row["compartments"]),
                f"artifact:{row['artifact_id']}",
            ),
            ArtifactProvenance(
                row["producer_principal_id"],
                row["owner_tenant_id"],
                row["source_tool"],
                row["source_version"],
                created,
            ),
            frozenset(row["visible_to_tenants"]),
            row["metadata_schema"],
        )
