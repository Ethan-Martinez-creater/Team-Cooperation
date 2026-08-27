from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..security.redaction import SecretRedactor

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class ReviewDisclosure(str, Enum):
    VALUE = "value"
    HASH = "hash"
    COUNT = "count"
    REDACTED = "redacted"


@dataclass(frozen=True, slots=True)
class ApprovalReviewField:
    name: str
    json_pointer: str
    disclosure: ReviewDisclosure

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError("approval review field name is invalid")
        if not self.json_pointer.startswith("/") or len(self.json_pointer) > 512:
            raise ValueError("approval review field requires a bounded JSON pointer")


@dataclass(frozen=True, slots=True)
class ApprovalReviewPolicy:
    fields: tuple[ApprovalReviewField, ...]
    required_approver_role: str = "tool_approver"
    expires_in_seconds: int = 900

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.required_approver_role):
            raise ValueError("approval role is invalid")
        if not 60 <= self.expires_in_seconds <= 86_400:
            raise ValueError("approval lifetime must be between 60 and 86400 seconds")
        if not 1 <= len(self.fields) <= 64:
            raise ValueError("approval review policy requires 1 to 64 fields")
        names = {field.name for field in self.fields}
        views = {(field.json_pointer, field.disclosure) for field in self.fields}
        if len(names) != len(self.fields) or len(views) != len(self.fields):
            raise ValueError("approval review fields must be unique")


class ApprovalReviewProjector:
    """Build a deterministic, tool-owned review view from validated arguments."""

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        self.redactor = redactor or SecretRedactor()

    def project(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        policy: ApprovalReviewPolicy,
    ) -> dict[str, Any]:
        fields = []
        for field in policy.fields:
            value = self._resolve(arguments, field.json_pointer)
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            rendered: dict[str, Any] = {
                "name": field.name,
                "json_pointer": field.json_pointer,
                "disclosure": field.disclosure.value,
                "value_digest": digest,
            }
            if field.disclosure is ReviewDisclosure.VALUE:
                redacted = self.redactor.redact(canonical)
                rendered["value"] = (
                    json.loads(redacted.text) if not redacted.findings else redacted.text
                )
                rendered["redaction_findings"] = list(redacted.findings)
            elif field.disclosure is ReviewDisclosure.COUNT:
                if not isinstance(value, (list, dict, str)):
                    raise ValueError(f"review field {field.name} is not countable")
                rendered["count"] = len(value)
            elif field.disclosure is ReviewDisclosure.REDACTED:
                rendered["value"] = "[REDACTED]"
            fields.append(rendered)
        projection = {
            "schema": "coifesp.approval-review.v1",
            "tool_name": tool_name,
            "fields": fields,
        }
        encoded = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 65_536:
            raise ValueError("approval review projection exceeds its size limit")
        return projection

    @staticmethod
    def _resolve(value: Any, pointer: str) -> Any:
        current = value
        parts = pointer.split("/")[1:]
        if len(parts) > 32:
            raise ValueError("approval review JSON pointer is too deep")
        for raw_part in parts:
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                raise ValueError("approval review JSON pointer does not resolve")
        return current
