from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.audit import AuditEvent
from coifesp_harness.errors import IntegrityError
from coifesp_harness.postgres_audit import (
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)


def audit_log(*, key_id="audit-v1", keys=None):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    keyring = AuditSigningKeyring(
        active_key_id=key_id,
        verification_keys=keys or {key_id: b"k" * 32},
    )
    audit = SQLAlchemyAuditLog(engine=engine, keyring=keyring)
    audit.create_schema()
    return audit


def event(
    *,
    tenant_id="tenant-a",
    event_id="event-1",
    occurred_at="2026-07-29T10:00:00+00:00",
    outcome="allowed",
):
    return AuditEvent(
        tenant_id=tenant_id,
        event_type="memory.write",
        actor_id="user-1",
        outcome=outcome,
        details={"memory_id": "memory-1"},
        correlation_id="correlation-1",
        event_id=event_id,
        occurred_at=occurred_at,
    )


def test_sqlalchemy_audit_builds_and_verifies_per_tenant_chain() -> None:
    audit = audit_log()
    audit.append(event(event_id="event-1"))
    audit.append(
        event(
            event_id="event-2",
            occurred_at="2026-07-29T10:01:00+00:00",
        )
    )
    audit.append(
        event(
            tenant_id="tenant-b",
            event_id="event-3",
            occurred_at="2026-07-29T10:02:00+00:00",
        )
    )

    assert audit.verify_tenant_chain("tenant-a") == 2
    assert audit.verify_tenant_chain("tenant-b") == 1


def test_audit_append_is_idempotent_but_rejects_event_id_conflict() -> None:
    audit = audit_log()
    original = event()

    assert audit.append(original) == "event-1"
    assert audit.append(original) == "event-1"
    assert audit.verify_tenant_chain("tenant-a") == 1

    with pytest.raises(IntegrityError, match="different content"):
        audit.append(replace(original, outcome="denied"))


def test_audit_tampering_is_detected() -> None:
    audit = audit_log()
    audit.append(event())
    with audit.engine.begin() as connection:
        connection.execute(
            update(audit.events)
            .where(audit.events.c.tenant_id == "tenant-a")
            .values(payload='{"tampered":true}')
        )

    with pytest.raises(IntegrityError, match="hash"):
        audit.verify_tenant_chain("tenant-a")


def test_audit_key_rotation_verifies_old_and_new_events() -> None:
    first = audit_log(
        key_id="audit-v1",
        keys={"audit-v1": b"a" * 32},
    )
    first.append(event(event_id="event-v1"))

    rotated = SQLAlchemyAuditLog(
        engine=first.engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v2",
            verification_keys={
                "audit-v1": b"a" * 32,
                "audit-v2": b"b" * 32,
            },
        ),
    )
    rotated.append(
        event(
            event_id="event-v2",
            occurred_at="2026-07-29T10:01:00+00:00",
        )
    )

    assert rotated.verify_tenant_chain("tenant-a") == 2
    with rotated.engine.connect() as connection:
        key_ids = (
            connection.execute(select(rotated.events.c.key_id).order_by(rotated.events.c.sequence))
            .scalars()
            .all()
        )
    assert key_ids == ["audit-v1", "audit-v2"]


def test_missing_historical_verification_key_fails_closed() -> None:
    original = audit_log(
        key_id="audit-v1",
        keys={"audit-v1": b"a" * 32},
    )
    original.append(event())
    without_old_key = SQLAlchemyAuditLog(
        engine=original.engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v2",
            verification_keys={"audit-v2": b"b" * 32},
        ),
    )

    with pytest.raises(IntegrityError, match="signature"):
        without_old_key.verify_tenant_chain("tenant-a")


def test_audit_key_material_is_redacted_from_repr() -> None:
    keyring = AuditSigningKeyring(
        active_key_id="audit-v1",
        verification_keys={"audit-v1": b"very-secret-audit-key-material!!!"},
    )
    assert "very-secret-audit-key-material" not in repr(keyring)


def test_caller_owned_transaction_can_roll_back_audit_append() -> None:
    audit = audit_log()

    with audit.engine.connect() as connection:
        transaction = connection.begin()
        audit.append_in_transaction(connection, event())
        assert audit.verify_tenant_chain_in_transaction(connection, "tenant-a") == 1
        transaction.rollback()

    assert audit.verify_tenant_chain("tenant-a") == 0
