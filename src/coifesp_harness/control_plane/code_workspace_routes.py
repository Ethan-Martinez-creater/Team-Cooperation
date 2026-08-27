from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .auth import Authenticated, BearerAuthenticator
from .models import StrictModel
from .product_models import ProjectRepositoryView


class RepositoryBindBody(StrictModel):
    repository_id: str
    connector_id: str
    connector_version: int
    remote_repository_id: str
    default_branch: str
    available_operations: tuple[str, ...] = ("read_tree", "read_blob", "search_code")


class DraftFileEntry(StrictModel):
    path: str
    new_content: str


class ChangeDraftCreateBody(StrictModel):
    repository_id: str
    base_commit: str
    files: tuple[DraftFileEntry, ...]
    reason: str


class ChangeDraftDecideBody(StrictModel):
    approve: bool
    expected_version: int


def _service(request: Request):
    service = getattr(request.app.state, "code_workspace_service", None)
    if service is None:
        from ..config import ConfigurationError

        raise ConfigurationError("code workspace service is unavailable")
    return service


def _principal_id(authenticated: Authenticated) -> str:
    return authenticated.principal.principal_id


def _repository_view(item) -> ProjectRepositoryView:
    return ProjectRepositoryView(
        project_id=item.project_id,
        repository_id=item.repository_id,
        connector_id=item.connector_id,
        connector_version=item.connector_version,
        remote_repository_id=item.remote_repository_id,
        default_branch=item.default_branch,
        available_operations=list(item.available_operations),
        created_by=item.created_by,
        created_at=item.created_at,
    )


def _draft_view(item) -> dict:
    return {
        "draft_id": item.draft_id,
        "project_id": item.project_id,
        "repository_id": item.repository_id,
        "base_commit": item.base_commit,
        "files": list(item.files),
        "patch_text": item.patch_text,
        "status": item.status.value,
        "version": item.version,
        "created_by": item.created_by,
        "created_at": item.created_at,
        "decided_by": item.decided_by,
        "decided_at": item.decided_at,
        "patch_artifact_id": item.patch_artifact_id,
        "patch_artifact_sha256": item.patch_artifact_sha256,
    }


def build_code_workspace_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}/code", tags=["code-workspace"])

    @router.post(
        "/repositories",
        response_model=ProjectRepositoryView,
        status_code=201,
    )
    async def bind_repository(
        project_id: str,
        body: RepositoryBindBody,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            item = await run_in_threadpool(
                service.bind_repository,
                actor_id=_principal_id(authenticated),
                project_id=project_id,
                repository_id=body.repository_id,
                connector_id=body.connector_id,
                connector_version=body.connector_version,
                remote_repository_id=body.remote_repository_id,
                default_branch=body.default_branch,
                available_operations=body.available_operations,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _repository_view(item)

    @router.get("/repositories", response_model=list[ProjectRepositoryView])
    async def list_repositories(
        project_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        items = await run_in_threadpool(
            service.list_repositories, actor_id=_principal_id(authenticated), project_id=project_id
        )
        return [_repository_view(item) for item in items]

    @router.get("/repositories/{repository_id}/status")
    async def git_context_status(
        project_id: str,
        repository_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        status = await run_in_threadpool(
            service.git_context_status,
            actor_id=_principal_id(authenticated),
            project_id=project_id,
            repository_id=repository_id,
        )
        return {
            "repository_bound": status.repository_bound,
            "repository_id": status.repository_id,
            "default_branch": status.default_branch,
            "reason": status.reason,
        }

    @router.get("/repositories/{repository_id}/tree")
    async def list_tree(
        project_id: str,
        repository_id: str,
        commit: str,
        path: str = "",
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            entries = await run_in_threadpool(
                service.list_tree,
                actor_id=_principal_id(authenticated),
                project_id=project_id,
                repository_id=repository_id,
                commit=commit,
                path=path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return [
            {
                "path": item.path,
                "kind": item.kind,
                "mode": item.mode,
                "size_bytes": item.size_bytes,
            }
            for item in entries
        ]

    @router.get("/repositories/{repository_id}/blob")
    async def read_blob(
        project_id: str,
        repository_id: str,
        commit: str,
        path: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            blob = await run_in_threadpool(
                service.read_blob,
                actor_id=_principal_id(authenticated),
                project_id=project_id,
                repository_id=repository_id,
                commit=commit,
                path=path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "path": blob.path,
            "commit": blob.commit,
            "size_bytes": blob.size_bytes,
            "sha256": blob.sha256,
            "text": blob.text,
        }

    @router.get("/repositories/{repository_id}/search")
    async def search_code(
        project_id: str,
        repository_id: str,
        commit: str,
        query: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            hits = await run_in_threadpool(
                service.search_code,
                actor_id=_principal_id(authenticated),
                project_id=project_id,
                repository_id=repository_id,
                commit=commit,
                query=query,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return list(hits)

    @router.post("/change-drafts", status_code=201)
    async def create_change_draft(
        project_id: str,
        body: ChangeDraftCreateBody,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        try:
            item = await run_in_threadpool(
                service.create_change_draft,
                actor_id=_principal_id(authenticated),
                project_id=project_id,
                repository_id=body.repository_id,
                base_commit=body.base_commit,
                files=[{"path": entry.path, "new_content": entry.new_content} for entry in body.files],
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _draft_view(item)

    @router.get("/change-drafts")
    async def list_change_drafts(
        project_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        items = await run_in_threadpool(
            service.list_change_drafts, actor_id=_principal_id(authenticated), project_id=project_id
        )
        return [_draft_view(item) for item in items]

    @router.get("/change-drafts/{draft_id}")
    async def get_change_draft(
        project_id: str,
        draft_id: str,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        item = await run_in_threadpool(
            service.get_change_draft,
            actor_id=_principal_id(authenticated),
            project_id=project_id,
            draft_id=draft_id,
        )
        return _draft_view(item)

    @router.post("/change-drafts/{draft_id}:decide")
    async def decide_change_draft(
        project_id: str,
        draft_id: str,
        body: ChangeDraftDecideBody,
        authenticated: Authenticated = Depends(authenticator),
        request: Request = None,
    ):
        service = _service(request)
        patch_content_service = getattr(request.app.state, "artifact_content_service", None)
        try:
            item = await run_in_threadpool(
                service.decide_change_draft,
                actor_id=_principal_id(authenticated),
                project_id=project_id,
                draft_id=draft_id,
                approve=body.approve,
                expected_version=body.expected_version,
                patch_content_service=patch_content_service,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _draft_view(item)

    return router
