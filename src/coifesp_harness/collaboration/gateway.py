from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from ..audit import AuditEvent, AuditSink
from ..errors import (
    DuplicateRequest,
    IdempotencyConflict,
    IntegrityError,
    PolicyDenied,
)
from ..idempotency import ClaimStatus, IdempotencyStore
from ..config import ConfigurationError, Settings
from ..key_material import configured_keys
from ..security.policy import DecisionEffect, PolicyEngine
from ..security.redaction import SecretRedactor
from .models import CollaborationEnvelope, OutboundMessage


class SignedEnvelopeCodec:
    """Canonical HMAC envelope codec with freshness and structural validation."""

    def __init__(
        self,
        signing_key: bytes | None = None,
        *,
        active_key_id: str | None = None,
        verification_keys: dict[str, bytes] | None = None,
        legacy_v1_key_id: str | None = None,
        max_age: timedelta = timedelta(days=7),
        future_skew: timedelta = timedelta(minutes=5),
        max_content_bytes: int = 262_144,
    ) -> None:
        if verification_keys is None:
            if signing_key is None or len(signing_key) < 32:
                raise ValueError("envelope signing key must contain at least 32 bytes")
            self.active_key_id = None
            self._keys = {"legacy-v1": bytes(signing_key)}
            self.legacy_v1_key_id = "legacy-v1"
        else:
            copied = {name: bytes(value) for name, value in verification_keys.items()}
            if (
                not active_key_id
                or active_key_id not in copied
                or any(len(value) < 32 for value in copied.values())
            ):
                raise ValueError("envelope keyring is invalid")
            if legacy_v1_key_id is not None and legacy_v1_key_id not in copied:
                raise ValueError("legacy v1 envelope key is unavailable")
            self.active_key_id = active_key_id
            self._keys = copied
            self.legacy_v1_key_id = legacy_v1_key_id
        if max_age <= timedelta(0) or future_skew < timedelta(0) or max_content_bytes <= 0:
            raise ValueError("envelope validation limits are invalid")
        self.max_age = max_age
        self.future_skew = future_skew
        self.max_content_bytes = max_content_bytes

    @classmethod
    def from_settings(cls, settings: Settings, **limits) -> "SignedEnvelopeCodec":
        if not settings.envelope_keys:
            if settings.envelope_signing_key is None:
                raise ConfigurationError("COIFESP_ENVELOPE_SIGNING_KEY is required")
            return cls(settings.envelope_signing_key.reveal().encode("utf-8"), **limits)
        active, keys = configured_keys(
            active_key_id=settings.envelope_key_id,
            versioned=settings.envelope_keys,
            legacy=settings.envelope_signing_key,
            legacy_name="COIFESP_ENVELOPE_SIGNING_KEY",
        )
        return cls(
            active_key_id=active,
            verification_keys=keys,
            legacy_v1_key_id=settings.envelope_legacy_v1_key_id,
            **limits,
        )

    def issue(
        self,
        *,
        message_id: str,
        idempotency_key: str,
        correlation_id: str,
        sender_tenant_id: str,
        sender_principal_id: str,
        recipient_tenant_id: str,
        purpose: str,
        classification: str,
        compartments: tuple[str, ...],
        content: str,
        redaction_findings: tuple[str, ...] = (),
        issued_at: datetime | None = None,
    ) -> CollaborationEnvelope:
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        unsigned = {
            "schema_version": (
                "coifesp.collaboration.v2" if self.active_key_id else "coifesp.collaboration.v1"
            ),
            "message_id": message_id,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "sender_tenant_id": sender_tenant_id,
            "sender_principal_id": sender_principal_id,
            "recipient_tenant_id": recipient_tenant_id,
            "purpose": purpose,
            "classification": classification,
            "compartments": compartments,
            "content": content,
            "content_digest": content_digest,
            "redaction_findings": redaction_findings,
            "issued_at": (issued_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        }
        if self.active_key_id:
            unsigned["signing_key_id"] = self.active_key_id
        key_id = self.active_key_id or self.legacy_v1_key_id
        assert key_id is not None
        envelope = CollaborationEnvelope(**unsigned, signature=self._sign(unsigned, key_id))
        self.verify(envelope)
        return envelope

    def verify(
        self,
        envelope: CollaborationEnvelope,
        *,
        now: datetime | None = None,
        expected_recipient_tenant_id: str | None = None,
    ) -> None:
        if envelope.schema_version not in {"coifesp.collaboration.v1", "coifesp.collaboration.v2"}:
            raise IntegrityError("collaboration envelope schema is unsupported")
        if envelope.schema_version == "coifesp.collaboration.v2":
            key_id = envelope.signing_key_id
            if not key_id or key_id not in self._keys:
                raise IntegrityError("collaboration envelope signing key is unavailable")
        else:
            if envelope.signing_key_id is not None:
                raise IntegrityError("legacy collaboration envelope contains a key id")
            key_id = self.legacy_v1_key_id
            if key_id is None:
                raise IntegrityError("legacy collaboration envelope verification is disabled")
        identifiers = (
            envelope.message_id,
            envelope.idempotency_key,
            envelope.correlation_id,
            envelope.sender_tenant_id,
            envelope.sender_principal_id,
            envelope.recipient_tenant_id,
            envelope.purpose,
        )
        if any(
            not value or len(value) > 128 or any(ord(character) < 32 for character in value)
            for value in identifiers
        ):
            raise IntegrityError("collaboration envelope identifier is invalid")
        if expected_recipient_tenant_id is not None and not hmac.compare_digest(
            envelope.recipient_tenant_id, expected_recipient_tenant_id
        ):
            raise IntegrityError("collaboration envelope recipient is invalid")
        if envelope.classification not in {
            "PUBLIC",
            "INTERNAL",
            "CONFIDENTIAL",
            "RESTRICTED",
        }:
            raise IntegrityError("collaboration envelope classification is invalid")
        if len(envelope.compartments) != len(set(envelope.compartments)):
            raise IntegrityError("collaboration envelope compartments are invalid")
        if len(envelope.content.encode("utf-8")) > self.max_content_bytes:
            raise IntegrityError("collaboration envelope content exceeds the size limit")
        try:
            issued_at = datetime.fromisoformat(envelope.issued_at)
        except ValueError as exc:
            raise IntegrityError("collaboration envelope issued_at is invalid") from exc
        if issued_at.tzinfo is None:
            raise IntegrityError("collaboration envelope issued_at must be timezone-aware")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        issued_at = issued_at.astimezone(UTC)
        if issued_at > current + self.future_skew:
            raise IntegrityError("collaboration envelope was issued in the future")
        if issued_at < current - self.max_age:
            raise IntegrityError("collaboration envelope has expired")
        values = asdict(envelope)
        signature = values.pop("signature")
        if envelope.schema_version == "coifesp.collaboration.v1":
            values.pop("signing_key_id")
        if not hmac.compare_digest(signature, self._sign(values, key_id)):
            raise IntegrityError("collaboration envelope signature is invalid")
        digest = hashlib.sha256(envelope.content.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, envelope.content_digest):
            raise IntegrityError("collaboration envelope content digest is invalid")

    def _sign(self, value: dict[str, object], key_id: str) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._keys[key_id], canonical, hashlib.sha256).hexdigest()


class SecureCollaborationGateway:
    """Policy-controlled and signed boundary for cross-team information exchange."""

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        audit: AuditSink,
        signing_key: bytes,
        idempotency: IdempotencyStore,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.policy = policy
        self.audit = audit
        self.codec = SignedEnvelopeCodec(signing_key)
        self.idempotency = idempotency
        self.redactor = redactor or SecretRedactor()

    def publish(self, message: OutboundMessage) -> CollaborationEnvelope:
        decision = self.policy.decide_disclosure(
            sender=message.sender,
            recipient=message.recipient,
            resource=message.label,
            purpose=message.purpose,
            grant=message.grant,
        )
        if decision.effect is not DecisionEffect.PERMIT:
            self._audit(message, decision.effect.value, decision.reason)
            raise PolicyDenied(decision.reason)

        request_digest = self._request_digest(message)
        claim = self.idempotency.claim(
            namespace="collaboration.publish",
            tenant_id=message.sender.tenant_id,
            idempotency_key=message.idempotency_key,
            request_digest=request_digest,
        )
        if claim is ClaimStatus.DUPLICATE:
            self._audit(message, "duplicate_suppressed", "same request was already published")
            raise DuplicateRequest("collaboration request was already published")
        if claim is ClaimStatus.CONFLICT:
            self._audit(
                message,
                "idempotency_conflict",
                "idempotency key was reused with different content",
            )
            raise IdempotencyConflict(
                "collaboration idempotency key conflicts with an earlier request"
            )

        redacted = self.redactor.redact(message.content)
        envelope = self.codec.issue(
            message_id=message.message_id,
            idempotency_key=message.idempotency_key,
            correlation_id=message.correlation_id,
            sender_tenant_id=message.sender.tenant_id,
            sender_principal_id=message.sender.principal_id,
            recipient_tenant_id=message.recipient.tenant_id,
            purpose=message.purpose,
            classification=message.label.classification.name,
            compartments=tuple(sorted(message.label.compartments)),
            content=redacted.text,
            redaction_findings=redacted.findings,
        )
        self._audit(
            message,
            "published",
            "disclosure policy passed",
            content_digest=envelope.content_digest,
            redaction_findings=redacted.findings,
        )
        return envelope

    def verify(self, envelope: CollaborationEnvelope) -> None:
        self.codec.verify(envelope)

    @staticmethod
    def _request_digest(message: OutboundMessage) -> str:
        canonical = json.dumps(
            {
                "message_id": message.message_id,
                "sender_tenant_id": message.sender.tenant_id,
                "recipient_tenant_id": message.recipient.tenant_id,
                "purpose": message.purpose,
                "resource_id": message.label.resource_id,
                "classification": message.label.classification.name,
                "compartments": sorted(message.label.compartments),
                "content": message.content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _audit(
        self,
        message: OutboundMessage,
        outcome: str,
        reason: str,
        **extra: object,
    ) -> None:
        self.audit.append(
            AuditEvent(
                tenant_id=message.sender.tenant_id,
                event_type="collaboration.publish",
                actor_id=message.sender.principal_id,
                outcome=outcome,
                details={
                    "message_id": message.message_id,
                    "recipient_tenant_id": message.recipient.tenant_id,
                    "resource_id": message.label.resource_id,
                    "classification": message.label.classification.name,
                    "purpose": message.purpose,
                    "reason": reason,
                    **extra,
                },
                correlation_id=message.correlation_id,
            )
        )
