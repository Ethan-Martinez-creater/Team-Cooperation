"""Existing human-account authentication for delivery decisions, never model PASS flags."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from .auth import Authenticated


def build_delivery_router(*, authenticator, service):
    router = APIRouter(prefix="/v1/projects/{project_id}/processes/{process_id}", tags=["delivery"])

    async def invoke(method, authenticated, **kwargs):
        if authenticated.principal.is_service:
            raise HTTPException(status_code=403, detail="delivery decisions require a human account")
        try:
            return await run_in_threadpool(method, actor_id=authenticated.principal.principal_id, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/deliveries")
    async def list_deliveries(project_id: str, process_id: str,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await invoke(service.list_deliveries, authenticated, project_id=project_id, process_id=process_id)

    @router.post("/completion-contracts")
    async def propose_contract(project_id: str, process_id: str, payload: dict,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await invoke(service.propose_contract, authenticated, project_id=project_id,
            process_id=process_id, payload=payload)

    @router.post("/completion-contracts/{contract_id}:approve")
    async def approve_contract(project_id: str, process_id: str, contract_id: str, payload: dict,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await invoke(service.approve_contract, authenticated, project_id=project_id,
            process_id=process_id, contract_id=contract_id, payload=payload)

    @router.post("/deliveries:prepare")
    async def prepare_delivery(project_id: str, process_id: str, payload: dict,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        if set(payload) != {"expected_process_version"}:
            raise HTTPException(status_code=422, detail="expected_process_version is the only preparation field")
        return await invoke(service.prepare_delivery, authenticated, project_id=project_id,
            process_id=process_id, expected_process_version=payload["expected_process_version"])

    @router.post("/deliveries/{delivery_id}:decide")
    async def decide_delivery(project_id: str, process_id: str, delivery_id: str, payload: dict,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await invoke(service.decide, authenticated, project_id=project_id, process_id=process_id,
            delivery_id=delivery_id, payload=payload)

    return router
