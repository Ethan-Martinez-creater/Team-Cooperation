import hashlib
import io
import json
import zipfile
from dataclasses import replace

import pytest

from coifesp_harness.delivery.bundle import BundleArtifact, assemble_delivery_bundle


def artifact(resource_id="resource-a", *, content=b"verified deliverable", filename="report.txt"):
    return BundleArtifact(resource_id, "v1", filename, "text/plain",
                          hashlib.sha256(content).hexdigest(), content)


def test_composition_produces_reproducible_real_bytes_and_pinned_manifest():
    a, b = artifact(), artifact("resource-b", content="设计说明".encode())
    first = assemble_delivery_bundle([b, a])
    assert assemble_delivery_bundle(iter([a, b])) == first
    assert hashlib.sha256(first.content).hexdigest() == first.sha256
    manifest = json.loads(first.manifest)
    assert manifest["schema"] == "coifesp.delivery-bundle.v1"
    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
        assert archive.namelist() == ["manifest.json", "artifacts/000001", "artifacts/000002"]
        assert archive.read("manifest.json") == first.manifest
        for entry, source in zip(manifest["artifacts"], (a, b), strict=True):
            assert archive.read(entry["path"]) == source.content
            assert entry["sha256"] == source.sha256
            assert entry["resource_id"] == source.resource_id
            assert entry["version"] == source.version


@pytest.mark.parametrize("filename", ["../../outside.txt", "C:\\secret.txt", "/etc/passwd",
                                    "manifest.json", "团队说明.txt"])
def test_untrusted_filenames_are_metadata_not_archive_paths(filename):
    result = assemble_delivery_bundle([artifact(filename=filename)])
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert archive.namelist() == ["manifest.json", "artifacts/000001"]
    assert json.loads(result.manifest)["artifacts"][0]["filename"] == filename


@pytest.mark.parametrize("inputs,match", [([], "empty"),
    ([artifact(), artifact()], "duplicate"),
    ([replace(artifact(), content=b"tampered")], "integrity"),
    ([replace(artifact(), sha256="invalid")], "digest"),
    ([replace(artifact(), resource_id="")], "resource_id"),
    ([replace(artifact(), version="")], "version")])
def test_invalid_or_unverified_artifacts_cannot_produce_bundle(inputs, match):
    with pytest.raises(ValueError, match=match):
        assemble_delivery_bundle(inputs)


def test_limits_are_checked_before_bundle_creation():
    with pytest.raises(ValueError, match="count"):
        assemble_delivery_bundle([artifact(), artifact("b")], max_artifacts=1)
    with pytest.raises(ValueError, match="byte limit"):
        assemble_delivery_bundle([artifact()], max_total_bytes=1)
    with pytest.raises(ValueError, match="limit"):
        assemble_delivery_bundle([artifact()], max_artifacts=True)
    with pytest.raises(TypeError, match="immutable"):
        assemble_delivery_bundle([replace(artifact(), content=bytearray(b"mutable"))])
