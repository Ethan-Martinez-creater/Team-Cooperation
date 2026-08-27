from __future__ import annotations

from typing import Any

from ..collaboration.governance_models import AssignmentState
from ..collaboration.service import GovernanceService
from ..errors import PolicyDenied, ResourceNotFound
from ..security import Principal
from .models import ExecutionTask, TaskLease, TaskStatus
from .repository import SQLAlchemyTaskRepository


class TaskExecutionService:
    """Authenticated boundary between governance assignments and worker leases."""

    def __init__(
        self,
        *,
        repository: SQLAlchemyTaskRepository,
        governance: GovernanceService,
    ) -> None:
        self.repository = repository
        self.governance = governance

    def enqueue_assignment(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        task_id: str,
        program_id: str,
        assignment_id: str,
        queue: str,
        payload: dict[str, Any],
        dependencies: tuple[str, ...] = (),
        priority: int = 0,
        max_attempts: int = 3,
    ) -> ExecutionTask:
        board = self.governance.read_program(principal=principal, program_id=program_id)
        assignment = board.assignments.get(assignment_id)
        if assignment is None or principal.tenant_id not in assignment.visible_to_tenants:
            raise ResourceNotFound("assignment is absent or hidden")
        if assignment.assignee_id != principal.principal_id:
            raise PolicyDenied("only the governance assignee may enqueue execution")
        if assignment.state is not AssignmentState.IN_PROGRESS:
            raise PolicyDenied("assignment must be in progress before execution is enqueued")
        return self.repository.enqueue(
            tenant_id=principal.tenant_id,
            actor_id=principal.principal_id,
            idempotency_key=idempotency_key,
            task_id=task_id,
            program_id=program_id,
            assignment_id=assignment_id,
            queue=queue,
            payload=payload,
            dependencies=dependencies,
            priority=priority,
            max_attempts=max_attempts,
        )

    def get(self, *, principal: Principal, task_id: str) -> ExecutionTask:
        task = self.repository.get(tenant_id=principal.tenant_id, task_id=task_id)
        if task.created_by != principal.principal_id and not principal.roles.intersection(
            {"execution_controller", "platform_administrator"}
        ):
            raise ResourceNotFound("execution task is absent or hidden")
        return task

    def cancel(self, *, principal: Principal, task_id: str) -> TaskStatus:
        self.get(principal=principal, task_id=task_id)
        return self.repository.request_cancel(
            tenant_id=principal.tenant_id,
            task_id=task_id,
            actor_id=principal.principal_id,
        )

    def claim(
        self,
        *,
        worker: Principal,
        queue: str,
        lease_seconds: int = 60,
    ) -> TaskLease | None:
        self._require_worker(worker)
        return self.repository.claim_next(
            tenant_id=worker.tenant_id,
            queue=queue,
            worker_id=worker.principal_id,
            lease_seconds=lease_seconds,
        )

    def start(self, *, worker: Principal, task_id: str, lease_token: str) -> None:
        self._require_worker(worker)
        self.repository.start(
            tenant_id=worker.tenant_id,
            task_id=task_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
        )

    def heartbeat(
        self,
        *,
        worker: Principal,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ):
        self._require_worker(worker)
        return self.repository.heartbeat(
            tenant_id=worker.tenant_id,
            task_id=task_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
        )

    def succeed(
        self,
        *,
        worker: Principal,
        task_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> None:
        self._require_worker(worker)
        self.repository.succeed(
            tenant_id=worker.tenant_id,
            task_id=task_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            result=result,
        )

    def fail(
        self,
        *,
        worker: Principal,
        task_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: int = 30,
    ) -> TaskStatus:
        self._require_worker(worker)
        return self.repository.fail(
            tenant_id=worker.tenant_id,
            task_id=task_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            error_code=error_code,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )

    def acknowledge_cancel(
        self,
        *,
        worker: Principal,
        task_id: str,
        lease_token: str,
    ) -> None:
        self._require_worker(worker)
        self.repository.acknowledge_cancel(
            tenant_id=worker.tenant_id,
            task_id=task_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
        )

    @staticmethod
    def _require_worker(principal: Principal) -> None:
        if not principal.is_service or "execution_worker" not in principal.roles:
            raise PolicyDenied("execution lease operations require a worker service identity")
