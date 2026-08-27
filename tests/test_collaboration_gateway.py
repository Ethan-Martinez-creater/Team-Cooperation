from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.errors import IntegrityError, PolicyDenied
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.collaboration import (
    OutboundMessage,
    SecureCollaborationGateway,
    SignedEnvelopeCodec,
)
from coifesp_harness.security import (
    Classification,
    DisclosureGrant,
    PolicyEngine,
    Principal,
    ResourceLabel,
)


def actors_and_label():
    sender = Principal(
        "alice",
        "team-a",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    recipient = Principal(
        "bob",
        "team-b",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    label = ResourceLabel(
        "team-a",
        Classification.CONFIDENTIAL,
        frozenset({"project-x"}),
        "artifact-1",
    )
    return sender, recipient, label


def test_cross_team_message_is_gated_redacted_signed_and_verifiable() -> None:
    sender, recipient, label = actors_and_label()
    gateway = SecureCollaborationGateway(
        policy=PolicyEngine(),
        audit=InMemoryAuditSink(),
        signing_key=b"a" * 32,
        idempotency=InMemoryIdempotencyStore(),
    )
    base = OutboundMessage(
        message_id="message-1",
        idempotency_key="idem-1",
        correlation_id="corr-1",
        sender=sender,
        recipient=recipient,
        purpose="integration",
        content="API_KEY=super-secret-value",
        label=label,
    )
    with pytest.raises(PolicyDenied):
        gateway.publish(base)

    grant = DisclosureGrant(
        grant_id="grant-1",
        owner_tenant_id="team-a",
        recipient_tenant_id="team-b",
        resource_id="artifact-1",
        purpose="integration",
        approved_by="security-owner",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        max_classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    envelope = gateway.publish(replace(base, grant=grant))
    assert "super-secret-value" not in envelope.content
    assert envelope.redaction_findings == ("credential_assignment",)
    gateway.verify(envelope)

    with pytest.raises(IntegrityError):
        gateway.verify(replace(envelope, content=envelope.content + "tampered"))


def test_envelope_codec_rejects_wrong_recipient_future_and_expired_messages() -> None:
    codec = SignedEnvelopeCodec(b"a" * 32, max_age=timedelta(hours=1))
    now = datetime.now(UTC)
    envelope = codec.issue(
        message_id="message-1",
        idempotency_key="idem-1",
        correlation_id="corr-1",
        sender_tenant_id="team-a",
        sender_principal_id="alice",
        recipient_tenant_id="team-b",
        purpose="governance-event",
        classification="CONFIDENTIAL",
        compartments=("program-x",),
        content='{"event":"approved"}',
        issued_at=now,
    )
    codec.verify(envelope, now=now, expected_recipient_tenant_id="team-b")
    with pytest.raises(IntegrityError, match="recipient"):
        codec.verify(envelope, now=now, expected_recipient_tenant_id="team-c")
    with pytest.raises(IntegrityError, match="expired"):
        codec.verify(envelope, now=now + timedelta(hours=2))

    permissive_issuer = SignedEnvelopeCodec(
        b"a" * 32,
        max_age=timedelta(hours=1),
        future_skew=timedelta(minutes=20),
    )
    future = permissive_issuer.issue(
        message_id="message-2",
        idempotency_key="idem-2",
        correlation_id="corr-2",
        sender_tenant_id="team-a",
        sender_principal_id="alice",
        recipient_tenant_id="team-b",
        purpose="governance-event",
        classification="INTERNAL",
        compartments=(),
        content="future",
        issued_at=now + timedelta(minutes=10),
    )
    with pytest.raises(IntegrityError, match="future"):
        codec.verify(future, now=now)


def test_versioned_envelope_rotation_writes_v2_and_verifies_legacy_v1() -> None:
    old = SignedEnvelopeCodec(b"o" * 32)
    legacy = old.issue(
        message_id="legacy-message",
        idempotency_key="legacy-idem",
        correlation_id="legacy-corr",
        sender_tenant_id="team-a",
        sender_principal_id="alice",
        recipient_tenant_id="team-b",
        purpose="integration",
        classification="INTERNAL",
        compartments=(),
        content="legacy",
    )
    rotated = SignedEnvelopeCodec(
        active_key_id="envelope-v2",
        verification_keys={"envelope-v1": b"o" * 32, "envelope-v2": b"n" * 32},
        legacy_v1_key_id="envelope-v1",
    )
    rotated.verify(legacy)
    current = rotated.issue(
        message_id="current-message",
        idempotency_key="current-idem",
        correlation_id="current-corr",
        sender_tenant_id="team-a",
        sender_principal_id="alice",
        recipient_tenant_id="team-b",
        purpose="integration",
        classification="INTERNAL",
        compartments=(),
        content="current",
    )
    assert current.schema_version == "coifesp.collaboration.v2"
    assert current.signing_key_id == "envelope-v2"
    rotated.verify(current)
    with pytest.raises(IntegrityError, match="verification is disabled"):
        SignedEnvelopeCodec(
            active_key_id="envelope-v2",
            verification_keys={"envelope-v2": b"n" * 32},
        ).verify(legacy)
