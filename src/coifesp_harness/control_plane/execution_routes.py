from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.concurrency import run_in_threadpool

from ..errors import PolicyDenied
from ..execution import ExecutionTask, TaskExecutionService
from ..execution.repository import TaskExecutionError
from .auth import Authenticated, BearerAuthenticator
from .execution_models import (
    ExecutionEnqueueBody,
    ExecutionFailureBody,
    ExecutionLeaseBody,
    ExecutionLeaseResponse,
    ExecutionStatusResponse,
    ExecutionSuccessBody,
    ExecutionTaskResponse,
    HeartbeatBody,
    LeaseTokenBody,
    ProjectWorkExecutionEnqueueBody,
)


def build_execution_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(tags=["execution"])

    @router.post(
        "/v1/executions",
        response_model=ExecutionTaskResponse,
        status_code=201,
        deprecated=True,
    )
    async def enqueue(
        body: ExecutionEnqueueBody,
        request: Request,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionTaskResponse:
        task = await run_in_threadpool(
            _service(request).enqueue_assignment,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            task_id=body.task_id,
            program_id=body.program_id,
            assignment_id=body.assignment_id,
            queue=body.queue,
            payload=body.payload,
            dependencies=tuple(body.dependencies),
            priority=body.priority,
            max_attempts=body.max_attempts,
        )
        return _task_response(task)

    @router.post(
        "/v1/project-executions",
        response_model=ExecutionTaskResponse,
        status_code=201,
    )
    async def enqueue_project_work(
        body: ProjectWorkExecutionEnqueueBody,
        request: Request,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionTaskResponse:
        if (
            not authenticated.principal.is_service
            or "project_orchestrator" not in authenticated.principal.roles
        ):
            raise PolicyDenied(
                "project execution admission requires the project orchestrator"
            )
        task = await run_in_threadpool(
            _service(request).enqueue_project_work,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            process_id=body.process_id,
            team_task_id=body.team_task_id,
            work_node_id=body.work_node_id,
            contract_version=body.contract_version,
        )
        return _task_response(task)

    @router.get("/v1/executions/{task_id}", response_model=ExecutionTaskResponse)
    async def get_task(
        task_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionTaskResponse:
        task = await run_in_threadpool(
            _service(request).get,
            principal=authenticated.principal,
            task_id=task_id,
        )
        return _task_response(task)

    @router.post(
        "/v1/executions/{task_id}:cancel",
        response_model=ExecutionStatusResponse,
    )
    async def cancel(
        task_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionStatusResponse:
        status = await run_in_threadpool(
            _service(request).cancel,
            principal=authenticated.principal,
            task_id=task_id,
        )
        return ExecutionStatusResponse(task_id=task_id, status=status)

    @router.post(
        "/v1/worker/queues/{queue}:claim",
        response_model=ExecutionLeaseResponse,
        responses={204: {"description": "No runnable task"}},
    )
    async def claim(
        queue: str,
        body: ExecutionLeaseBody,
        request: Request,
        response: Response,
        authenticated: Authenticated = Depends(authenticator),
    ):
        lease = await run_in_threadpool(
            _service(request).claim,
            worker=authenticated.principal,
            queue=queue,
            lease_seconds=body.lease_seconds,
        )
        if lease is None:
            response.status_code = 204
            return Response(status_code=204)
        return ExecutionLeaseResponse(
            task=_task_response(lease.task),
            lease_token=lease.lease_token,
            lease_expires_at=lease.lease_expires_at,
        )

    @router.post(
        "/v1/worker/executions/{task_id}:start",
        response_model=ExecutionStatusResponse,
    )
    async def start(
        task_id: str,
        body: LeaseTokenBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionStatusResponse:
        await run_in_threadpool(
            _service(request).start,
            worker=authenticated.principal,
            task_id=task_id,
            lease_token=body.lease_token,
        )
        return ExecutionStatusResponse(task_id=task_id, status="running")

    @router.post(
        "/v1/worker/executions/{task_id}:heartbeat",
        response_model=ExecutionStatusResponse,
    )
    async def heartbeat(
        task_id: str,
        body: HeartbeatBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionStatusResponse:
        expires_at = await run_in_threadpool(
            _service(request).heartbeat,
            worker=authenticated.principal,
            task_id=task_id,
            lease_token=body.lease_token,
            lease_seconds=body.lease_seconds,
        )
        return ExecutionStatusResponse(
            task_id=task_id,
            status="running",
            lease_expires_at=expires_at,
        )

    @router.post(
        "/v1/worker/executions/{task_id}:succeed",
        response_model=ExecutionStatusResponse,
    )
    async def succeed(
        task_id: str,
        body: ExecutionSuccessBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionStatusResponse:
        await run_in_threadpool(
            _service(request).succeed,
            worker=authenticated.principal,
            task_id=task_id,
            lease_token=body.lease_token,
            result=body.result,
        )
        return ExecutionStatusResponse(task_id=task_id, status="succeeded")

    @router.post(
        "/v1/worker/executions/{task_id}:fail",
        response_model=ExecutionStatusResponse,
    )
    async def fail(
        task_id: str,
        body: ExecutionFailureBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionStatusResponse:
        status = await run_in_threadpool(
            _service(request).fail,
            worker=authenticated.principal,
            task_id=task_id,
            lease_token=body.lease_token,
            error_code=body.error_code,
            retryable=body.retryable,
            retry_delay_seconds=body.retry_delay_seconds,
        )
        return ExecutionStatusResponse(task_id=task_id, status=status)

    @router.post(
        "/v1/worker/executions/{task_id}:acknowledge-cancel",
        response_model=ExecutionStatusResponse,
    )
    async def acknowledge_cancel(
        task_id: str,
        body: LeaseTokenBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExecutionStatusResponse:
        await run_in_threadpool(
            _service(request).acknowledge_cancel,
            worker=authenticated.principal,
            task_id=task_id,
            lease_token=body.lease_token,
        )
        return ExecutionStatusResponse(task_id=task_id, status="cancelled")

    return router


def _service(request: Request) -> TaskExecutionService:
    service = getattr(request.app.state, "task_execution_service", None)
    if service is None:
        raise TaskExecutionError("task execution service is not configured")
    return service


def _task_response(task: ExecutionTask) -> ExecutionTaskResponse:
    return ExecutionTaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        program_id=task.program_id,
        assignment_id=task.assignment_id,
        project_id=task.project_id,
        process_id=task.process_id,
        team_task_id=task.team_task_id,
        work_node_id=task.work_node_id,
        contract_version=task.contract_version,
        queue=task.queue,
        status=task.status,
        priority=task.priority,
        max_attempts=task.max_attempts,
        attempt_count=task.attempt_count,
        available_at=task.available_at,
        dependencies=list(task.dependencies),
        cancel_requested=task.cancel_requested,
        result=task.result,
        error_code=task.error_code,
    )
