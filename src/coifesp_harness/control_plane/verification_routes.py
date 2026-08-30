"""Run a configured verification or inspect its evidence; no caller-supplied PASS."""

from fastapi import APIRouter, Depends, HTTPException
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

    @router.post("/{project_id}/tasks/{task_id}:retry-verification-tools")
    async def retry_verification_tools(
        project_id: str,
        task_id: str,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await run_in_threadpool(
            service.verify_task, project_id=project_id, task_id=task_id,
            actor_id=authenticated.principal.principal_id, retry_tools=True,
        )

    @router.post("/{project_id}/tasks/{task_id}:retry-agent-review")
    async def retry_agent_review(
        project_id: str, task_id: str,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await run_in_threadpool(
            service.verify_task, project_id=project_id, task_id=task_id,
            actor_id=authenticated.principal.principal_id, retry_reviews=True,
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

    @router.get("/{project_id}/tasks/{task_id}/human-reviews")
    async def human_reviews(project_id: str, task_id: str,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        return await run_in_threadpool(service.human_reviews, project_id=project_id, task_id=task_id,
                                      actor_id=authenticated.principal.principal_id)

    @router.post("/{project_id}/tasks/{task_id}/human-reviews/{review_id}:decide")
    async def decide_human_review(project_id: str, task_id: str, review_id: str, decision: dict,
        authenticated: Authenticated = Depends(authenticator),  # noqa: B008
    ):
        if getattr(authenticated.principal, "is_service", False):
            raise HTTPException(status_code=403, detail="human review requires a human account")
        try:
            return await run_in_threadpool(service.decide_human_review, project_id=project_id,
                task_id=task_id, review_id=review_id, actor_id=authenticated.principal.principal_id,
                decision=decision)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
