from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..security.redaction import SecretRedactor
from .models import MemoryScope, MemoryWriteRequest, SourceType, TrustLevel


class AdmissionEffect(str, Enum):
    ACCEPT = "accept"
    QUARANTINE = "quarantine"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    effect: AdmissionEffect
    reason: str
    indicators: tuple[str, ...] = ()


class MemoryAdmissionPolicy:
    MAX_CONTENT_BYTES = 32_768
    _injection_patterns = (
        (
            "instruction_override",
            re.compile(
                r"(?i)\b(ignore|disregard|override)\b.{0,40}"
                r"\b(previous|prior|system|developer)\b.{0,20}\b(instruction|prompt)s?\b"
            ),
        ),
        (
            "system_prompt_request",
            re.compile(r"(?i)\b(reveal|print|repeat|exfiltrate)\b.{0,40}\bsystem prompt\b"),
        ),
        (
            "tool_coercion",
            re.compile(
                r"(?i)\b(call|execute|invoke|run)\b.{0,30}"
                r"\b(tool|shell|command)\b.{0,30}\b(secret|credential|token|password)\b"
            ),
        ),
    )

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        self.redactor = redactor or SecretRedactor()

    def assess(self, request: MemoryWriteRequest) -> AdmissionDecision:
        encoded_size = len(request.content.encode("utf-8"))
        if encoded_size == 0:
            return AdmissionDecision(AdmissionEffect.DENY, "memory content is empty")
        if encoded_size > self.MAX_CONTENT_BYTES:
            return AdmissionDecision(
                AdmissionEffect.DENY,
                "memory content exceeds the admission size limit",
            )
        secret_findings = self.redactor.redact(request.content).findings
        if secret_findings:
            return AdmissionDecision(
                AdmissionEffect.DENY,
                "memory content contains a possible secret",
                secret_findings,
            )
        injection_findings = tuple(
            name for name, pattern in self._injection_patterns if pattern.search(request.content)
        )
        if injection_findings:
            return AdmissionDecision(
                AdmissionEffect.QUARANTINE,
                "memory content contains instruction-like attack indicators",
                injection_findings,
            )
        shared_scope = request.scope in {
            MemoryScope.TEAM_PROJECT,
            MemoryScope.ORGANIZATION,
        }
        if shared_scope and request.source.trust_level < TrustLevel.VERIFIED:
            return AdmissionDecision(
                AdmissionEffect.QUARANTINE,
                "low-trust content cannot enter shared memory without review",
            )
        external_source = request.source.source_type in {
            SourceType.TOOL,
            SourceType.DOCUMENT,
            SourceType.A2A,
        }
        if external_source and request.source.trust_level is TrustLevel.UNTRUSTED:
            return AdmissionDecision(
                AdmissionEffect.QUARANTINE,
                "untrusted external content requires review",
            )
        return AdmissionDecision(AdmissionEffect.ACCEPT, "admission checks passed")
