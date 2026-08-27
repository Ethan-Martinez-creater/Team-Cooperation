from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .auth import Authenticated, BearerAuthenticator
from .product_models import (
    DocumentDerivativeView,
    DocumentDraftCreateBody,
    DocumentDraftDecideBody,
    DocumentDraftView,
    DocumentVersionView,
)


def _service(request: Request):
    service = getattr(request.app.state, "document_workspace_service", None)
    if service is None:
        from ..config import ConfigurationError

        raise ConfigurationError("document workspace service is unavailable")
    return service


def _principal_id(authenticated: Authenticated) -> str:
    return authenticated.principal.principal_id


def _version_view(item) -> DocumentVersionView:
    return DocumentVersionView(
        version_id=item.version_id,
        resource_id=item.resource_id,
        version_number=item.version_number,
        artifact_id=item.artifact_id,
        artifact_sha256=item.artifact_sha256,
        parent_version_id=item.parent_version_id,
        created_by=item.created_by,
        reason=item.reason,
        created_at=item.created_at,
    )


def _derivative_view(item) -> DocumentDerivativeView:
    return DocumentDerivativeView(
        derivative_id=item.derivative_id,
        resource_id=item.resource_id,
        version_id=item.version_id,
        derivative_type=item.derivative_type.value,
        status=item.status.value,
        summary=item.summary,
        size_bytes=item.size_bytes,
        error_category=item.error_category,
        created_at=item.created_at,
    )


def _draft_view(item) -> DocumentDraftView:
    return DocumentDraftView(
        draft_id=item.draft_id,
        resource_id=item.resource_id,
        source_version_id=item.source_version_id,
        modification_json=item.modification_json,
        generated_version_id=item.generated_version_id,
        status=item.status.value,
        version=item.version,
        created_by=item.created_by,
        created_at=item.created_at,
        decided_by=item.decided_by,
        decided_at=item.decided_at,
    )


def build_document_workspace_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}/resources/{resource_id}/documents", tags=["document-workspace"])

    @router.post("/versions:ensure-initial", response_model=DocumentVersionView, status_code=201)
    async def ensure_initial_version(
        project_id: str,
        resource_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        item = await run_in_threadpool(
            service.ensure_initial_version,
            actor_id=_principal_id(authenticated),
            resource_id=resource_id,
        )
        return _version_view(item)

    @router.get("/versions", response_model=list[DocumentVersionView])
    async def list_versions(
        project_id: str,
        resource_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        items = await run_in_threadpool(
            service.list_versions, actor_id=_principal_id(authenticated), resource_id=resource_id
        )
        return [_version_view(item) for item in items]

    @router.get("/versions/{version_id}", response_model=DocumentVersionView)
    async def get_version(
        project_id: str,
        resource_id: str,
        version_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        item = await run_in_threadpool(
            service.get_version,
            actor_id=_principal_id(authenticated),
            resource_id=resource_id,
            version_id=version_id,
        )
        return _version_view(item)

    @router.post("/parse", response_model=DocumentDerivativeView, status_code=201)
    async def parse_resource(
        project_id: str,
        resource_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        item = await run_in_threadpool(
            service.parse_resource, actor_id=_principal_id(authenticated), resource_id=resource_id
        )
        return _derivative_view(item)

    @router.get("/derivatives", response_model=list[DocumentDerivativeView])
    async def list_derivatives(
        project_id: str,
        resource_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        items = await run_in_threadpool(
            service.list_derivatives, actor_id=_principal_id(authenticated), resource_id=resource_id
        )
        return [_derivative_view(item) for item in items]

    @router.get("/derivatives/{derivative_id}/content")
    async def get_derivative_content(
        project_id: str,
        resource_id: str,
        derivative_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            content = await run_in_threadpool(
                service.get_derivative_content,
                actor_id=_principal_id(authenticated),
                resource_id=resource_id,
                derivative_id=derivative_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return content

    @router.post("/change-drafts", response_model=DocumentDraftView, status_code=201)
    async def create_change_draft(
        project_id: str,
        resource_id: str,
        body: DocumentDraftCreateBody,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            item = await run_in_threadpool(
                service.create_change_draft,
                actor_id=_principal_id(authenticated),
                resource_id=resource_id,
                source_version_id=body.source_version_id,
                modification=body.modification,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _draft_view(item)

    @router.get("/change-drafts", response_model=list[DocumentDraftView])
    async def list_change_drafts(
        project_id: str,
        resource_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        items = await run_in_threadpool(
            service.list_change_drafts, actor_id=_principal_id(authenticated), resource_id=resource_id
        )
        return [_draft_view(item) for item in items]

    @router.get("/change-drafts/{draft_id}", response_model=DocumentDraftView)
    async def get_change_draft(
        project_id: str,
        resource_id: str,
        draft_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        item = await run_in_threadpool(
            service.get_change_draft,
            actor_id=_principal_id(authenticated),
            resource_id=resource_id,
            draft_id=draft_id,
        )
        return _draft_view(item)

    @router.post("/change-drafts/{draft_id}:decide", response_model=DocumentDraftView)
    async def decide_change_draft(
        project_id: str,
        resource_id: str,
        draft_id: str,
        body: DocumentDraftDecideBody,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            item = await run_in_threadpool(
                service.decide_change_draft,
                actor_id=_principal_id(authenticated),
                resource_id=resource_id,
                draft_id=draft_id,
                approve=body.approve,
                expected_version=body.expected_version,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _draft_view(item)

    return router
