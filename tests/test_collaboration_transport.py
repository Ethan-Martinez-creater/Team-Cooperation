from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.collaboration import (
    DurableCollaborationTransport,
    SignedEnvelopeCodec,
)
from coifesp_harness.collaboration.repository import (
    COLLABORATION_INBOX,
    GOVERNANCE_EVENTS,
    GOVERNANCE_METADATA,
    GOVERNANCE_OUTBOX,
    GOVERNANCE_PROGRAMS,
)
from coifesp_harness.errors import IntegrityError, PolicyDenied
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Principal


def setup_transport(*, max_attempts: int = 3):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1",
            verification_keys={"audit-v1": b"a" * 32},
        ),
    )
    audit.create_schema()
    GOVERNANCE_METADATA.create_all(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(GOVERNANCE_PROGRAMS).values(
                program_id="program-1",
                owner_tenant_id="team-a",
                title="Program",
                objective="Private objective",
                classification=2,
                compartments=["program-1"],
                participant_tenant_ids=["team-a", "team-b"],
                aggregate_version=1,
                last_event_sequence=1,
                created_by="lead-a",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(GOVERNANCE_EVENTS).values(
                program_id="program-1",
                sequence=1,
                event_id="event-1",
                event_type="governance.assignment.proposed",
                actor_id="lead-a",
                actor_tenant_id="team-a",
                subject_id="assignment-1",
                payload={
                    "subject_id": "assignment-1",
                    "deliverable_contract": "API_KEY=must-redact",
                },
                visible_to_tenants=["team-a", "team-b"],
                occurred_at=now,
                audit_event_id="event-1",
            )
        )
        connection.execute(
            insert(GOVERNANCE_OUTBOX).values(
                message_id="message-1",
                program_id="program-1",
                event_sequence=1,
                producer_tenant_id="team-a",
                recipient_tenant_id="team-b",
                status="pending",
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                envelope=None,
                envelope_digest=None,
                last_error_code=None,
                published_at=None,
                created_at=now,
            )
        )
    transport = DurableCollaborationTransport(
        engine=engine,
        audit_log=audit,
        codec=SignedEnvelopeCodec(b"e" * 32),
    )
    relay = Principal(
        "relay-a",
        "team-a",
        roles=frozenset({"collaboration_relay"}),
        is_service=True,
    )
    consumer = Principal(
        "consumer-b",
        "team-b",
        roles=frozenset({"collaboration_consumer"}),
        is_service=True,
    )
    return transport, audit, relay, consumer


def test_outbox_to_inbox_delivery_is_signed_redacted_and_replay_safe() -> None:
    transport, audit, relay, consumer = setup_transport()
    lease = transport.claim_outbound(relay=relay)
    assert lease is not None
    envelope = transport.build_envelope(lease)
    assert "must-redact" not in envelope.content
    assert envelope.recipient_tenant_id == "team-b"

    with pytest.raises(IntegrityError, match="outbox lease"):
        transport.mark_published(
            relay=relay,
            message_id=lease.message_id,
            lease_token="stale-token",
            envelope=envelope,
        )
    transport.mark_published(
        relay=relay,
        message_id=lease.message_id,
        lease_token=lease.lease_token,
        envelope=envelope,
    )
    first = transport.receive(consumer=consumer, envelope=envelope)
    duplicate = transport.receive(consumer=consumer, envelope=envelope)
    assert first.duplicate is False
    assert duplicate.duplicate is True

    inbox_lease = transport.claim_inbound(
        consumer=consumer,
        handler_key="governance-projector-v1",
    )
    assert inbox_lease is not None
    with pytest.raises(IntegrityError, match="inbox lease"):
        transport.complete_inbound(
            consumer=consumer,
            message_id=envelope.message_id,
            lease_token="stale-token",
            result_digest="b" * 64,
        )
    transport.complete_inbound(
        consumer=consumer,
        message_id=envelope.message_id,
        lease_token=inbox_lease.lease_token,
        result_digest="b" * 64,
    )
    with transport.engine.connect() as connection:
        status = connection.execute(select(COLLABORATION_INBOX.c.status)).scalar_one()
    assert status == "processed"
    assert audit.verify_tenant_chain("team-a") == 2
    assert audit.verify_tenant_chain("team-b") == 2


def test_tampering_wrong_service_role_and_dead_letter_are_rejected() -> None:
    transport, _, relay, consumer = setup_transport(max_attempts=1)
    with pytest.raises(PolicyDenied, match="service role"):
        transport.claim_outbound(relay=Principal("user", "team-a"))

    lease = transport.claim_outbound(relay=relay)
    assert lease is not None
    assert (
        transport.fail_outbound(
            relay=relay,
            message_id=lease.message_id,
            lease_token=lease.lease_token,
            error_code="network_failure",
        )
        == "dead_letter"
    )

    valid = transport.codec.issue(
        message_id="message-2",
        idempotency_key="message-2",
        correlation_id="program-1",
        sender_tenant_id="team-a",
        sender_principal_id="lead-a",
        recipient_tenant_id="team-b",
        purpose="governance-event",
        classification="INTERNAL",
        compartments=(),
        content="safe",
    )
    with pytest.raises(IntegrityError):
        transport.receive(
            consumer=consumer,
            envelope=replace(valid, content="tampered"),
        )
