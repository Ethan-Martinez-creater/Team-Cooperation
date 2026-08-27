import hashlib
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from ..artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance
from ..security import Classification, ResourceLabel
from .auth import Authenticated, BearerAuthenticator
from .models import ArtifactPublishBody, ArtifactUploadMetadata, ArtifactView


def build_artifact_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

    @router.get("", response_model=list[ArtifactView])
    async def list_artifacts(
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        if request.app.state.artifact_repository is None:
            return []
        values = await run_in_threadpool(
            request.app.state.artifact_repository.list_visible,
            principal=authenticated.principal,
            limit=100,
        )
        return [_view(item) for item in values]

    @router.post(":upload", response_model=ArtifactView, status_code=201)
    async def upload(
        request: Request,
        response: Response,
        metadata: str = Form(..., min_length=2, max_length=16_384),
        content: UploadFile = File(...),
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
        authenticated: Authenticated = Depends(authenticator),
    ):
        principal = authenticated.principal
        if principal.is_service or "artifact_publisher" not in principal.roles:
            from ..errors import PolicyDenied
            raise PolicyDenied("artifact publisher role is required")
        service = getattr(request.app.state, "artifact_content_service", None)
        if service is None:
            from ..config import ConfigurationError
            raise ConfigurationError("artifact content storage is not configured")
        try:
            values = ArtifactUploadMetadata.model_validate(json.loads(metadata))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError("artifact upload metadata is invalid") from exc
        maximum = request.app.state.settings.artifact_max_upload_bytes
        digest = hashlib.sha256()
        size = 0
        while chunk := await content.read(1_048_576):
            size += len(chunk)
            if size > maximum:
                raise ValueError("artifact content exceeds the configured upload limit")
            digest.update(chunk)
        await content.seek(0)
        media_type = (content.content_type or "application/octet-stream").lower()
        manifest = ArtifactManifest(
            values.artifact_id,
            values.kind,
            media_type,
            f"artifact-store://{principal.tenant_id}/pending",
            digest.hexdigest(),
            size,
            ResourceLabel(
                principal.tenant_id,
                Classification[values.classification.name],
                frozenset(values.compartments),
                f"artifact:{values.artifact_id}",
            ),
            ArtifactProvenance(
                principal.principal_id,
                principal.tenant_id,
                "workspace.upload",
                "1",
                datetime.now(UTC),
            ),
            frozenset(values.visible_to_tenants),
        )

        def chunks():
            while value := content.file.read(1_048_576):
                yield value

        result, duplicate = await run_in_threadpool(
            service.publish,
            principal=principal,
            idempotency_key=idempotency_key,
            manifest=manifest,
            chunks=chunks(),
        )
        if duplicate:
            response.status_code = 200
        return _view(result)

    @router.post("", response_model=ArtifactView, status_code=201)
    async def publish(body: ArtifactPublishBody, request: Request, response: Response,
                      idempotency_key: str = Header(..., alias="Idempotency-Key",
                        min_length=1, max_length=128,
                        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
                      authenticated: Authenticated = Depends(authenticator)):
        principal = authenticated.principal
        if principal.is_service or "artifact_publisher" not in principal.roles:
            from ..errors import PolicyDenied
            raise PolicyDenied("artifact publisher role is required")
        value = ArtifactManifest(body.artifact_id, body.kind, body.media_type,
            body.content_uri, body.sha256, body.size_bytes,
            ResourceLabel(principal.tenant_id, Classification[body.classification.name],
                frozenset(body.compartments), f"artifact:{body.artifact_id}"),
            ArtifactProvenance(principal.principal_id, principal.tenant_id,
                body.source_tool, body.source_version, body.created_at),
            frozenset(body.visible_to_tenants))
        result, duplicate = await run_in_threadpool(request.app.state.artifact_repository.publish,
            principal=principal, idempotency_key=idempotency_key, manifest=value)
        if duplicate: response.status_code = 200
        return _view(result)

    @router.get("/{owner_tenant_id}/{artifact_id}", response_model=ArtifactView)
    async def read(owner_tenant_id: str, artifact_id: str, request: Request,
                   authenticated: Authenticated = Depends(authenticator)):
        return _view(await run_in_threadpool(request.app.state.artifact_repository.read,
            principal=authenticated.principal, owner_tenant_id=owner_tenant_id,
            artifact_id=artifact_id))

    @router.get("/{owner_tenant_id}/{artifact_id}/content")
    async def download(
        owner_tenant_id: str,
        artifact_id: str,
        sha256: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        service = getattr(request.app.state, "artifact_content_service", None)
        if service is None:
            from ..config import ConfigurationError
            raise ConfigurationError("artifact content storage is not configured")
        manifest = await run_in_threadpool(
            request.app.state.artifact_repository.read,
            principal=authenticated.principal,
            owner_tenant_id=owner_tenant_id,
            artifact_id=artifact_id,
            expected_sha256=sha256,
        )
        stream = await run_in_threadpool(
            service.open,
            principal=authenticated.principal,
            owner_tenant_id=owner_tenant_id,
            artifact_id=artifact_id,
            expected_sha256=sha256,
        )
        return StreamingResponse(
            stream,
            media_type=manifest.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact_id}"',
                "Digest": f"sha-256={sha256}",
            },
        )
    return router


def _view(item):
    return ArtifactView(owner_tenant_id=item.label.owner_tenant_id,
        artifact_id=item.artifact_id, kind=item.kind,
        media_type=item.media_type, content_uri=item.content_uri, sha256=item.sha256,
        size_bytes=item.size_bytes, classification=item.label.classification.name.lower(),
        compartments=sorted(item.label.compartments),
        producer_principal_id=item.provenance.producer_principal_id,
        source_tool=item.provenance.source_tool, source_version=item.provenance.source_version,
        created_at=item.provenance.created_at,
        visible_to_tenants=sorted(item.visible_to_tenants))
