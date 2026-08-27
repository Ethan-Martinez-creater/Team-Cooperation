from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Request,
    Response,
)
from fastapi.concurrency import run_in_threadpool

from ..errors import MemoryUnavailableError, ResourceNotFound
from ..memory import (
    MemoryScope,
    MemoryService,
    MemorySource,
    MemoryStatus,
    MemoryWriteRequest,
    SourceType,
    TrustLevel,
)
from ..security import Classification, ResourceLabel
from .auth import Authenticated, BearerAuthenticator
from .models import (
    MemoryCreateBody,
    MemoryReviewBody,
    MemoryReviewResponse,
    MemorySearchResponse,
    MemoryDeletionRequestBody,
    MemoryDeletionDecisionBody,
    MemoryDeletionResponse,
    MemoryLegalHoldBody,
    MemoryLegalHoldReleaseBody,
    MemoryViewResponse,
    MemoryWriteResponse,
)


def build_memory_router(
    *,
    authenticator: BearerAuthenticator,
) -> APIRouter:
    router = APIRouter(prefix="/v1/memories", tags=["memory"])

    @router.post(
        "",
        response_model=MemoryWriteResponse,
        status_code=201,
    )
    async def create_memory(
        body: MemoryCreateBody,
        request: Request,
        response: Response,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
        authenticated: Authenticated = Depends(authenticator),
    ) -> MemoryWriteResponse:
        principal = authenticated.principal
        service = _memory_service(request)
        source_type = SourceType.AGENT if principal.is_service else SourceType.USER
        write_request = MemoryWriteRequest(
            memory_id=body.memory_id,
            idempotency_key=idempotency_key,
            correlation_id=request.state.request_id,
            principal=principal,
            scope=body.scope,
            kind=body.kind,
            content=body.content,
            label=ResourceLabel(
                owner_tenant_id=principal.tenant_id,
                classification=Classification[body.classification.name],
                compartments=frozenset(body.compartments),
                resource_id=f"memory:{body.memory_id}",
            ),
            source=MemorySource(
                source_type=source_type,
                source_id=principal.principal_id,
                source_uri=None,
                trust_level=TrustLevel.LOW,
            ),
            owner_principal_id=(
                principal.principal_id if body.scope is MemoryScope.USER_PRIVATE else None
            ),
            project_id=body.project_id,
            session_id=body.session_id,
            expires_at=body.expires_at,
        )
        result = await run_in_threadpool(service.write, write_request)
        if result.duplicate:
            response.status_code = 200
        elif result.status is MemoryStatus.QUARANTINED:
            response.status_code = 202
        return MemoryWriteResponse(
            memory_id=result.memory_id,
            status=result.status,
            version=result.version,
            admission_reason=result.admission_reason,
            duplicate=result.duplicate,
        )

    @router.get(
        "/{memory_id}",
        response_model=MemoryViewResponse,
    )
    async def read_memory(
        memory_id: str,
        request: Request,
        session_id: str | None = Query(default=None, max_length=128),
        authenticated: Authenticated = Depends(authenticator),
    ) -> MemoryViewResponse:
        service = _memory_service(request)
        try:
            view = await run_in_threadpool(
                service.read,
                principal=authenticated.principal,
                memory_id=memory_id,
                session_id=session_id,
            )
        except MemoryUnavailableError as exc:
            raise ResourceNotFound("memory is not available") from exc
        return _view_response(view)

    @router.get("/-/search", response_model=list[MemorySearchResponse])
    async def search_memories(
        request: Request,
        q: str = Query(min_length=1, max_length=2_000),
        scope: MemoryScope = Query(),
        project_id: str | None = Query(default=None, max_length=128),
        session_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=100),
        hybrid: bool = Query(default=False),
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[MemorySearchResponse]:
        values = await run_in_threadpool(_memory_service(request).search,
            principal=authenticated.principal, query=q, scope=scope,
            project_id=project_id, session_id=session_id, limit=limit, hybrid=hybrid)
        return [MemorySearchResponse(memory=_view_response(item.memory),
            lexical_score=item.lexical_score, semantic_score=item.semantic_score,
            combined_score=item.combined_score) for item in values]

    @router.get(
        "",
        response_model=list[MemoryViewResponse],
    )
    async def recall_memories(
        request: Request,
        scope: MemoryScope = Query(),
        project_id: str | None = Query(default=None, max_length=128),
        session_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=100),
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[MemoryViewResponse]:
        service = _memory_service(request)
        views = await run_in_threadpool(
            service.recall,
            principal=authenticated.principal,
            scope=scope,
            project_id=project_id,
            session_id=session_id,
            limit=limit,
        )
        return [_view_response(view) for view in views]

    @router.get(
        "/{memory_id}/review",
        response_model=MemoryViewResponse,
    )
    async def read_memory_for_review(
        memory_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> MemoryViewResponse:
        service = _memory_service(request)
        try:
            view = await run_in_threadpool(
                service.read_for_review,
                principal=authenticated.principal,
                memory_id=memory_id,
            )
        except MemoryUnavailableError as exc:
            raise ResourceNotFound("quarantined memory is not available") from exc
        return _view_response(view)

    @router.post(
        "/{memory_id}/review",
        response_model=MemoryReviewResponse,
    )
    async def review_memory(
        memory_id: str,
        body: MemoryReviewBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> MemoryReviewResponse:
        service = _memory_service(request)
        try:
            status = await run_in_threadpool(
                service.review,
                principal=authenticated.principal,
                memory_id=memory_id,
                expected_version=body.expected_version,
                approve=body.approve,
                reason=body.reason,
            )
        except MemoryUnavailableError as exc:
            raise ResourceNotFound("quarantined memory is not available") from exc
        return MemoryReviewResponse(memory_id=memory_id, status=status)

    @router.post("/{memory_id}/deletion-requests", response_model=MemoryDeletionResponse,
                 status_code=202)
    async def request_deletion(memory_id: str, body: MemoryDeletionRequestBody,
                               request: Request,
                               authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_lifecycle(request).request_deletion,
            principal=authenticated.principal, request_id=body.request_id,
            memory_id=memory_id, expected_version=body.expected_version, reason=body.reason)
        return MemoryDeletionResponse(request_id=value.request_id,
            memory_id=value.memory_id, status=value.status)

    @router.post("/deletion-requests/{request_id}:decide",
                 response_model=MemoryDeletionResponse)
    async def decide_deletion(request_id: str, body: MemoryDeletionDecisionBody,
                              request: Request,
                              authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_lifecycle(request).decide_deletion,
            principal=authenticated.principal, request_id=request_id,
            approve=body.approve, reason=body.reason)
        return MemoryDeletionResponse(request_id=value.request_id,
            memory_id=value.memory_id, status=value.status)

    @router.post("/{memory_id}/legal-holds", status_code=201)
    async def create_legal_hold(memory_id: str, body: MemoryLegalHoldBody,
                                request: Request,
                                authenticated: Authenticated = Depends(authenticator)):
        await run_in_threadpool(_lifecycle(request).create_hold,
            principal=authenticated.principal, hold_id=body.hold_id,
            memory_id=memory_id, reason=body.reason)
        return {"hold_id": body.hold_id, "memory_id": memory_id, "status": "active"}

    @router.post("/legal-holds/{hold_id}:release")
    async def release_legal_hold(hold_id: str, body: MemoryLegalHoldReleaseBody,
                                 request: Request,
                                 authenticated: Authenticated = Depends(authenticator)):
        await run_in_threadpool(_lifecycle(request).release_hold,
            principal=authenticated.principal, hold_id=hold_id, reason=body.reason)
        return {"hold_id": hold_id, "status": "released"}

    return router


def _memory_service(request: Request) -> MemoryService:
    service = getattr(request.app.state, "memory_service", None)
    if service is None:
        raise MemoryError("Memory service is not configured")
    return service


def _lifecycle(request: Request):
    service = getattr(request.app.state, "memory_lifecycle_service", None)
    if service is None:
        raise MemoryError("Memory lifecycle service is not configured")
    return service


def _view_response(view) -> MemoryViewResponse:
    return MemoryViewResponse(
        memory_id=view.memory_id,
        scope=view.scope,
        kind=view.kind,
        content=view.content,
        classification=view.label.classification.name.lower(),
        compartments=sorted(view.label.compartments),
        source_type=view.source.source_type.value,
        trust_level=view.source.trust_level.name.lower(),
        status=view.status,
        created_at=view.created_at,
        expires_at=view.expires_at,
        version=view.version,
    )
