from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import and_, select

from ..collaboration.governance_models import AssignmentState
from ..collaboration.service import GovernanceService
from ..errors import (
    GovernanceConflictError,
    IdempotencyConflict,
    PolicyDenied,
    ResourceNotFound,
)
from ..product.repository import TEAM_TASKS
from ..project_process.models import ProjectProcessPhase, ProjectProcessStatus
from ..project_process.repository import PROJECT_PROCESSES
from ..security import Principal
from ..team_agents.task_contract_models import validate_task_contract
from ..work_graph.repository import PROJECT_WORK_NODES, PROJECT_WORK_RELATIONS
from .models import ExecutionTask, TaskLease, TaskStatus
from .repository import SQLAlchemyTaskRepository


class TaskExecutionService:
    """Durable execution boundary for Project Work and legacy assignments."""

    def __init__(
        self,
        *,
        repository: SQLAlchemyTaskRepository,
        governance: GovernanceService,
        project_repository=None,
        work_graph_repository=None,
        allow_legacy_assignments: bool = True,
    ) -> None:
        self.repository = repository
        self.governance = governance
        self.project_repository = project_repository
        self.work_graph_repository = work_graph_repository
        self.allow_legacy_assignments = allow_legacy_assignments
        if project_repository is not None:
            engines = {id(repository.engine), id(project_repository.engine)}
            if work_graph_repository is not None:
                engines.add(id(work_graph_repository.engine))
            if len(engines) != 1:
                raise ValueError("project work execution requires one shared database engine")

    def enqueue_project_work(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        process_id: str,
        team_task_id: str,
        work_node_id: str,
        contract_version: int,
    ) -> ExecutionTask:
        """Queue immutable, version-pinned Project Work without TaskAssignment."""
        if self.project_repository is None or self.work_graph_repository is None:
            raise GovernanceConflictError("project work execution is not configured")
        if type(contract_version) is not int or contract_version < 1:
            raise ValueError("project work contract version is invalid")
        identity = hashlib.sha256(
            f"{process_id}\n{team_task_id}\n{contract_version}".encode()
        ).hexdigest()
        execution_task_id = f"project-work:{identity[:40]}"
        with self.project_repository.transaction() as connection:
            bound_execution = self.repository.using_connection(connection)
            replay = bound_execution.find_by_idempotency(
                tenant_id=principal.tenant_id,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                if (
                    replay.task_id != execution_task_id
                    or replay.process_id != process_id
                    or replay.team_task_id != team_task_id
                    or replay.work_node_id != work_node_id
                    or replay.contract_version != contract_version
                ):
                    raise IdempotencyConflict(
                        "project work idempotency key was reused with different content"
                    )
                return replay
            process, task, contract = validate_project_work_admission(
                connection,
                project_repository=self.project_repository,
                principal_team_id=principal.tenant_id,
                process_id=process_id,
                team_task_id=team_task_id,
                work_node_id=work_node_id,
                contract_version=contract_version,
                allowed_task_statuses=frozenset({"in_progress"}),
            )
            return bound_execution.enqueue(
                tenant_id=principal.tenant_id,
                actor_id=principal.principal_id,
                idempotency_key=idempotency_key,
                task_id=execution_task_id,
                queue=task["target_team_id"],
                payload={
                    "schema": "coifesp.project-work-execution.v1",
                    "project_id": process.project_id,
                    "process_id": process_id,
                    "team_task_id": team_task_id,
                    "work_node_id": work_node_id,
                    "contract_version": contract_version,
                    "contract": contract,
                },
                priority=self._project_priority(task["priority"]),
                max_attempts=3,
                project_id=process.project_id,
                process_id=process_id,
                team_task_id=team_task_id,
                work_node_id=work_node_id,
                contract_version=contract_version,
            )

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
        if not self.allow_legacy_assignments:
            raise GovernanceConflictError(
                "legacy assignment execution is disabled; use project work execution"
            )
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

    @staticmethod
    def _require_verified_dependencies(connection, *, project_id: str, work_node_id: str):
        targets = connection.execute(
            select(
                PROJECT_WORK_NODES.c.node_type,
                PROJECT_WORK_NODES.c.subject_id,
            )
            .select_from(
                PROJECT_WORK_RELATIONS.join(
                    PROJECT_WORK_NODES,
                    and_(
                        PROJECT_WORK_NODES.c.node_id
                        == PROJECT_WORK_RELATIONS.c.target_node_id,
                        PROJECT_WORK_NODES.c.project_id
                        == PROJECT_WORK_RELATIONS.c.project_id,
                    ),
                )
            )
            .where(
                and_(
                    PROJECT_WORK_RELATIONS.c.project_id == project_id,
                    PROJECT_WORK_RELATIONS.c.source_node_id == work_node_id,
                    PROJECT_WORK_RELATIONS.c.relation_type == "depends_on",
                )
            )
            .with_for_update()
        ).all()
        for node_type, subject_id in targets:
            if node_type != "task":
                raise GovernanceConflictError(
                    "project work has a non-task dependency without completion evidence"
                )
            status = connection.execute(
                select(TEAM_TASKS.c.status).where(
                    and_(
                        TEAM_TASKS.c.project_id == project_id,
                        TEAM_TASKS.c.task_id == subject_id,
                    )
                ).with_for_update()
            ).scalar_one_or_none()
            if status != "verified":
                raise GovernanceConflictError(
                    "project work dependencies are not verified"
                )

    @staticmethod
    def _project_priority(value: str) -> int:
        return {"low": -10, "normal": 0, "high": 10, "urgent": 20}.get(value, 0)


def validate_project_work_admission(
    connection,
    *,
    project_repository,
    principal_team_id: str,
    process_id: str,
    team_task_id: str,
    work_node_id: str,
    contract_version: int,
    allowed_task_statuses: frozenset[str],
):
    """Validate one authoritative Project Work Contract on the caller transaction."""
    if not allowed_task_statuses:
        raise ValueError("project work admission requires allowed task statuses")
    locked_process = connection.execute(
        select(PROJECT_PROCESSES.c.process_id)
        .where(PROJECT_PROCESSES.c.process_id == process_id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked_process is None:
        raise ResourceNotFound("project process is absent or hidden")
    process = project_repository.process(connection, process_id)
    task = (
        connection.execute(
            select(TEAM_TASKS)
            .where(
                and_(
                    TEAM_TASKS.c.task_id == team_task_id,
                    TEAM_TASKS.c.project_id == process.project_id,
                )
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if task is None:
        raise ResourceNotFound("project work is absent or hidden")
    if task["target_team_id"] != principal_team_id:
        raise PolicyDenied("only the target team may enqueue project work")
    if process.status not in {
        ProjectProcessStatus.READY,
        ProjectProcessStatus.RUNNING,
    }:
        raise GovernanceConflictError("project process cannot admit execution work")
    if process.phase is not ProjectProcessPhase.EXECUTION:
        raise GovernanceConflictError(
            "project work execution requires the EXECUTION process phase"
        )
    if task["status"] not in allowed_task_statuses:
        allowed = sorted(allowed_task_statuses)
        requirement = (
            allowed[0] if len(allowed) == 1 else "one of " + ", ".join(allowed)
        )
        raise GovernanceConflictError(
            f"project work status must be {requirement} before execution"
        )
    if task["process_id"] != process_id or task["work_node_id"] != work_node_id:
        raise GovernanceConflictError("project work process or node binding changed")
    node = (
        connection.execute(
            select(PROJECT_WORK_NODES)
            .where(
                and_(
                    PROJECT_WORK_NODES.c.node_id == work_node_id,
                    PROJECT_WORK_NODES.c.project_id == process.project_id,
                )
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if (
        node is None
        or node["node_type"] != "task"
        or node["subject_id"] != team_task_id
    ):
        raise GovernanceConflictError(
            "project work node does not identify the TeamTask"
        )
    if (
        task["source_contract_version"] != contract_version
        or task["accepted_contract_version"] != contract_version
    ):
        raise GovernanceConflictError(
            "a version-pinned accepted project work contract is required"
        )
    contract = validate_task_contract(
        requested_capability=task["requested_capability"],
        input_manifest=task["input_manifest_json"],
        output_contract=task["output_contract_json"],
        verification_policy=task["verification_policy_json"],
        autonomy_requirement=task["autonomy_requirement"],
    )
    TaskExecutionService._require_verified_dependencies(
        connection,
        project_id=process.project_id,
        work_node_id=work_node_id,
    )
    return process, task, contract
