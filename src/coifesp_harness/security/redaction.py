from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    findings: tuple[str, ...]


class SecretRedactor:
    """Conservative last-line defense for accidental secret propagation.

    Redaction never changes a resource's classification and is not a declassifier.
    """

    _patterns = (
        ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
        (
            "credential_assignment",
            re.compile(
                r"(?i)\b(api[_-]?key|secret|password|access[_-]?token)\b"
                r"\s*[:=]\s*[\"']?[^\s,\"']{8,}"
            ),
        ),
    )

    def redact(self, text: str) -> RedactionResult:
        value = text
        findings: list[str] = []
        for name, pattern in self._patterns:
            if pattern.search(value):
                findings.append(name)
                value = pattern.sub(f"[REDACTED:{name}]", value)
        return RedactionResult(value, tuple(findings))
