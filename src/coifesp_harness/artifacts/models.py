from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import FrozenSet
from urllib.parse import urlsplit

from ..security import ResourceLabel

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_URI_SCHEMES = frozenset({"artifact", "artifact-store", "git", "https", "s3", "az", "gs"})


class ArtifactKind(str, Enum):
    SOURCE_CODE = "source_code"
    BUILD = "build"
    TEST_REPORT = "test_report"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    PDF = "pdf"
    MESSAGE = "message"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    producer_principal_id: str
    producer_tenant_id: str
    source_tool: str
    source_version: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not all(
            (
                self.producer_principal_id,
                self.producer_tenant_id,
                self.source_tool,
                self.source_version,
            )
        ):
            raise ValueError("complete artifact provenance is required")
        if self.created_at.tzinfo is None:
            raise ValueError("artifact provenance timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Content-addressed handoff contract shared by coding and office workflows."""

    artifact_id: str
    kind: ArtifactKind
    media_type: str
    content_uri: str
    sha256: str
    size_bytes: int
    label: ResourceLabel
    provenance: ArtifactProvenance
    visible_to_tenants: FrozenSet[str]
    metadata_schema: str = "coifesp.artifact.v1"

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.media_type:
            raise ValueError("artifact id and media type are required")
        if self.size_bytes < 0 or not _SHA256.fullmatch(self.sha256):
            raise ValueError("artifact size and lowercase SHA-256 digest are required")
        parsed = urlsplit(self.content_uri)
        if parsed.scheme not in _ALLOWED_URI_SCHEMES:
            raise ValueError("artifact content URI scheme is not allowed")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("artifact content URI must not contain credentials or fragments")
        if self.provenance.producer_tenant_id != self.label.owner_tenant_id:
            raise ValueError("artifact provenance tenant must own the security label")
        if not self.visible_to_tenants or self.label.owner_tenant_id not in self.visible_to_tenants:
            raise ValueError("artifact requires explicit visibility including its owner tenant")

    def is_visible_to(self, tenant_id: str) -> bool:
        return tenant_id in self.visible_to_tenants
