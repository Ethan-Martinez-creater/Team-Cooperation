from datetime import UTC, datetime
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
import pytest

from coifesp_harness.artifacts import (ArtifactKind, ArtifactManifest,
    ArtifactProvenance, SQLAlchemyArtifactRepository, ArtifactDeliveryGuard)
from coifesp_harness.errors import ResourceNotFound
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal, ResourceLabel


def test_artifact_registry_is_idempotent_audited_and_cross_tenant_visible():
    engine = create_engine("sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring(
        active_key_id="audit-v1", verification_keys={"audit-v1": b"a"*32}))
    audit.create_schema(); repo = SQLAlchemyArtifactRepository(engine=engine, audit_log=audit)
    repo.create_schema()
    owner = Principal("publisher", "team-a", roles=frozenset({"artifact_publisher"}),
        clearance=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"}))
    artifact = ArtifactManifest("report-1", ArtifactKind.DOCUMENT,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "artifact://team-a/reports/report-1", "a"*64, 123,
        ResourceLabel("team-a", Classification.CONFIDENTIAL,
            frozenset({"project-x"}), "artifact:report-1"),
        ArtifactProvenance("publisher", "team-a", "word-worker", "1.0", datetime.now(UTC)),
        frozenset({"team-a", "team-b"}))
    first = repo.publish(principal=owner, idempotency_key="publish-1", manifest=artifact)
    second = repo.publish(principal=owner, idempotency_key="publish-1", manifest=artifact)
    assert first[1] is False and second[1] is True and audit.verify_tenant_chain("team-a") == 1
    reader = Principal("reader", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    assert repo.read(principal=reader, owner_tenant_id="team-a",
        artifact_id="report-1", expected_sha256="a"*64).sha256 == "a"*64
    with pytest.raises(ResourceNotFound):
        repo.read(principal=Principal("outsider", "team-c", clearance=Classification.RESTRICTED,
            compartments=frozenset({"project-x"})), owner_tenant_id="team-a",
            artifact_id="report-1")


def test_delivery_guard_requires_owner_id_digest_and_assignment_visibility():
    class Assignment:
        visible_to_tenants = frozenset({"team-a", "team-b"})
    class Board:
        assignments = {"task-1": Assignment()}
        classification = Classification.CONFIDENTIAL
        compartments = frozenset({"project-x"})
    class Repository:
        def get(self, connection, **kwargs):
            return ArtifactManifest("report-1", ArtifactKind.PDF, "application/pdf",
                "artifact://team-a/report-1", "a"*64, 1,
                ResourceLabel("team-a", Classification.CONFIDENTIAL,
                    frozenset({"project-x"}), "artifact:report-1"),
                ArtifactProvenance("publisher", "team-a", "pdf", "1", datetime.now(UTC)),
                frozenset({"team-a", "team-b"}))
    guard = ArtifactDeliveryGuard(Repository())
    principal = Principal("worker", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    guard.assert_assignment_artifacts(None, principal=principal, board=Board(),
        assignment_id="task-1",
        artifact_refs=("artifact://team-a/report-1?sha256=" + "a"*64,))
    from coifesp_harness.errors import IntegrityError
    with pytest.raises(IntegrityError):
        guard.assert_assignment_artifacts(None, principal=principal, board=Board(),
            assignment_id="task-1",
            artifact_refs=("artifact://team-a/report-1?sha256=" + "b"*64,))
    with pytest.raises(ValueError, match="bind owner"):
        guard.assert_assignment_artifacts(None, principal=principal, board=Board(),
            assignment_id="task-1", artifact_refs=("git:commit:abc",))
