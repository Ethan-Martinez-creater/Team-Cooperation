from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from ..security.models import Classification, ResourceLabel


@dataclass(frozen=True, slots=True)
class SkillManifest:
    name: str
    version: str
    description: str
    tenant_id: str
    classification: Classification
    compartments: FrozenSet[str]
    required_tools: FrozenSet[str]
    signer_key_id: str

    @property
    def resource_label(self) -> ResourceLabel:
        return ResourceLabel(
            owner_tenant_id=self.tenant_id,
            classification=self.classification,
            compartments=self.compartments,
            resource_id=f"skill:{self.name}:{self.version}",
        )


@dataclass(frozen=True, slots=True)
class VerifiedSkill:
    manifest: SkillManifest
    instructions: str
    content_digest: str
    package_path: Path

    def render_for_context(self) -> str:
        """Render as signed provenance data, not as a higher-trust system message."""
        header = (
            f'<skill name="{self.manifest.name}" version="{self.manifest.version}" '
            f'digest="sha256:{self.content_digest}" integrity="verified" '
            f'instruction_trust="untrusted">'
        )
        return f"{header}\n{self.instructions}\n</skill>"
