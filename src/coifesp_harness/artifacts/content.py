from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Iterator

from ..errors import IntegrityError, PolicyDenied
from ..security import Principal
from .models import ArtifactManifest
from .repository import SQLAlchemyArtifactRepository
from .storage import ArtifactObjectStore


class ArtifactContentService:
    """Composes immutable bytes with the existing authorized manifest registry."""

    def __init__(self, repository: SQLAlchemyArtifactRepository, store: ArtifactObjectStore) -> None:
        self.repository, self.store = repository, store

    def publish(self, *, principal: Principal, idempotency_key: str,
                manifest: ArtifactManifest, chunks: Iterable[bytes]):
        if principal.is_service or "artifact_publisher" not in principal.roles:
            raise PolicyDenied("artifact publisher role is required")
        owner = manifest.label.owner_tenant_id
        stored = self.store.put(
            tenant_id=owner, chunks=chunks, expected_sha256=manifest.sha256,
            expected_size=manifest.size_bytes, idempotency_key=idempotency_key,
        )
        bound = replace(manifest, content_uri=stored.storage_uri)
        return self.repository.publish(
            principal=principal, idempotency_key=idempotency_key, manifest=bound
        )

    def open(self, *, principal: Principal, owner_tenant_id: str, artifact_id: str,
             expected_sha256: str, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        manifest = self.repository.read(
            principal=principal, owner_tenant_id=owner_tenant_id,
            artifact_id=artifact_id, expected_sha256=expected_sha256,
        )
        stored = self.store.stat(tenant_id=owner_tenant_id, sha256=manifest.sha256)
        if stored.size_bytes != manifest.size_bytes:
            raise IntegrityError("artifact object size differs from its manifest")
        return self.store.open(
            tenant_id=owner_tenant_id, sha256=manifest.sha256, offset=offset, length=length
        )

    def open_policy_authorized(self, *, owner_tenant_id: str, sha256: str,
                               expected_size: int | None = None) -> Iterator[bytes]:
        """Open bytes after an independent product policy has authorized the request."""
        stored = self.store.stat(tenant_id=owner_tenant_id, sha256=sha256)
        if expected_size is not None and stored.size_bytes != expected_size:
            raise IntegrityError("artifact object size differs from the bound project resource")
        return self.store.open(tenant_id=owner_tenant_id, sha256=sha256)
