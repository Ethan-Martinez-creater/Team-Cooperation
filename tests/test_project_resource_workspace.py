import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.artifacts import (
    ArtifactContentService,
    LocalImmutableArtifactStore,
    SQLAlchemyArtifactRepository,
)
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectResourceService,
    TeamCollaborationService,
)


async def request(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


def stack(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}
        ),
    )
    audit.create_schema()
    artifacts = SQLAlchemyArtifactRepository(engine=engine, audit_log=audit)
    artifacts.create_schema()
    content = ArtifactContentService(artifacts, LocalImmutableArtifactStore(tmp_path))
    resources = ProjectResourceService(engine, artifact_repository=artifacts)
    collaboration = TeamCollaborationService(engine)
    app = create_app(
        settings=Settings.from_environment({"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}),
        product_account_service=accounts,
        project_directory_service=ProjectDirectoryService(engine),
        project_resource_service=resources,
        team_collaboration_service=collaboration,
        artifact_repository=artifacts,
        artifact_content_service=content,
    )
    return app


def team_admin(app, handle):
    created = asyncio.run(
        request(app, "POST", "/v1/teams/register", json={"handle": handle, "name": handle})
    ).json()
    password = "Admin-Correct-Horse-42!"
    asyncio.run(
        request(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": created["administrator_username"],
                "current_password": created["administrator_initial_password"],
                "new_password": password,
            },
        )
    )
    session = asyncio.run(
        request(
            app,
            "POST",
            "/v1/sessions",
            json={"login": created["administrator_username"], "password": password},
        )
    ).json()
    return created["team"], {"Authorization": f"Bearer {session['access_token']}"}


def project_with_two_teams(app):
    alpha, alpha_headers = team_admin(app, "resource-alpha")
    beta, beta_headers = team_admin(app, "resource-beta")
    relation = asyncio.run(
        request(
            app,
            "POST",
            "/v1/team-relations/requests",
            headers=alpha_headers,
            json={"recipient_team_handle": beta["handle"], "message": "交付资料"},
        )
    ).json()
    asyncio.run(
        request(
            app,
            "POST",
            f"/v1/team-relations/requests/{relation['request_id']}:decide",
            headers=beta_headers,
            json={"accept": True},
        )
    )
    project = asyncio.run(
        request(
            app,
            "POST",
            "/v1/projects",
            headers=alpha_headers,
            json={
                "name": "资料协作",
                "description": "",
                "owner_assignment_name": "主导团队",
                "owner_kind": "product",
            },
        )
    ).json()
    asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project['project_id']}/teams",
            headers=alpha_headers,
            json={
                "team_id": beta["team_id"],
                "assignment_name": "工程团队",
                "kind": "engineering",
            },
        )
    )
    return project["project_id"], alpha_headers, beta_headers


def test_project_upload_is_recoverable_previewable_and_policy_bound(tmp_path):
    app = stack(tmp_path)
    project_id, alpha_headers, beta_headers = project_with_two_teams(app)
    headers = {**alpha_headers, "Idempotency-Key": "upload-project-spec-v1"}
    payload = "接口约定：响应必须包含 request_id。".encode()
    upload = asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project_id}/resources:upload",
            headers=headers,
            data={"title": "接口约定.md", "propagation": "project_readonly"},
            files={"content": ("contract.md", payload, "text/markdown")},
        )
    )
    assert upload.status_code == 201, upload.text
    resource = upload.json()
    assert resource["media_type"] == "text/markdown"
    assert resource["propagation"] == "project_readonly"

    replay = asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project_id}/resources:upload",
            headers=headers,
            data={"title": "接口约定.md", "propagation": "project_readonly"},
            files={"content": ("contract.md", payload, "text/markdown")},
        )
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["resource_id"] == resource["resource_id"]

    listed = asyncio.run(
        request(app, "GET", f"/v1/projects/{project_id}/resources", headers=beta_headers)
    )
    assert [item["resource_id"] for item in listed.json()] == [resource["resource_id"]]
    preview = asyncio.run(
        request(
            app,
            "GET",
            f"/v1/projects/{project_id}/resources/{resource['resource_id']}/preview",
            headers=beta_headers,
        )
    )
    assert preview.status_code == 200
    assert preview.content == payload
    assert preview.headers["content-disposition"] == "inline"
    assert preview.headers["x-content-type-options"] == "nosniff"
    denied_download = asyncio.run(
        request(
            app,
            "GET",
            f"/v1/projects/{project_id}/resources/{resource['resource_id']}/content",
            headers=beta_headers,
        )
    )
    assert denied_download.status_code == 403

    activities = asyncio.run(
        request(app, "GET", f"/v1/projects/{project_id}/activities", headers=beta_headers)
    ).json()
    assert [item["event_type"] for item in activities].count("resource.published") == 1
    foreign_artifacts = asyncio.run(
        request(app, "GET", "/v1/artifacts", headers=beta_headers)
    ).json()
    assert foreign_artifacts == []


def test_private_project_upload_does_not_disclose_or_emit_shared_activity(tmp_path):
    app = stack(tmp_path)
    project_id, alpha_headers, beta_headers = project_with_two_teams(app)
    upload = asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project_id}/resources:upload",
            headers={**alpha_headers, "Idempotency-Key": "private-design-v1"},
            data={"title": "内部设计", "propagation": "team_private"},
            files={"content": ("design.txt", b"private implementation", "text/plain")},
        )
    )
    assert upload.status_code == 201, upload.text
    resource_id = upload.json()["resource_id"]
    assert (
        asyncio.run(
            request(app, "GET", f"/v1/projects/{project_id}/resources", headers=beta_headers)
        ).json()
        == []
    )
    denied = asyncio.run(
        request(
            app,
            "GET",
            f"/v1/projects/{project_id}/resources/{resource_id}/preview",
            headers=beta_headers,
        )
    )
    assert denied.status_code == 403
    assert (
        asyncio.run(
            request(app, "GET", f"/v1/projects/{project_id}/activities", headers=beta_headers)
        ).json()
        == []
    )


def test_project_upload_recovery_binds_full_command_metadata(tmp_path):
    app = stack(tmp_path)
    project_id, alpha_headers, _ = project_with_two_teams(app)
    headers = {**alpha_headers, "Idempotency-Key": "stable-upload-command"}
    service = app.state.project_resource_service
    publish = service.publish

    def interrupted(**_):
        raise RuntimeError("simulated interruption after immutable content publication")

    service.publish = interrupted
    first = asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project_id}/resources:upload",
            headers=headers,
            data={"title": "原始标题", "propagation": "portable"},
            files={"content": ("result.txt", b"result", "text/plain")},
        )
    )
    assert first.status_code == 500
    service.publish = publish

    changed = asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project_id}/resources:upload",
            headers=headers,
            data={"title": "篡改标题", "propagation": "portable"},
            files={"content": ("result.txt", b"result", "text/plain")},
        )
    )
    assert changed.status_code == 409, changed.text

    recovered = asyncio.run(
        request(
            app,
            "POST",
            f"/v1/projects/{project_id}/resources:upload",
            headers=headers,
            data={"title": "原始标题", "propagation": "portable"},
            files={"content": ("result.txt", b"result", "text/plain")},
        )
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["title"] == "原始标题"
