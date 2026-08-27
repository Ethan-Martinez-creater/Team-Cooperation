import hashlib
from datetime import UTC, datetime

import pytest

from coifesp_harness.artifacts import (
    ArtifactContentService, ArtifactKind, ArtifactManifest, ArtifactProvenance,
    LocalImmutableArtifactStore,
)
from coifesp_harness.errors import IntegrityError, ResourceNotFound
from coifesp_harness.security import Classification, Principal, ResourceLabel


def test_local_store_streams_verifies_ranges_and_is_idempotent(tmp_path):
    payload = b"production artifact bytes" * 1000
    digest = hashlib.sha256(payload).hexdigest()
    store = LocalImmutableArtifactStore(tmp_path, chunk_bytes=4096)
    chunks = (payload[index:index + 137] for index in range(0, len(payload), 137))
    first = store.put(tenant_id="team-a", chunks=chunks, expected_sha256=digest,
                      expected_size=len(payload), idempotency_key="upload-1")
    second = store.put(tenant_id="team-a", chunks=(payload,), expected_sha256=digest,
                       expected_size=len(payload), idempotency_key="upload-1")
    assert first == second
    assert b"".join(store.open(tenant_id="team-a", sha256=digest)) == payload
    assert b"".join(store.open(tenant_id="team-a", sha256=digest, offset=11, length=29)) == payload[11:40]
    with pytest.raises(ResourceNotFound):
        store.stat(tenant_id="team-b", sha256=digest)


def test_local_store_rejects_digest_size_and_path_attacks(tmp_path):
    store = LocalImmutableArtifactStore(tmp_path)
    with pytest.raises(IntegrityError, match="identity"):
        store.put(tenant_id="team-a", chunks=(b"wrong",), expected_sha256="0" * 64,
                  expected_size=5, idempotency_key="upload-1")
    with pytest.raises(IntegrityError, match="size"):
        store.put(tenant_id="team-a", chunks=(b"too-long",),
                  expected_sha256=hashlib.sha256(b"too-long").hexdigest(),
                  expected_size=2, idempotency_key="upload-2")
    with pytest.raises(ValueError, match="identity"):
        store.stat(tenant_id="../escape", sha256="0" * 64)


def test_existing_object_tamper_is_detected(tmp_path):
    payload = b"original"; digest = hashlib.sha256(payload).hexdigest()
    store = LocalImmutableArtifactStore(tmp_path)
    store.put(tenant_id="team-a", chunks=(payload,), expected_sha256=digest,
              expected_size=len(payload), idempotency_key="upload-1")
    path = tmp_path / "team-a" / digest[:2] / digest
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store.put(tenant_id="team-a", chunks=(payload,), expected_sha256=digest,
                  expected_size=len(payload), idempotency_key="upload-1")
    with pytest.raises(IntegrityError, match="during read"):
        b"".join(store.open(tenant_id="team-a", sha256=digest))


def test_content_service_binds_store_uri_and_authorized_manifest():
    payload = b"report"; digest = hashlib.sha256(payload).hexdigest()
    manifest = ArtifactManifest(
        "report-1", ArtifactKind.DOCUMENT, "text/plain", "artifact://team-a/pending",
        digest, len(payload), ResourceLabel("team-a", Classification.CONFIDENTIAL,
        frozenset({"finance"}), "artifact:report-1"),
        ArtifactProvenance("alice", "team-a", "documents.export", "1", datetime.now(UTC)),
        frozenset({"team-a"}),
    )
    principal = Principal("alice", "team-a", frozenset({"artifact_publisher"}),
                          Classification.CONFIDENTIAL, frozenset({"finance"}))

    class Store:
        def put(self, **kwargs):
            from coifesp_harness.artifacts import StoredObject
            assert b"".join(kwargs["chunks"]) == payload
            return StoredObject("team-a", digest, len(payload), f"artifact-store://team-a/{digest}")
    class Repository:
        def publish(self, **kwargs):
            assert kwargs["manifest"].content_uri == f"artifact-store://team-a/{digest}"
            return kwargs["manifest"], False
    result, duplicate = ArtifactContentService(Repository(), Store()).publish(
        principal=principal, idempotency_key="upload-1", manifest=manifest, chunks=(payload,)
    )
    assert not duplicate and result.sha256 == digest
