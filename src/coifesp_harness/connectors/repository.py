from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine

from ..audit import AuditEvent
from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification, Principal
from .models import ConnectorEndpoint

CONNECTOR_METADATA = MetaData()
LIST = JSON().with_variant(ARRAY(String(256)), "postgresql")
_SECRET_ENV = re.compile(r"^COIFESP_CONNECTOR_[A-Z0-9_]{1,80}_CLIENT_SECRET$")

CONNECTOR_REGISTRATIONS = Table("connector_registrations", CONNECTOR_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("connector_id", String(64), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("proposed_action", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("base_url", String(2048), nullable=False), Column("token_endpoint", String(2048), nullable=False),
    Column("client_id", String(256), nullable=False), Column("client_secret_env", String(128), nullable=False),
    Column("scopes", LIST, nullable=False), Column("allowed_paths", LIST, nullable=False),
    Column("max_classification", Integer, nullable=False), Column("timeout_millis", Integer, nullable=False),
    Column("max_response_bytes", Integer, nullable=False), Column("max_attempts", Integer, nullable=False),
    Column("circuit_failure_threshold", Integer, nullable=False),
    Column("circuit_cooldown_millis", Integer, nullable=False),
    Column("config_digest", String(64), nullable=False), Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("reviewed_by", String(128), nullable=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=True), Column("review_reason_digest", String(64), nullable=True),
    CheckConstraint("proposed_action IN ('activate','disable')", name="action"),
    CheckConstraint("status IN ('pending','active','rejected','disabled')", name="status"),
    CheckConstraint("version > 0 AND max_classification BETWEEN 0 AND 3", name="values"),
    CheckConstraint("length(config_digest)=64", name="digest"),
    CheckConstraint("(status='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR (status<>'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="review"))


class SQLAlchemyConnectorRegistry:
    def __init__(self, *, engine: Engine, audit_log: SQLAlchemyAuditLog) -> None:
        if audit_log.engine is not engine: raise ValueError("connector registry and audit must share one engine")
        self.engine, self.audit_log = engine, audit_log
    def create_schema(self): CONNECTOR_METADATA.create_all(self.engine)
    @contextmanager
    def transaction(self, tenant):
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql": connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant":tenant})
            elif connection.dialect.name != "sqlite": raise RuntimeError("connector registry supports PostgreSQL and SQLite only")
            yield connection

    def propose(self, *, principal: Principal, endpoint: ConnectorEndpoint,
                client_secret_env: str):
        if principal.is_service or "connector_administrator" not in principal.roles or endpoint.tenant_id != principal.tenant_id:
            raise PolicyDenied("connector administrator role is required")
        if _SECRET_ENV.fullmatch(client_secret_env) is None: raise ValueError("connector secret reference is invalid")
        values = self._values(endpoint, client_secret_env); digest = self._digest(values)
        with self.transaction(principal.tenant_id) as connection:
            pending = connection.execute(select(CONNECTOR_REGISTRATIONS.c.version).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id == endpoint.connector_id,
                CONNECTOR_REGISTRATIONS.c.status == "pending"))).scalar_one_or_none()
            if pending is not None:
                raise GovernanceConflictError("connector already has a pending revision")
            latest = connection.execute(select(CONNECTOR_REGISTRATIONS.c.version).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id==principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id==endpoint.connector_id))
                .order_by(CONNECTOR_REGISTRATIONS.c.version.desc()).limit(1).with_for_update()).scalar_one_or_none()
            version = int(latest or 0)+1
            row = dict(**values, proposed_action="activate", status="pending", version=version, config_digest=digest,
                created_by=principal.principal_id, created_at=datetime.now(UTC),
                reviewed_by=None, reviewed_at=None, review_reason_digest=None)
            connection.execute(insert(CONNECTOR_REGISTRATIONS).values(**row))
            self._audit(connection, principal, endpoint.connector_id, "connector.proposed", "pending", version)
        return endpoint.connector_id, "pending", version

    def request_disable(self, *, principal: Principal, connector_id: str):
        if principal.is_service or "connector_administrator" not in principal.roles:
            raise PolicyDenied("connector administrator role is required")
        with self.transaction(principal.tenant_id) as connection:
            pending = connection.execute(select(CONNECTOR_REGISTRATIONS.c.version).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id == connector_id,
                CONNECTOR_REGISTRATIONS.c.status == "pending"))).scalar_one_or_none()
            if pending is not None:
                raise GovernanceConflictError("connector already has a pending revision")
            active = connection.execute(select(CONNECTOR_REGISTRATIONS).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id == connector_id,
                CONNECTOR_REGISTRATIONS.c.status == "active"))
                .order_by(CONNECTOR_REGISTRATIONS.c.version.desc()).limit(1).with_for_update()).mappings().one_or_none()
            if active is None:
                raise ResourceNotFound("active connector is absent or hidden")
            version = int(connection.execute(select(CONNECTOR_REGISTRATIONS.c.version).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id == connector_id))
                .order_by(CONNECTOR_REGISTRATIONS.c.version.desc()).limit(1)).scalar_one()) + 1
            values = {key: active[key] for key in (
                "tenant_id", "connector_id", "base_url", "token_endpoint", "client_id",
                "client_secret_env", "scopes", "allowed_paths", "max_classification",
                "timeout_millis", "max_response_bytes", "max_attempts",
                "circuit_failure_threshold", "circuit_cooldown_millis", "config_digest")}
            connection.execute(insert(CONNECTOR_REGISTRATIONS).values(**values,
                proposed_action="disable", status="pending", version=version,
                created_by=principal.principal_id, created_at=datetime.now(UTC),
                reviewed_by=None, reviewed_at=None, review_reason_digest=None))
            self._audit(connection, principal, connector_id,
                "connector.disable_requested", "pending", version)
        return connector_id, "pending", version

    def review(self, *, principal: Principal, connector_id: str, expected_version: int,
               approve: bool, reason: str):
        if principal.is_service or "connector_reviewer" not in principal.roles: raise PolicyDenied("connector reviewer role is required")
        if not reason.strip() or len(reason)>2000: raise ValueError("connector review reason is invalid")
        with self.transaction(principal.tenant_id) as connection:
            where=and_(CONNECTOR_REGISTRATIONS.c.tenant_id==principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id==connector_id,
                CONNECTOR_REGISTRATIONS.c.version==expected_version)
            row=connection.execute(select(CONNECTOR_REGISTRATIONS).where(where).with_for_update()).mappings().one_or_none()
            if row is None: raise ResourceNotFound("connector is absent or hidden")
            if row["status"]!="pending" or int(row["version"])!=expected_version or row["created_by"]==principal.principal_id:
                raise GovernanceConflictError("connector review violates state, version, or separation of duties")
            target = ("active" if row["proposed_action"] == "activate" else "disabled") if approve else "rejected"
            if approve:
                connection.execute(update(CONNECTOR_REGISTRATIONS).where(and_(
                    CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                    CONNECTOR_REGISTRATIONS.c.connector_id == connector_id,
                    CONNECTOR_REGISTRATIONS.c.status == "active"
                )).values(status="disabled"))
            connection.execute(update(CONNECTOR_REGISTRATIONS).where(where).values(status=target,
                reviewed_by=principal.principal_id, reviewed_at=datetime.now(UTC),
                review_reason_digest=hashlib.sha256(reason.encode()).hexdigest()))
            self._audit(connection, principal, connector_id, "connector.reviewed", target, expected_version)
        return connector_id,target,expected_version

    def available_for_tenant(self, *, principal: Principal):
        """Workspace-safe connector status for the caller's own tenant.

        Returns only identifiers and lifecycle state; endpoint URLs, client ids
        and secret references are never projected to the workspace. The query
        is scoped to the principal tenant so other teams cannot enumerate
        another tenant's connectors.
        """
        with self.transaction(principal.tenant_id) as connection:
            latest = connection.execute(
                select(
                    CONNECTOR_REGISTRATIONS.c.connector_id,
                    func.max(CONNECTOR_REGISTRATIONS.c.version).label("version"),
                )
                .where(CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id)
                .group_by(CONNECTOR_REGISTRATIONS.c.connector_id)
            ).all()
            items = []
            for connector_id, version in latest:
                row = connection.execute(
                    select(
                        CONNECTOR_REGISTRATIONS.c.status,
                        CONNECTOR_REGISTRATIONS.c.proposed_action,
                        CONNECTOR_REGISTRATIONS.c.created_at,
                        CONNECTOR_REGISTRATIONS.c.reviewed_at,
                    )
                    .where(
                        and_(
                            CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                            CONNECTOR_REGISTRATIONS.c.connector_id == connector_id,
                            CONNECTOR_REGISTRATIONS.c.version == version,
                        )
                    )
                ).mappings().one()
                items.append(
                    {
                        "connector_id": connector_id,
                        "auth_type": "client_credentials",
                        "status": row["status"],
                        "proposed_action": row["proposed_action"],
                        "version": int(version),
                        "created_at": row["created_at"],
                        "reviewed_at": row["reviewed_at"],
                    }
                )
        return sorted(items, key=lambda item: item["connector_id"])

    def read_revision(self, *, principal: Principal, connector_id: str, version: int):
        if principal.is_service or not principal.roles.intersection(
                {"connector_administrator", "connector_reviewer"}):
            raise PolicyDenied("connector review access is required")
        with self.transaction(principal.tenant_id) as connection:
            row = connection.execute(select(CONNECTOR_REGISTRATIONS).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id == principal.tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id == connector_id,
                CONNECTOR_REGISTRATIONS.c.version == version))).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("connector revision is absent or hidden")
        return {key: row[key] for key in (
            "connector_id", "version", "proposed_action", "status", "base_url",
            "token_endpoint", "client_id", "client_secret_env", "scopes",
            "allowed_paths", "max_classification", "timeout_millis",
            "max_response_bytes", "max_attempts", "circuit_failure_threshold",
            "circuit_cooldown_millis", "config_digest", "created_by", "created_at",
            "reviewed_by", "reviewed_at")}

    def active_endpoint(self, *, principal: Principal, connector_id: str,
                        environment) -> ConnectorEndpoint:
        return self.active_endpoint_for_worker(
            tenant_id=principal.tenant_id,
            connector_id=connector_id,
            environment=environment,
        )

    def active_endpoint_for_worker(self, *, tenant_id: str, connector_id: str,
                                   environment) -> ConnectorEndpoint:
        """Resolve the latest reviewed active revision for one tenant worker."""
        with self.transaction(tenant_id) as connection:
            row=connection.execute(select(CONNECTOR_REGISTRATIONS).where(and_(
                CONNECTOR_REGISTRATIONS.c.tenant_id==tenant_id,
                CONNECTOR_REGISTRATIONS.c.connector_id==connector_id,
                CONNECTOR_REGISTRATIONS.c.status=="active"))
                .order_by(CONNECTOR_REGISTRATIONS.c.version.desc()).limit(1)).mappings().one_or_none()
        if row is None: raise ResourceNotFound("connector is absent or disabled")
        secret=environment.get(row["client_secret_env"],"").strip()
        if not secret: raise PolicyDenied("connector secret is unavailable")
        from ..config import SecretValue
        return ConnectorEndpoint(connector_id=row["connector_id"],tenant_id=row["tenant_id"],
            base_url=row["base_url"],token_endpoint=row["token_endpoint"],client_id=row["client_id"],
            client_secret=SecretValue(secret),scopes=tuple(row["scopes"]),allowed_paths=frozenset(row["allowed_paths"]),
            max_classification=Classification(int(row["max_classification"])),timeout_seconds=row["timeout_millis"]/1000,
            max_response_bytes=int(row["max_response_bytes"]),max_attempts=int(row["max_attempts"]),
            circuit_failure_threshold=int(row["circuit_failure_threshold"]),
            circuit_cooldown_seconds=row["circuit_cooldown_millis"]/1000)

    @staticmethod
    def _values(e, secret_env): return {
        "tenant_id": e.tenant_id, "connector_id": e.connector_id,
        "base_url": e.base_url, "token_endpoint": e.token_endpoint,
        "client_id": e.client_id, "client_secret_env": secret_env,
        "scopes": list(e.scopes), "allowed_paths": sorted(e.allowed_paths),
        "max_classification": int(e.max_classification),
        "timeout_millis": round(e.timeout_seconds*1000),
        "max_response_bytes": e.max_response_bytes, "max_attempts": e.max_attempts,
        "circuit_failure_threshold": e.circuit_failure_threshold,
        "circuit_cooldown_millis": round(e.circuit_cooldown_seconds*1000),
    }
    @staticmethod
    def _digest(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def _audit(self,c,p,cid,event,outcome,version): self.audit_log.append_in_transaction(c,AuditEvent(
        tenant_id=p.tenant_id,event_type=event,actor_id=p.principal_id,outcome=outcome,
        details={"connector_id":cid,"version":version},correlation_id=cid))
