from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from ..errors import IntegrityError, ResourceNotFound

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StoredObject:
    tenant_id: str
    sha256: str
    size_bytes: int
    storage_uri: str


class ArtifactObjectStore(Protocol):
    def put(
        self, *, tenant_id: str, chunks: Iterable[bytes], expected_sha256: str,
        expected_size: int, idempotency_key: str,
    ) -> StoredObject: ...

    def open(
        self, *, tenant_id: str, sha256: str, offset: int = 0,
        length: int | None = None,
    ) -> Iterator[bytes]: ...

    def stat(self, *, tenant_id: str, sha256: str) -> StoredObject: ...


class LocalImmutableArtifactStore:
    """Content-addressed local backend; production object stores implement the same protocol."""

    def __init__(self, root: Path, *, max_object_bytes: int = 2_147_483_648, chunk_bytes: int = 1_048_576) -> None:
        if max_object_bytes <= 0 or not 4096 <= chunk_bytes <= 8_388_608:
            raise ValueError("artifact store limits are invalid")
        self._root = root.resolve(strict=True)
        if not self._root.is_dir() or self._root.is_symlink():
            raise ValueError("artifact store root must be an existing real directory")
        self._maximum, self._chunk = max_object_bytes, chunk_bytes

    def _path(self, tenant_id: str, digest: str) -> Path:
        if _ID.fullmatch(tenant_id) is None or _DIGEST.fullmatch(digest) is None:
            raise ValueError("artifact storage identity is invalid")
        candidate = self._root / tenant_id / digest[:2] / digest
        if self._root not in candidate.parents:
            raise ValueError("artifact storage path escapes its root")
        return candidate

    def put(self, *, tenant_id: str, chunks: Iterable[bytes], expected_sha256: str,
            expected_size: int, idempotency_key: str) -> StoredObject:
        if _ID.fullmatch(idempotency_key) is None or expected_size < 0 or expected_size > self._maximum:
            raise ValueError("artifact upload bounds or idempotency key are invalid")
        target = self._path(tenant_id, expected_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve(strict=True)
        if self._root not in resolved_parent.parents or any(
            component.is_symlink()
            for component in (self._root / tenant_id, self._root / tenant_id / expected_sha256[:2])
        ):
            raise IntegrityError("artifact storage directory is not trusted")
        target = resolved_parent / expected_sha256
        if target.exists():
            return self._verify_existing(target, tenant_id, expected_sha256, expected_size)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
        temporary = Path(temporary_name)
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes) or not chunk:
                        raise ValueError("artifact chunks must be non-empty bytes")
                    size += len(chunk)
                    if size > expected_size or size > self._maximum:
                        raise IntegrityError("artifact content exceeds its declared size")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise IntegrityError("artifact content identity does not match its declaration")
            try:
                os.link(temporary, target)
            except FileExistsError:
                return self._verify_existing(target, tenant_id, expected_sha256, expected_size)
            return StoredObject(tenant_id, expected_sha256, size, f"artifact-store://{tenant_id}/{expected_sha256}")
        finally:
            if temporary.exists():
                temporary.unlink()

    def _verify_existing(self, path: Path, tenant_id: str, digest: str, size: int) -> StoredObject:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise IntegrityError("existing artifact object identity is invalid")
        actual = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(self._chunk), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise IntegrityError("existing artifact object digest is invalid")
        return StoredObject(tenant_id, digest, size, f"artifact-store://{tenant_id}/{digest}")

    def stat(self, *, tenant_id: str, sha256: str) -> StoredObject:
        path = self._path(tenant_id, sha256)
        if not path.exists() or path.is_symlink() or not path.is_file():
            raise ResourceNotFound("artifact content is unavailable")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ResourceNotFound("artifact content is unavailable") from exc
        if self._root not in resolved.parents or resolved != path:
            raise IntegrityError("artifact storage path is not trusted")
        return StoredObject(tenant_id, sha256, path.stat().st_size, f"artifact-store://{tenant_id}/{sha256}")

    def open(self, *, tenant_id: str, sha256: str, offset: int = 0,
             length: int | None = None) -> Iterator[bytes]:
        value = self.stat(tenant_id=tenant_id, sha256=sha256)
        if type(offset) is not int or offset < 0 or offset > value.size_bytes:
            raise ValueError("artifact range offset is invalid")
        if length is not None and (type(length) is not int or length < 0 or offset + length > value.size_bytes):
            raise ValueError("artifact range length is invalid")
        remaining = value.size_bytes - offset if length is None else length
        path = self._path(tenant_id, sha256)
        with path.open("rb") as stream:
            actual = hashlib.sha256()
            for chunk in iter(lambda: stream.read(self._chunk), b""):
                actual.update(chunk)
            if actual.hexdigest() != sha256:
                raise IntegrityError("artifact content digest is invalid during read")
            stream.seek(offset)
            while remaining:
                chunk = stream.read(min(self._chunk, remaining))
                if not chunk:
                    raise IntegrityError("artifact content was truncated during read")
                remaining -= len(chunk)
                yield chunk
