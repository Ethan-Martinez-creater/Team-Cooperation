from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import IdempotencyConflict
from coifesp_harness.execution import SQLAlchemyTaskRepository, TaskStatus
from coifesp_harness.execution.repository import EXECUTION_TASKS, TaskExecutionError


def repository() -> SQLAlchemyTaskRepository:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    value = SQLAlchemyTaskRepository(engine=engine)
    value.create_schema()
    return value


def enqueue(
    repository: SQLAlchemyTaskRepository,
    task_id: str,
    *,
    tenant_id: str = "team-a",
    dependencies: tuple[str, ...] = (),
    max_attempts: int = 3,
):
    return repository.enqueue(
        tenant_id=tenant_id,
        actor_id="lead-a",
        idempotency_key=f"idem-{task_id}",
        task_id=task_id,
        queue="agent-work",
        payload={"operation": task_id},
        dependencies=dependencies,
        max_attempts=max_attempts,
    )


def test_enqueue_is_idempotent_and_tenant_scoped() -> None:
    repo = repository()
    first = enqueue(repo, "task-1")
    duplicate = enqueue(repo, "task-1")
    assert first.request_digest == duplicate.request_digest

    with pytest.raises(IdempotencyConflict):
        repo.enqueue(
            tenant_id="team-a",
            actor_id="lead-a",
            idempotency_key="idem-task-1",
            task_id="task-1",
            queue="agent-work",
            payload={"operation": "changed"},
        )
    with pytest.raises(TaskExecutionError, match="absent or hidden"):
        repo.get(tenant_id="team-b", task_id="task-1")


def test_dag_dependency_gates_claim_until_predecessor_succeeds() -> None:
    repo = repository()
    enqueue(repo, "build")
    enqueue(repo, "test", dependencies=("build",))

    first = repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-1")
    assert first is not None and first.task.task_id == "build"
    assert repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-2") is None
    repo.start(
        tenant_id="team-a",
        task_id="build",
        worker_id="worker-1",
        lease_token=first.lease_token,
    )
    repo.succeed(
        tenant_id="team-a",
        task_id="build",
        worker_id="worker-1",
        lease_token=first.lease_token,
        result={"artifact_ref": "git:commit:abc"},
    )
    second = repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-2")
    assert second is not None and second.task.task_id == "test"


def test_fencing_token_prevents_stale_worker_completion() -> None:
    repo = repository()
    enqueue(repo, "task-1")
    lease = repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-1")
    assert lease is not None
    with pytest.raises(TaskExecutionError, match="invalid or expired"):
        repo.start(
            tenant_id="team-a",
            task_id="task-1",
            worker_id="worker-1",
            lease_token="stale-token",
        )
    repo.start(
        tenant_id="team-a",
        task_id="task-1",
        worker_id="worker-1",
        lease_token=lease.lease_token,
    )
    repo.request_cancel(tenant_id="team-a", task_id="task-1", actor_id="lead-a")
    repo.acknowledge_cancel(
        tenant_id="team-a",
        task_id="task-1",
        worker_id="worker-1",
        lease_token=lease.lease_token,
    )
    assert repo.get(tenant_id="team-a", task_id="task-1").status is TaskStatus.CANCELLED


def test_retry_budget_and_dependency_failure_propagation() -> None:
    repo = repository()
    enqueue(repo, "producer", max_attempts=1)
    enqueue(repo, "consumer", dependencies=("producer",))
    lease = repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-1")
    assert lease is not None
    repo.start(
        tenant_id="team-a",
        task_id="producer",
        worker_id="worker-1",
        lease_token=lease.lease_token,
    )
    target = repo.fail(
        tenant_id="team-a",
        task_id="producer",
        worker_id="worker-1",
        lease_token=lease.lease_token,
        error_code="contract_failed",
        retryable=True,
    )
    assert target is TaskStatus.FAILED
    assert repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-2") is None
    consumer = repo.get(tenant_id="team-a", task_id="consumer")
    assert consumer.status is TaskStatus.CANCELLED
    assert consumer.error_code == "dependency_terminal_failure"


def test_expired_lease_is_recovered_and_reissued_with_new_token() -> None:
    repo = repository()
    enqueue(repo, "task-1", max_attempts=2)
    original = repo.claim_next(
        tenant_id="team-a",
        queue="agent-work",
        worker_id="worker-1",
        lease_seconds=5,
    )
    assert original is not None
    with repo.engine.begin() as connection:
        connection.execute(
            update(EXECUTION_TASKS)
            .where(EXECUTION_TASKS.c.task_id == "task-1")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert repo.recover_expired(tenant_id="team-a") == 1
    recovered = repo.claim_next(tenant_id="team-a", queue="agent-work", worker_id="worker-2")
    assert recovered is not None
    assert recovered.lease_token != original.lease_token
    assert recovered.task.attempt_count == 2
