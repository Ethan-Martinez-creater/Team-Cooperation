import asyncio
import hashlib

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.artifacts import (
    ArtifactContentService,
    LocalImmutableArtifactStore,
    SQLAlchemyArtifactRepository,
)
from coifesp_harness.control_plane import create_app
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal
from test_agent_run_api import Verifier, identity, settings


async def request(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="https://control.example.test",
    ) as client:
        return await client.request(method, path, **kwargs)


def stack(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}
        ),
    )
    audit.create_schema()
    repository = SQLAlchemyArtifactRepository(engine=engine, audit_log=audit)
    repository.create_schema()
    content = ArtifactContentService(repository, LocalImmutableArtifactStore(tmp_path))
    owner = Principal(
        "alice",
        "team-a",
        roles=frozenset({"artifact_publisher"}),
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
    )
    app = create_app(
        settings=settings(),
        verifier=Verifier({"owner": identity(owner)}),
        artifact_repository=repository,
        artifact_content_service=content,
    )
    return app


def test_workspace_upload_lists_and_downloads_immutable_content(tmp_path):
    app = stack(tmp_path)
    payload = b"reviewed team delivery"
    metadata = (
        '{"artifact_id":"report-1","kind":"document",'
        '"classification":"confidential","compartments":["program-1"],'
        '"visible_to_tenants":["team-a"]}'
    )
    uploaded = asyncio.run(
        request(
            app,
            "POST",
            "/v1/artifacts:upload",
            files={"content": ("report.txt", payload, "text/plain")},
            data={"metadata": metadata},
            headers={"Authorization": "Bearer owner", "Idempotency-Key": "upload-report-1"},
        )
    )
    assert uploaded.status_code == 201, uploaded.text
    value = uploaded.json()
    assert value["owner_tenant_id"] == "team-a"
    assert value["sha256"] == hashlib.sha256(payload).hexdigest()
    assert value["content_uri"].startswith("artifact-store://team-a/")

    replayed = asyncio.run(
        request(
            app,
            "POST",
            "/v1/artifacts:upload",
            files={"content": ("report.txt", payload, "text/plain")},
            data={"metadata": metadata},
            headers={"Authorization": "Bearer owner", "Idempotency-Key": "upload-report-1"},
        )
    )
    assert replayed.status_code == 200
    assert replayed.json()["created_at"] == value["created_at"]

    listed = asyncio.run(
        request(
            app,
            "GET",
            "/v1/artifacts",
            headers={"Authorization": "Bearer owner"},
        )
    )
    assert [item["artifact_id"] for item in listed.json()] == ["report-1"]

    downloaded = asyncio.run(
        request(
            app,
            "GET",
            f'/v1/artifacts/team-a/report-1/content?sha256={value["sha256"]}',
            headers={"Authorization": "Bearer owner"},
        )
    )
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["digest"] == f'sha-256={value["sha256"]}'


def test_workspace_upload_requires_publisher_role_and_configured_store(tmp_path):
    app = stack(tmp_path)
    viewer = Principal("viewer", "team-a")
    app.state.oidc_verifier.identities["viewer"] = identity(viewer)
    denied = asyncio.run(
        request(
            app,
            "POST",
            "/v1/artifacts:upload",
            files={"content": ("x.txt", b"x", "text/plain")},
            data={
                "metadata": '{"artifact_id":"x","kind":"generic",'
                '"classification":"internal","visible_to_tenants":["team-a"]}'
            },
            headers={"Authorization": "Bearer viewer", "Idempotency-Key": "upload-x"},
        )
    )
    assert denied.status_code == 403
