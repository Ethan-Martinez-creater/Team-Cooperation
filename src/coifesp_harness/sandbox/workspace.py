from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SandboxWorkspace:
    tenant_id: str
    job_id: str
    path: Path


class SandboxWorkspaceManager:
    """Creates non-shared job directories without following existing links."""

    def __init__(self, *, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("sandbox workspace root must be absolute")
        self.root = root.resolve(strict=False)

    def prepare(self, *, tenant_id: str, job_id: str) -> SandboxWorkspace:
        if not _ID.fullmatch(tenant_id) or not _ID.fullmatch(job_id):
            raise ValueError("sandbox workspace identity is invalid")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        tenant = self.root / tenant_id
        tenant.mkdir(mode=0o700, exist_ok=True)
        path = tenant / job_id
        path.mkdir(mode=0o700, exist_ok=True)
        for candidate in (self.root, tenant, path):
            if candidate.is_symlink() or not candidate.resolve(strict=True).is_relative_to(
                self.root
            ):
                raise ValueError("sandbox workspace contains an unsafe link")
        marker = path / ".coifesp-workspace.json"
        expected = {
            "schema": "coifesp.sandbox-workspace.v1",
            "tenant_id": tenant_id,
            "job_id": job_id,
        }
        if marker.exists():
            if marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise ValueError("sandbox workspace ownership marker is invalid")
        else:
            # Exclusive creation prevents two identities from claiming a path.
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(
                    descriptor,
                    json.dumps(expected, sort_keys=True, separators=(",", ":")).encode(),
                )
            finally:
                os.close(descriptor)
        return SandboxWorkspace(tenant_id, job_id, path)
