from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import (
    ARRAY, JSON, CheckConstraint, Column, DateTime, Integer, MetaData, String, Table,
    Text, UniqueConstraint, and_, insert, select, text, update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..audit import AuditEvent
from ..errors import GovernanceConflictError, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification
from .models import CapabilityCapacity, CapacityNegotiation, CapacityReservation, TeamCapability

CAPABILITY_METADATA = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
})
LIST = JSON().with_variant(ARRAY(String(128)), "postgresql")
OBJECT = JSON().with_variant(JSONB(), "postgresql")

TEAM_CAPABILITIES = Table(
    "team_capabilities", CAPABILITY_METADATA,
    Column("provider_tenant_id", String(128), primary_key=True),
    Column("capability_id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("name", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("tags", LIST, nullable=False),
    Column("protocols", LIST, nullable=False),
    Column("input_contract", String(1024), nullable=False),
    Column("output_contract", String(1024), nullable=False),
    Column("max_input_classification", Integer, nullable=False),
    Column("required_compartments", LIST, nullable=False),
    Column("residency_regions", LIST, nullable=False),
    Column("visible_to_tenants", LIST, nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("published_by", String(128), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("max_input_classification BETWEEN 0 AND 3", name="classification"),
    CheckConstraint("length(content_digest)=64", name="digest"),
    UniqueConstraint("provider_tenant_id", "capability_id", "content_digest", name="uq_team_capability_content"),
)

CAPABILITY_COMMANDS = Table(
    "team_capability_commands", CAPABILITY_METADATA,
    Column("tenant_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("request_digest", String(64), nullable=False),
    Column("capability_id", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("length(request_digest)=64", name="request_digest"),
)

CAPABILITY_EVENTS = Table(
    "team_capability_events", CAPABILITY_METADATA,
    Column("provider_tenant_id", String(128), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("capability_id", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("payload", OBJECT, nullable=False),
    Column("visible_to_tenants", LIST, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider_tenant_id", "event_id", name="uq_team_capability_event"),
    CheckConstraint("sequence > 0", name="sequence"),
)

CAPABILITY_CAPACITY = Table(
    "team_capability_capacity", CAPABILITY_METADATA,
    Column("provider_tenant_id", String(128), primary_key=True),
    Column("capability_id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("available_slots", Integer, nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("state_version", Integer, nullable=False),
    Column("updated_by", String(128), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("visible_to_tenants", LIST, nullable=False),
    CheckConstraint("status IN ('available','limited','unavailable')", name="status"),
    CheckConstraint("available_slots >= 0 AND state_version > 0", name="values"),
)

CAPACITY_RESERVATIONS = Table(
    "team_capacity_reservations", CAPABILITY_METADATA,
    Column("provider_tenant_id", String(128), primary_key=True),
    Column("reservation_id", String(128), primary_key=True),
    Column("capability_id", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    Column("consumer_tenant_id", String(128), nullable=False),
    Column("slots", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("released_by", String(128), nullable=True),
    Column("released_at", DateTime(timezone=True), nullable=True),
    Column("visible_to_tenants", LIST, nullable=False),
    CheckConstraint("slots > 0", name="slots"),
    CheckConstraint("status IN ('active','released','expired')", name="status"),
)

CAPACITY_NEGOTIATIONS = Table(
    "team_capacity_negotiations", CAPABILITY_METADATA,
    Column("provider_tenant_id", String(128), primary_key=True),
    Column("negotiation_id", String(128), primary_key=True),
    Column("consumer_tenant_id", String(128), nullable=False),
    Column("capability_id", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    Column("requested_slots", Integer, nullable=False),
    Column("earliest_start", DateTime(timezone=True), nullable=False),
    Column("latest_end", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("state_version", Integer, nullable=False),
    Column("request_reason_digest", String(64), nullable=False),
    Column("decision_reason_digest", String(64), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", String(128), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("visible_to_tenants", LIST, nullable=False),
    CheckConstraint("requested_slots > 0 AND state_version > 0", name="values"),
    CheckConstraint("status IN ('proposed','accepted','rejected','withdrawn')", name="status"),
    CheckConstraint("length(request_reason_digest)=64 AND (decision_reason_digest IS NULL OR length(decision_reason_digest)=64)", name="digests"),
)


class SQLAlchemyCapabilityRepository:
    def __init__(self, *, engine: Engine, audit_log: SQLAlchemyAuditLog) -> None:
        if audit_log.engine is not engine:
            raise ValueError("capability directory and audit must share one engine")
        self.engine = engine
        self.audit_log = audit_log

    def create_schema(self) -> None:
        CAPABILITY_METADATA.create_all(self.engine)

    @contextmanager
    def transaction(self, tenant_id: str) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            self.set_tenant(connection, tenant_id)
            yield connection

    @staticmethod
    def set_tenant(connection: Connection, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("tenant_id is invalid")
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant": tenant_id})
        elif connection.dialect.name != "sqlite":
            raise RuntimeError("capability directory supports PostgreSQL and SQLite only")

    def claim(self, connection: Connection, *, tenant_id: str, key: str, digest: str,
              capability_id: str, version: str) -> bool:
        values = dict(tenant_id=tenant_id, idempotency_key=key, request_digest=digest,
                      capability_id=capability_id, version=version, created_at=datetime.now(UTC))
        base = pg_insert(CAPABILITY_COMMANDS) if connection.dialect.name == "postgresql" else sqlite_insert(CAPABILITY_COMMANDS)
        inserted = connection.execute(
            base.values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
            .returning(CAPABILITY_COMMANDS.c.idempotency_key)
        ).scalar_one_or_none()
        row = connection.execute(select(CAPABILITY_COMMANDS).where(and_(
            CAPABILITY_COMMANDS.c.tenant_id == tenant_id,
            CAPABILITY_COMMANDS.c.idempotency_key == key,
        ))).mappings().one()
        if row["request_digest"] != digest or row["capability_id"] != capability_id or row["version"] != version:
            raise GovernanceConflictError("idempotency key was reused with a different capability publication")
        return inserted is None

    def insert(self, connection: Connection, *, values: dict, event: AuditEvent) -> TeamCapability:
        try:
            connection.execute(insert(TEAM_CAPABILITIES).values(**values))
        except Exception as exc:
            existing = connection.execute(select(TEAM_CAPABILITIES).where(and_(
                TEAM_CAPABILITIES.c.provider_tenant_id == values["provider_tenant_id"],
                TEAM_CAPABILITIES.c.capability_id == values["capability_id"],
                TEAM_CAPABILITIES.c.version == values["version"],
            ))).mappings().one_or_none()
            if existing is not None:
                raise GovernanceConflictError("capability version already exists") from exc
            raise
        if connection.dialect.name == "postgresql":
            connection.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('coifesp-capability-events-v1'),hashtext(:tenant))"
            ), {"tenant": values["provider_tenant_id"]})
        last = connection.execute(select(CAPABILITY_EVENTS.c.sequence).where(
            CAPABILITY_EVENTS.c.provider_tenant_id == values["provider_tenant_id"]
        ).order_by(CAPABILITY_EVENTS.c.sequence.desc()).limit(1).with_for_update()).scalar_one_or_none()
        normalized = event.normalized()
        connection.execute(insert(CAPABILITY_EVENTS).values(
            provider_tenant_id=values["provider_tenant_id"], sequence=int(last or 0) + 1,
            event_id=normalized.event_id, event_type=normalized.event_type,
            capability_id=values["capability_id"], version=values["version"],
            actor_id=normalized.actor_id,
            payload={"content_digest": values["content_digest"]},
            visible_to_tenants=values["visible_to_tenants"],
            occurred_at=datetime.fromisoformat(normalized.occurred_at),
        ))
        self.audit_log.append_in_transaction(connection, normalized)
        return self._record(values)

    def get(self, connection: Connection, *, provider_tenant_id: str,
            capability_id: str, version: str) -> TeamCapability:
        row = connection.execute(select(TEAM_CAPABILITIES).where(and_(
            TEAM_CAPABILITIES.c.provider_tenant_id == provider_tenant_id,
            TEAM_CAPABILITIES.c.capability_id == capability_id,
            TEAM_CAPABILITIES.c.version == version,
        ))).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("capability is absent or hidden")
        return self._record(row)

    def list(self, connection: Connection, *, protocol: str | None = None,
             tag: str | None = None, limit: int = 100) -> tuple[TeamCapability, ...]:
        rows = connection.execute(select(TEAM_CAPABILITIES).order_by(
            TEAM_CAPABILITIES.c.provider_tenant_id, TEAM_CAPABILITIES.c.capability_id,
            TEAM_CAPABILITIES.c.published_at.desc(),
        )).mappings().all()
        values = [self._record(row) for row in rows]
        if protocol:
            values = [item for item in values if protocol in item.protocols]
        if tag:
            values = [item for item in values if tag in item.tags]
        return tuple(values[:limit])

    def upsert_capacity(self, connection: Connection, *, capability: TeamCapability,
                        status: str, available_slots: int, valid_until: datetime,
                        expected_version: int | None, actor_id: str) -> CapabilityCapacity:
        where = and_(CAPABILITY_CAPACITY.c.provider_tenant_id == capability.provider_tenant_id,
            CAPABILITY_CAPACITY.c.capability_id == capability.capability_id,
            CAPABILITY_CAPACITY.c.version == capability.version)
        existing = connection.execute(select(CAPABILITY_CAPACITY).where(where).with_for_update()).mappings().one_or_none()
        current = int(existing["state_version"]) if existing else 0
        if expected_version != (current or None):
            raise GovernanceConflictError("capacity state version is stale")
        values = dict(provider_tenant_id=capability.provider_tenant_id,
            capability_id=capability.capability_id, version=capability.version,
            status=status, available_slots=available_slots, valid_until=valid_until,
            state_version=current + 1, updated_by=actor_id, updated_at=datetime.now(UTC),
            visible_to_tenants=list(capability.visible_to_tenants))
        if existing:
            connection.execute(update(CAPABILITY_CAPACITY).where(where).values(**values))
        else:
            connection.execute(insert(CAPABILITY_CAPACITY).values(**values))
        return self._capacity(values)

    def capacities(self, connection: Connection) -> dict[tuple[str, str, str], CapabilityCapacity]:
        now = datetime.now(UTC)
        reserved: dict[tuple[str, str, str], int] = {}
        for row in connection.execute(select(CAPACITY_RESERVATIONS).where(and_(
                CAPACITY_RESERVATIONS.c.status == "active",
                CAPACITY_RESERVATIONS.c.expires_at > now))).mappings():
            key = (row["provider_tenant_id"], row["capability_id"], row["version"])
            reserved[key] = reserved.get(key, 0) + int(row["slots"])
        result = {}
        for row in connection.execute(select(CAPABILITY_CAPACITY)).mappings():
            key = (row["provider_tenant_id"], row["capability_id"], row["version"])
            value = dict(row)
            value["available_slots"] = max(0, int(row["available_slots"]) - reserved.get(key, 0))
            result[key] = self._capacity(value)
        return result

    def reserve(self, connection: Connection, *, reservation_id: str,
                capability: TeamCapability, consumer_tenant_id: str, slots: int,
                expires_at: datetime, actor_id: str) -> tuple[CapacityReservation, bool]:
        existing = connection.execute(select(CAPACITY_RESERVATIONS).where(and_(
            CAPACITY_RESERVATIONS.c.provider_tenant_id == capability.provider_tenant_id,
            CAPACITY_RESERVATIONS.c.reservation_id == reservation_id))).mappings().one_or_none()
        if existing is not None:
            existing_expires = existing["expires_at"]
            if existing_expires.tzinfo is None: existing_expires = existing_expires.replace(tzinfo=UTC)
            if (existing["capability_id"] != capability.capability_id
                    or existing["version"] != capability.version
                    or existing["consumer_tenant_id"] != consumer_tenant_id
                    or int(existing["slots"]) != slots or existing_expires != expires_at):
                raise GovernanceConflictError("reservation ID was reused with different parameters")
            return self._reservation(existing), True
        capacity_where = and_(CAPABILITY_CAPACITY.c.provider_tenant_id == capability.provider_tenant_id,
            CAPABILITY_CAPACITY.c.capability_id == capability.capability_id,
            CAPABILITY_CAPACITY.c.version == capability.version)
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:provider),hashtext(:capability))"),
                {"provider": capability.provider_tenant_id,
                 "capability": f"{capability.capability_id}:{capability.version}"})
        capacity = connection.execute(select(CAPABILITY_CAPACITY).where(capacity_where)).mappings().one_or_none()
        now = datetime.now(UTC)
        capacity_valid_until = capacity["valid_until"] if capacity else None
        if capacity_valid_until is not None and capacity_valid_until.tzinfo is None:
            capacity_valid_until = capacity_valid_until.replace(tzinfo=UTC)
        if not capacity or capacity_valid_until <= now or capacity["status"] == "unavailable":
            raise GovernanceConflictError("capability capacity is unavailable")
        active = connection.execute(select(CAPACITY_RESERVATIONS.c.slots).where(and_(
            CAPACITY_RESERVATIONS.c.provider_tenant_id == capability.provider_tenant_id,
            CAPACITY_RESERVATIONS.c.capability_id == capability.capability_id,
            CAPACITY_RESERVATIONS.c.version == capability.version,
            CAPACITY_RESERVATIONS.c.status == "active",
            CAPACITY_RESERVATIONS.c.expires_at > now))).scalars().all()
        if sum(active) + slots > int(capacity["available_slots"]):
            raise GovernanceConflictError("capability capacity reservation conflicts")
        values = dict(provider_tenant_id=capability.provider_tenant_id,
            reservation_id=reservation_id, capability_id=capability.capability_id,
            version=capability.version, consumer_tenant_id=consumer_tenant_id,
            slots=slots, status="active", expires_at=expires_at, created_by=actor_id,
            created_at=now, released_by=None, released_at=None,
            visible_to_tenants=sorted({capability.provider_tenant_id, consumer_tenant_id}))
        connection.execute(insert(CAPACITY_RESERVATIONS).values(**values))
        return self._reservation(values), False

    def release_reservation(self, connection: Connection, *, provider_tenant_id: str,
                            reservation_id: str, actor_tenant_id: str,
                            actor_id: str) -> CapacityReservation:
        where = and_(CAPACITY_RESERVATIONS.c.provider_tenant_id == provider_tenant_id,
            CAPACITY_RESERVATIONS.c.reservation_id == reservation_id)
        row = connection.execute(select(CAPACITY_RESERVATIONS).where(where).with_for_update()).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("capacity reservation is absent or hidden")
        if actor_tenant_id not in {row["provider_tenant_id"], row["consumer_tenant_id"]}:
            raise ResourceNotFound("capacity reservation is absent or hidden")
        if row["status"] != "active":
            raise GovernanceConflictError("capacity reservation is not active")
        now = datetime.now(UTC)
        connection.execute(update(CAPACITY_RESERVATIONS).where(where).values(
            status="released", released_by=actor_id, released_at=now))
        changed = dict(row); changed.update(status="released")
        return self._reservation(changed)

    def propose_negotiation(self, connection: Connection, *, capability: TeamCapability,
                            negotiation_id: str, consumer_tenant_id: str,
                            requested_slots: int, earliest_start: datetime,
                            latest_end: datetime, reason_digest: str,
                            actor_id: str) -> CapacityNegotiation:
        values = dict(provider_tenant_id=capability.provider_tenant_id,
            negotiation_id=negotiation_id, consumer_tenant_id=consumer_tenant_id,
            capability_id=capability.capability_id, version=capability.version,
            requested_slots=requested_slots, earliest_start=earliest_start,
            latest_end=latest_end, status="proposed", state_version=1,
            request_reason_digest=reason_digest, decision_reason_digest=None,
            created_by=actor_id, created_at=datetime.now(UTC), decided_by=None,
            decided_at=None, visible_to_tenants=sorted({capability.provider_tenant_id,
                consumer_tenant_id}))
        try:
            connection.execute(insert(CAPACITY_NEGOTIATIONS).values(**values))
        except Exception as exc:
            raise GovernanceConflictError("capacity negotiation already exists") from exc
        return self._negotiation(values)

    def decide_negotiation(self, connection: Connection, *, provider_tenant_id: str,
                           negotiation_id: str, actor_tenant_id: str, actor_id: str,
                           expected_version: int, target: str,
                           reason_digest: str) -> CapacityNegotiation:
        where = and_(CAPACITY_NEGOTIATIONS.c.provider_tenant_id == provider_tenant_id,
            CAPACITY_NEGOTIATIONS.c.negotiation_id == negotiation_id)
        row = connection.execute(select(CAPACITY_NEGOTIATIONS).where(where).with_for_update()).mappings().one_or_none()
        if row is None: raise ResourceNotFound("capacity negotiation is absent or hidden")
        allowed = (actor_tenant_id == row["provider_tenant_id"] and target in {"accepted", "rejected"}) or (
            actor_tenant_id == row["consumer_tenant_id"] and target == "withdrawn")
        if not allowed or row["status"] != "proposed" or int(row["state_version"]) != expected_version:
            raise GovernanceConflictError("capacity negotiation state transition conflicts")
        now = datetime.now(UTC)
        connection.execute(update(CAPACITY_NEGOTIATIONS).where(where).values(status=target,
            state_version=expected_version + 1, decision_reason_digest=reason_digest,
            decided_by=actor_id, decided_at=now))
        changed = dict(row); changed.update(status=target, state_version=expected_version + 1)
        return self._negotiation(changed)

    @staticmethod
    def _negotiation(row) -> CapacityNegotiation:
        def aware(value): return value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return CapacityNegotiation(row["negotiation_id"], row["provider_tenant_id"],
            row["consumer_tenant_id"], row["capability_id"], row["version"],
            int(row["requested_slots"]), aware(row["earliest_start"]),
            aware(row["latest_end"]), row["status"], int(row["state_version"]))

    @staticmethod
    def _reservation(row) -> CapacityReservation:
        expires = row["expires_at"]
        if expires.tzinfo is None: expires = expires.replace(tzinfo=UTC)
        return CapacityReservation(row["reservation_id"], row["provider_tenant_id"],
            row["capability_id"], row["version"], row["consumer_tenant_id"],
            int(row["slots"]), row["status"], expires)

    @staticmethod
    def _capacity(row) -> CapabilityCapacity:
        valid = row["valid_until"]
        if valid.tzinfo is None: valid = valid.replace(tzinfo=UTC)
        return CapabilityCapacity(row["provider_tenant_id"], row["capability_id"],
            row["version"], row["status"], int(row["available_slots"]), valid,
            int(row["state_version"]))

    @staticmethod
    def _record(row) -> TeamCapability:
        published = row["published_at"]
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return TeamCapability(
            capability_id=row["capability_id"], provider_tenant_id=row["provider_tenant_id"],
            version=row["version"], name=row["name"], description=row["description"],
            tags=tuple(row["tags"]), protocols=tuple(row["protocols"]),
            input_contract=row["input_contract"], output_contract=row["output_contract"],
            max_input_classification=Classification(row["max_input_classification"]),
            required_compartments=tuple(row["required_compartments"]),
            residency_regions=tuple(row["residency_regions"]),
            visible_to_tenants=tuple(row["visible_to_tenants"]), content_digest=row["content_digest"],
            published_by=row["published_by"], published_at=published,
        )
