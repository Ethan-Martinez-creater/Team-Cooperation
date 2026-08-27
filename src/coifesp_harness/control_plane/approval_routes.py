from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool

from ..approvals import ApprovalRecord, ApprovalWorkflowError
from ..security import Classification
from .approval_models import (
    ApprovalCreateBody,
    ApprovalDecisionBody,
    ApprovalResponse,
    ApprovalRevokeBody,
)
from .auth import Authenticated, BearerAuthenticator


def build_approval_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(tags=["approvals"])

    @router.post("/v1/approvals", response_model=ApprovalResponse, status_code=201)
    async def create(
        body: ApprovalCreateBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ApprovalResponse:
        record = await run_in_threadpool(
            _service(request).request,
            principal=authenticated.principal,
            approval_id=body.approval_id,
            tool_name=body.tool_name,
            request_digest=body.request_digest,
            reason=body.reason,
            expires_in_seconds=body.expires_in_seconds,
            classification=Classification[body.classification.upper()],
            compartments=frozenset(body.compartments),
        )
        return _response(record)

    @router.get("/v1/approvals/{approval_id}", response_model=ApprovalResponse)
    async def get(
        approval_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ApprovalResponse:
        record = await run_in_threadpool(
            _service(request).get,
            principal=authenticated.principal,
            approval_id=approval_id,
        )
        return _response(record)

    @router.get("/v1/approvals", response_model=list[ApprovalResponse])
    async def list_pending(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[ApprovalResponse]:
        records = await run_in_threadpool(
            _service(request).list_pending,
            principal=authenticated.principal,
            limit=limit,
        )
        return [_response(record) for record in records]

    @router.post("/v1/approvals/{approval_id}:decide", response_model=ApprovalResponse)
    async def decide(
        approval_id: str,
        body: ApprovalDecisionBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ApprovalResponse:
        record = await run_in_threadpool(
            _service(request).decide,
            principal=authenticated.principal,
            approval_id=approval_id,
            approve=body.approve,
            expected_version=body.expected_version,
        )
        return _response(record)

    @router.post("/v1/approvals/{approval_id}:revoke", response_model=ApprovalResponse)
    async def revoke(
        approval_id: str,
        body: ApprovalRevokeBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ApprovalResponse:
        record = await run_in_threadpool(
            _service(request).revoke,
            principal=authenticated.principal,
            approval_id=approval_id,
            expected_version=body.expected_version,
        )
        return _response(record)

    return router


def _service(request: Request):
    service = getattr(request.app.state, "approval_service", None)
    if service is None:
        raise ApprovalWorkflowError("approval service is not configured")
    return service


def _response(record: ApprovalRecord) -> ApprovalResponse:
    value = asdict(record)
    value["classification"] = record.classification.name.lower()
    value["compartments"] = sorted(record.compartments)
    return ApprovalResponse(**value)
