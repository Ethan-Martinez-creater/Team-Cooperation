from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from ..capabilities import CapabilityDirectoryService, TeamCapability
from ..errors import GovernanceError
from ..security import Classification
from .auth import Authenticated, BearerAuthenticator
from .capability_models import (CapabilityCapacityBody, CapabilityCapacityView,
    CapabilityMatchBody, CapabilityMatchView, CapabilityPublishBody,
    CapabilityPublishResponse, CapabilityView, CapacityReservationBody,
    CapacityReservationView, CapacityNegotiationBody,
    CapacityNegotiationDecisionBody, CapacityNegotiationView)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128,
                                        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]


def build_capability_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/capabilities", tags=["capabilities"])

    @router.post("", response_model=CapabilityPublishResponse, status_code=201)
    async def publish(body: CapabilityPublishBody, request: Request, response: Response,
                      idempotency_key: IdempotencyKey,
                      authenticated: Authenticated = Depends(authenticator)) -> CapabilityPublishResponse:
        result = await run_in_threadpool(_service(request).publish,
            principal=authenticated.principal, idempotency_key=idempotency_key,
            capability_id=body.capability_id, version=body.version, name=body.name,
            description=body.description, tags=tuple(body.tags), protocols=tuple(body.protocols),
            input_contract=body.input_contract, output_contract=body.output_contract,
            max_input_classification=Classification[body.max_input_classification.name],
            required_compartments=tuple(body.required_compartments),
            residency_regions=tuple(body.residency_regions),
            visible_to_tenants=tuple(body.visible_to_tenants))
        if result.duplicate:
            response.status_code = 200
        response.headers["ETag"] = f'"{result.capability.content_digest}"'
        return CapabilityPublishResponse(capability=_view(result.capability), duplicate=result.duplicate)

    @router.get("", response_model=list[CapabilityView])
    async def discover(request: Request, protocol: str | None = Query(default=None, max_length=64),
                       tag: str | None = Query(default=None, max_length=64),
                       limit: int = Query(default=100, ge=1, le=200),
                       authenticated: Authenticated = Depends(authenticator)) -> list[CapabilityView]:
        values = await run_in_threadpool(_service(request).discover,
            principal=authenticated.principal, protocol=protocol, tag=tag, limit=limit)
        return [_view(item) for item in values]

    @router.put("/{provider_tenant_id}/{capability_id}/{version}/capacity",
                response_model=CapabilityCapacityView)
    async def declare_capacity(provider_tenant_id: str, capability_id: str, version: str,
                               body: CapabilityCapacityBody, request: Request,
                               authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_service(request).declare_capacity,
            principal=authenticated.principal, provider_tenant_id=provider_tenant_id,
            capability_id=capability_id, version=version, status=body.status,
            available_slots=body.available_slots, valid_until=body.valid_until,
            expected_version=body.expected_version)
        return CapabilityCapacityView(status=value.status,
            available_slots=value.available_slots, valid_until=value.valid_until,
            state_version=value.state_version)

    @router.post("/-/match", response_model=list[CapabilityMatchView])
    async def match(body: CapabilityMatchBody, request: Request,
                    authenticated: Authenticated = Depends(authenticator)):
        values = await run_in_threadpool(_service(request).match,
            principal=authenticated.principal, required_tags=tuple(body.required_tags),
            protocol=body.protocol,
            input_classification=Classification[body.input_classification.name],
            compartments=tuple(body.compartments),
            residency_regions=tuple(body.residency_regions), limit=body.limit)
        return [CapabilityMatchView(capability=_view(item.capability),
            capacity=CapabilityCapacityView(status=item.capacity.status,
                available_slots=item.capacity.available_slots,
                valid_until=item.capacity.valid_until,
                state_version=item.capacity.state_version),
            score=item.score, reasons=list(item.reasons)) for item in values]

    @router.post("/-/reservations", response_model=CapacityReservationView,
                 status_code=201)
    async def reserve(body: CapacityReservationBody, request: Request,
                      authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_service(request).reserve,
            principal=authenticated.principal, reservation_id=body.reservation_id,
            provider_tenant_id=body.provider_tenant_id,
            capability_id=body.capability_id, version=body.version,
            slots=body.slots, expires_at=body.expires_at)
        return _reservation_view(value)

    @router.post("/{provider_tenant_id}/reservations/{reservation_id}:release",
                 response_model=CapacityReservationView)
    async def release(provider_tenant_id: str, reservation_id: str, request: Request,
                      authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_service(request).release_reservation,
            principal=authenticated.principal, provider_tenant_id=provider_tenant_id,
            reservation_id=reservation_id)
        return _reservation_view(value)

    @router.post("/-/negotiations", response_model=CapacityNegotiationView,
                 status_code=201)
    async def propose_negotiation(body: CapacityNegotiationBody, request: Request,
                                  authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_service(request).propose_negotiation,
            principal=authenticated.principal, negotiation_id=body.negotiation_id,
            provider_tenant_id=body.provider_tenant_id,
            capability_id=body.capability_id, version=body.version,
            requested_slots=body.requested_slots, earliest_start=body.earliest_start,
            latest_end=body.latest_end, reason=body.reason)
        return _negotiation_view(value)

    @router.post("/{provider_tenant_id}/negotiations/{negotiation_id}:decide",
                 response_model=CapacityNegotiationView)
    async def decide_negotiation(provider_tenant_id: str, negotiation_id: str,
                                 body: CapacityNegotiationDecisionBody,
                                 request: Request,
                                 authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_service(request).decide_negotiation,
            principal=authenticated.principal, provider_tenant_id=provider_tenant_id,
            negotiation_id=negotiation_id, expected_version=body.expected_version,
            decision=body.decision, reason=body.reason)
        return _negotiation_view(value)
    return router


def _service(request: Request) -> CapabilityDirectoryService:
    service = getattr(request.app.state, "capability_service", None)
    if service is None:
        raise GovernanceError("capability directory is not configured")
    return service


def _view(item: TeamCapability) -> CapabilityView:
    return CapabilityView(capability_id=item.capability_id, provider_tenant_id=item.provider_tenant_id,
        version=item.version, name=item.name, description=item.description, tags=list(item.tags),
        protocols=list(item.protocols), input_contract=item.input_contract,
        output_contract=item.output_contract,
        max_input_classification=item.max_input_classification.name.lower(),
        required_compartments=list(item.required_compartments),
        residency_regions=list(item.residency_regions), content_digest=item.content_digest,
        published_at=item.published_at.isoformat())


def _reservation_view(item) -> CapacityReservationView:
    return CapacityReservationView(reservation_id=item.reservation_id,
        provider_tenant_id=item.provider_tenant_id, capability_id=item.capability_id,
        version=item.version, consumer_tenant_id=item.consumer_tenant_id,
        slots=item.slots, status=item.status, expires_at=item.expires_at)


def _negotiation_view(item) -> CapacityNegotiationView:
    return CapacityNegotiationView(negotiation_id=item.negotiation_id,
        provider_tenant_id=item.provider_tenant_id,
        consumer_tenant_id=item.consumer_tenant_id,
        capability_id=item.capability_id, version=item.version,
        requested_slots=item.requested_slots, earliest_start=item.earliest_start,
        latest_end=item.latest_end, status=item.status,
        state_version=item.state_version)
