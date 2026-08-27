from datetime import UTC, datetime

import pytest

from coifesp_harness.artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance
from coifesp_harness.security import Classification, ResourceLabel


def manifest(kind: ArtifactKind, media_type: str) -> ArtifactManifest:
    return ArtifactManifest(
        artifact_id=f"artifact-{kind.value}",
        kind=kind,
        media_type=media_type,
        content_uri=f"artifact://team-a/{kind.value}/42",
        sha256="a" * 64,
        size_bytes=42,
        label=ResourceLabel(
            owner_tenant_id="team-a",
            classification=Classification.CONFIDENTIAL,
            compartments=frozenset({"program-1"}),
        ),
        provenance=ArtifactProvenance(
            producer_principal_id="worker-a",
            producer_tenant_id="team-a",
            source_tool="office.document.export",
            source_version="1",
            created_at=datetime.now(UTC),
        ),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )


@pytest.mark.parametrize(
    ("kind", "media_type"),
    [
        (ArtifactKind.SOURCE_CODE, "application/vnd.git.commit"),
        (ArtifactKind.TEST_REPORT, "application/junit+xml"),
        (
            ArtifactKind.DOCUMENT,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            ArtifactKind.SPREADSHEET,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            ArtifactKind.PRESENTATION,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        (ArtifactKind.PDF, "application/pdf"),
    ],
)
def test_artifact_contract_covers_coding_and_office_outputs(
    kind: ArtifactKind, media_type: str
) -> None:
    value = manifest(kind, media_type)
    assert value.is_visible_to("team-b")
    assert not value.is_visible_to("team-c")


def test_artifact_contract_rejects_secrets_in_uri_and_implicit_visibility() -> None:
    value = manifest(ArtifactKind.DOCUMENT, "application/vnd.test")
    with pytest.raises(ValueError, match="credentials"):
        ArtifactManifest(
            artifact_id=value.artifact_id,
            kind=value.kind,
            media_type=value.media_type,
            content_uri="https://user:secret@storage.example.test/file",
            sha256=value.sha256,
            size_bytes=value.size_bytes,
            label=value.label,
            provenance=value.provenance,
            visible_to_tenants=value.visible_to_tenants,
        )
    with pytest.raises(ValueError, match="including its owner"):
        ArtifactManifest(
            artifact_id=value.artifact_id,
            kind=value.kind,
            media_type=value.media_type,
            content_uri=value.content_uri,
            sha256=value.sha256,
            size_bytes=value.size_bytes,
            label=value.label,
            provenance=value.provenance,
            visible_to_tenants=frozenset({"team-b"}),
        )
