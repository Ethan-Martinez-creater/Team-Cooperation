from __future__ import annotations

from dataclasses import dataclass

from ..security.models import DisclosureGrant, Principal, ResourceLabel


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    message_id: str
    idempotency_key: str
    correlation_id: str
    sender: Principal
    recipient: Principal
    purpose: str
    content: str
    label: ResourceLabel
    grant: DisclosureGrant | None = None


@dataclass(frozen=True, slots=True)
class CollaborationEnvelope:
    schema_version: str
    message_id: str
    idempotency_key: str
    correlation_id: str
    sender_tenant_id: str
    sender_principal_id: str
    recipient_tenant_id: str
    purpose: str
    classification: str
    compartments: tuple[str, ...]
    content: str
    content_digest: str
    redaction_findings: tuple[str, ...]
    issued_at: str
    signature: str
    signing_key_id: str | None = None
