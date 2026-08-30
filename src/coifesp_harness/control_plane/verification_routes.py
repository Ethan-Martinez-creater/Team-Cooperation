"""Run a configured verification or inspect its evidence; no caller-supplied PASS."""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from .auth import Authenticated


def build_task_verification_router(*, authenticator, service):
    router = APIRouter(prefix="/v1/projects", tags=["verification"])

    @router.post("/{project_id}/tasks/{task_id}:verify")
    async def verify_task(
        project_id: str,
        task_id: str,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await run_in_threadpool(
            service.verify_task,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.get("/{project_id}/tasks/{task_id}/verifications")
    async def verifications(
        project_id: str,
        task_id: str,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await run_in_threadpool(
            service.results,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
        )

    return router
