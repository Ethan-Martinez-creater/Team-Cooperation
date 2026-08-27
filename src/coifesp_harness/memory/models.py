from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum

from ..security.models import Principal, ResourceLabel


class MemoryScope(str, Enum):
    SESSION = "session"
    USER_PRIVATE = "user_private"
    TEAM_PROJECT = "team_project"
    ORGANIZATION = "organization"


class MemoryKind(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    PROCEDURE = "procedure"
    TASK_SUMMARY = "task_summary"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


class TrustLevel(IntEnum):
    UNTRUSTED = 0
    LOW = 1
    VERIFIED = 2
    AUTHORITATIVE = 3


class SourceType(str, Enum):
    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    DOCUMENT = "document"
    A2A = "a2a"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class MemorySource:
    source_type: SourceType
    source_id: str
    source_uri: str | None
    trust_level: TrustLevel


@dataclass(frozen=True, slots=True)
class MemoryWriteRequest:
    memory_id: str
    idempotency_key: str
    correlation_id: str
    principal: Principal
    scope: MemoryScope
    kind: MemoryKind
    content: str
    label: ResourceLabel
    source: MemorySource
    owner_principal_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EncryptedMemoryRecord:
    memory_id: str
    tenant_id: str
    scope: MemoryScope
    kind: MemoryKind
    status: MemoryStatus
    label: ResourceLabel
    source: MemorySource
    created_by: str
    owner_principal_id: str | None
    project_id: str | None
    session_id: str | None
    ciphertext: bytes
    nonce: bytes
    content_fingerprint: str
    key_id: str
    created_at: datetime
    expires_at: datetime | None
    version: int = 1


@dataclass(frozen=True, slots=True)
class MemoryView:
    memory_id: str
    scope: MemoryScope
    kind: MemoryKind
    content: str
    label: ResourceLabel
    source: MemorySource
    status: MemoryStatus
    created_at: datetime
    expires_at: datetime | None
    version: int

    def render_for_context(self) -> str:
        return (
            f'<memory id="{self.memory_id}" scope="{self.scope.value}" '
            f'kind="{self.kind.value}" source="{self.source.source_type.value}" '
            f'trust="{self.source.trust_level.name}" instruction_trust="untrusted">\n'
            f"{self.content}\n</memory>"
        )


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    memory_id: str
    status: MemoryStatus
    version: int
    admission_reason: str
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    memory: MemoryView
    lexical_score: float
    semantic_score: float | None
    combined_score: float
