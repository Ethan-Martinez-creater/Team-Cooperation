from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, IntEnum
from typing import TYPE_CHECKING

from ..security.models import DisclosureGrant, ResourceLabel

if TYPE_CHECKING:
    from ..runtime.models import Message

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ContextSource(str, Enum):
    SYSTEM = "system"
    USER = "user"
    MEMORY = "memory"
    TOOL = "tool"
    DOCUMENT = "document"
    A2A = "a2a"
    GOVERNANCE = "governance"
    SKILL = "skill"


class ContentTrust(IntEnum):
    UNTRUSTED = 0
    LOW = 1
    VERIFIED = 2
    AUTHORITATIVE = 3


class InstructionTrust(str, Enum):
    DATA_ONLY = "data_only"
    USER_INSTRUCTION = "user_instruction"
    SYSTEM_INSTRUCTION = "system_instruction"


@dataclass(frozen=True, slots=True)
class ContextItem:
    item_id: str
    content: str
    source: ContextSource
    source_id: str
    label: ResourceLabel
    content_trust: ContentTrust = ContentTrust.UNTRUSTED
    instruction_trust: InstructionTrust = InstructionTrust.DATA_ONLY
    priority: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    disclosure_grant: DisclosureGrant | None = None
    image_media_type: str | None = None
    image_data_base64: str | None = None
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (("item_id", self.item_id), ("source_id", self.source_id)):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        if not self.content or len(self.content.encode("utf-8")) > 1_000_000:
            raise ValueError("context content must contain 1 to 1,000,000 UTF-8 bytes")
        if (self.image_media_type is None) != (self.image_data_base64 is None):
            raise ValueError("context image media type and data must be provided together")
        image_bytes = b""
        if self.image_media_type is not None:
            if self.image_media_type not in {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            }:
                raise ValueError("context image media type is unsupported")
            try:
                image_bytes = base64.b64decode(self.image_data_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("context image data is invalid") from exc
            if not image_bytes or len(image_bytes) > 5_000_000:
                raise ValueError("context image must contain 1 to 5,000,000 bytes")
        if not -1000 <= self.priority <= 1000:
            raise ValueError("context priority must be between -1000 and 1000")
        if self.created_at.tzinfo is None:
            raise ValueError("context created_at must be timezone-aware")
        if (
            self.instruction_trust is InstructionTrust.SYSTEM_INSTRUCTION
            and self.source is not ContextSource.SYSTEM
        ):
            raise ValueError("only a system source may carry system instructions")
        if (
            self.instruction_trust is InstructionTrust.USER_INSTRUCTION
            and self.source is not ContextSource.USER
        ):
            raise ValueError("only a user source may carry user instructions")
        digest = hashlib.sha256(self.content.encode("utf-8"))
        if self.image_media_type is not None:
            digest.update(b"\x00image\x00")
            digest.update(self.image_media_type.encode("ascii"))
            digest.update(b"\x00")
            digest.update(image_bytes)
        object.__setattr__(self, "content_digest", digest.hexdigest())


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_input_tokens: int = 32_000
    reserved_output_tokens: int = 4_096
    max_item_tokens: int = 8_000
    max_items: int = 256

    def __post_init__(self) -> None:
        if (
            min(
                self.max_input_tokens,
                self.reserved_output_tokens,
                self.max_item_tokens,
                self.max_items,
            )
            <= 0
        ):
            raise ValueError("context budgets must be positive")
        if self.reserved_output_tokens >= self.max_input_tokens:
            raise ValueError("reserved output must be smaller than the input window")

    @property
    def usable_input_tokens(self) -> int:
        return self.max_input_tokens - self.reserved_output_tokens


@dataclass(frozen=True, slots=True)
class ExcludedContext:
    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextManifestEntry:
    item_id: str
    source: ContextSource
    source_id: str
    owner_tenant_id: str
    digest: str
    content_trust: ContentTrust
    instruction_trust: InstructionTrust
    compacted: bool


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    messages: tuple[Message, ...]
    manifest: tuple[ContextManifestEntry, ...]
    excluded: tuple[ExcludedContext, ...]
    estimated_input_tokens: int
    compacted_item_ids: tuple[str, ...]
