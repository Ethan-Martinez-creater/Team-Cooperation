"""Deterministic artifact composition; inputs must be loaded by the trusted service.

No model output is executed and no filesystem paths are opened. The caller must
authorize the project resources and pin their current verification evidence.
This assembler rechecks the actual bytes and creates a reproducible deliverable,
not a claim that integration or business acceptance has passed.
"""

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BundleArtifact:
    resource_id: str
    version: str
    filename: str
    media_type: str
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class DeliveryBundle:
    content: bytes
    sha256: str
    manifest: bytes


def assemble_delivery_bundle(artifacts, *, max_artifacts=256, max_total_bytes=64 * 1024 * 1024):
    """Create a ZIP of byte-verified artifacts plus a canonical JSON manifest.

    Archive paths use ordinal names rather than untrusted filenames/resource
    identifiers. Original names are descriptive JSON values only. Sorting by
    resource ID, fixed ZIP metadata and uncompressed entries give identical
    bytes across retries without a platform-specific compression dependency.
    """
    if type(max_artifacts) is not int or not 1 <= max_artifacts <= 10_000:
        raise ValueError("invalid bundle artifact limit")
    if type(max_total_bytes) is not int or not 1 <= max_total_bytes <= 1024 * 1024 * 1024:
        raise ValueError("invalid bundle byte limit")
    # Bound consumption even if an iterator is supplied, before writing bytes.
    selected, total, seen = [], 0, set()
    for artifact in artifacts:
        if len(selected) >= max_artifacts:
            raise ValueError("bundle artifact count exceeded")
        if not isinstance(artifact, BundleArtifact):
            raise TypeError("bundle inputs must be BundleArtifact values")
        for name, limit in (("resource_id", 128), ("version", 128),
                            ("filename", 1024), ("media_type", 256)):
            value = getattr(artifact, name)
            if type(value) is not str or not value.strip() or len(value) > limit:
                raise ValueError(f"invalid artifact {name}")
        if artifact.resource_id in seen:
            raise ValueError("duplicate bundle resource")
        if type(artifact.sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256):
            raise ValueError("invalid artifact digest")
        if type(artifact.content) is not bytes:
            raise TypeError("artifact content must be immutable bytes")
        total += len(artifact.content)
        if total > max_total_bytes:
            raise ValueError("bundle byte limit exceeded")
        if hashlib.sha256(artifact.content).hexdigest() != artifact.sha256:
            raise ValueError("artifact integrity mismatch")
        seen.add(artifact.resource_id)
        selected.append(artifact)
    if not selected:
        raise ValueError("an empty artifact set is not a deliverable")
    selected.sort(key=lambda item: item.resource_id)
    entries, metadata = [], []
    for index, artifact in enumerate(selected, 1):
        path = f"artifacts/{index:06d}"
        entries.append((path, artifact.content))
        metadata.append({"path": path, "resource_id": artifact.resource_id,
                         "version": artifact.version, "filename": artifact.filename,
                         "media_type": artifact.media_type, "sha256": artifact.sha256,
                         "size_bytes": len(artifact.content)})
    manifest = json.dumps({"schema": "coifesp.delivery-bundle.v1", "artifacts": metadata},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for path, content in [("manifest.json", manifest), *entries]:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    content = stream.getvalue()
    return DeliveryBundle(content, hashlib.sha256(content).hexdigest(), manifest)
