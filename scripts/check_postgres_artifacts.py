from __future__ import annotations
import sys, uuid
from datetime import UTC, datetime
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from coifesp_harness.artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance, SQLAlchemyArtifactRepository
from coifesp_harness.config import Settings
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal, ResourceLabel
from coifesp_harness.errors import ResourceNotFound

def main():
    load_dotenv(ROOT/".env",override=False); settings=Settings.from_environment()
    engine=create_engine(settings.database_url,hide_parameters=True)
    audit=SQLAlchemyAuditLog(engine=engine,keyring=AuditSigningKeyring.from_settings(settings))
    repo=SQLAlchemyArtifactRepository(engine=engine,audit_log=audit); suffix=uuid.uuid4().hex[:12]
    owner=f"artifact-owner-{suffix}"; consumer=f"artifact-reader-{suffix}"; artifact_id=f"report-{suffix}"
    principal=Principal("publisher",owner,roles=frozenset({"artifact_publisher"}),clearance=Classification.CONFIDENTIAL,compartments=frozenset({"project"}))
    manifest=ArtifactManifest(artifact_id,ArtifactKind.PDF,"application/pdf",
        f"artifact://{owner}/reports/{artifact_id}","a"*64,123,
        ResourceLabel(owner,Classification.CONFIDENTIAL,frozenset({"project"}),f"artifact:{artifact_id}"),
        ArtifactProvenance("publisher",owner,"pdf-worker","1.0",datetime.now(UTC)),frozenset({owner,consumer}))
    assert repo.publish(principal=principal,idempotency_key=f"publish-{suffix}",manifest=manifest)[1] is False
    assert repo.publish(principal=principal,idempotency_key=f"publish-{suffix}",manifest=manifest)[1] is True
    reader=Principal("reader",consumer,clearance=Classification.CONFIDENTIAL,compartments=frozenset({"project"}))
    assert repo.read(principal=reader,owner_tenant_id=owner,artifact_id=artifact_id,expected_sha256="a"*64).sha256=="a"*64
    try: repo.read(principal=Principal("out",f"out-{suffix}",clearance=Classification.RESTRICTED,compartments=frozenset({"project"})),owner_tenant_id=owner,artifact_id=artifact_id)
    except ResourceNotFound: pass
    else: raise AssertionError("cross-tenant artifact leaked")
    assert audit.verify_tenant_chain(owner)==1
    with engine.connect() as c:
        rev=c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        rls=c.execute(text("SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE relname=ANY(:names)"),{"names":["artifact_manifests","artifact_manifest_commands"]}).scalar_one()
    engine.dispose(); assert rev=="20260813_28" and rls
    print("ARTIFACT_REGISTRY_OK revision=20260813_28 idempotent=yes digest_bound=yes cross_tenant=explicit audit=verified rls=forced")

if __name__=="__main__": main()
