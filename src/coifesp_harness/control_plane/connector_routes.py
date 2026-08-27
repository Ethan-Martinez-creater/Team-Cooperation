from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from ..config import SecretValue
from ..connectors import ConnectorEndpoint
from ..security import Classification
from .auth import Authenticated, BearerAuthenticator
from .models import (ConnectorAvailableView, ConnectorDecisionBody, ConnectorProposalBody,
    ConnectorRevisionView, ConnectorStateView)


def build_connector_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/connectors", tags=["connectors"])

    @router.get("/available", response_model=list[ConnectorAvailableView])
    async def available(request: Request,
                        authenticated: Authenticated = Depends(authenticator)):
        registry = getattr(request.app.state, "connector_registry", None)
        if registry is None:
            return []
        items = await run_in_threadpool(registry.available_for_tenant,
            principal=authenticated.principal)
        return [ConnectorAvailableView(**item) for item in items]

    @router.post("", response_model=ConnectorStateView, status_code=202)
    async def propose(body: ConnectorProposalBody, request: Request,
                      authenticated: Authenticated = Depends(authenticator)):
        principal = authenticated.principal
        # Placeholder material is validated only for endpoint shape and is never persisted.
        endpoint = ConnectorEndpoint(body.connector_id, principal.tenant_id,
            body.base_url, body.token_endpoint, body.client_id,
            SecretValue("x" * 32), tuple(body.scopes), frozenset(body.allowed_paths),
            Classification[body.max_classification.name], body.timeout_seconds,
            body.max_response_bytes, body.max_attempts,
            body.circuit_failure_threshold, body.circuit_cooldown_seconds)
        connector_id, status, version = await run_in_threadpool(
            request.app.state.connector_registry.propose, principal=principal,
            endpoint=endpoint, client_secret_env=body.client_secret_env)
        return ConnectorStateView(connector_id=connector_id, status=status, version=version)

    @router.post("/{connector_id}:disable", response_model=ConnectorStateView,
                 status_code=202)
    async def request_disable(connector_id: str, request: Request,
                              authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(request.app.state.connector_registry.request_disable,
            principal=authenticated.principal, connector_id=connector_id)
        return ConnectorStateView(connector_id=value[0], status=value[1], version=value[2])

    @router.get("/{connector_id}/revisions/{version}",
                response_model=ConnectorRevisionView)
    async def revision(connector_id: str, version: int, request: Request,
                       authenticated: Authenticated = Depends(authenticator)):
        row = await run_in_threadpool(request.app.state.connector_registry.read_revision,
            principal=authenticated.principal, connector_id=connector_id, version=version)
        return ConnectorRevisionView(**row)

    @router.post("/{connector_id}/revisions/{version}:review",
                 response_model=ConnectorStateView)
    async def review(connector_id: str, version: int, body: ConnectorDecisionBody,
                     request: Request,
                     authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(request.app.state.connector_registry.review,
            principal=authenticated.principal, connector_id=connector_id,
            expected_version=version, approve=body.approve, reason=body.reason)
        return ConnectorStateView(connector_id=value[0], status=value[1], version=value[2])
    return router
