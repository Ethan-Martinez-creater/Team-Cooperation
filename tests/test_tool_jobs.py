from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import IdempotencyConflict, IntegrityError
from coifesp_harness.tool_jobs import (
    TOOL_JOBS,
    SQLAlchemyToolJobRepository,
    ToolJobError,
    ToolJobKeyring,
    ToolJobStatus,
)


def repository():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    value = SQLAlchemyToolJobRepository(
        engine=engine,
        keyring=ToolJobKeyring(master_key=b"k" * 32, key_id="test-v1"),
    )
    value.create_schema()
    return value


def enqueue(value, **overrides):
    request = {
        "tenant_id": "team-a",
        "actor_id": "alice",
        "job_id": "job-1",
        "run_id": "run-1",
        "call_id": "call-1",
        "tool_name": "send_email",
        "idempotency_key": "idem-1",
        "arguments": {"to": "private@example.test", "body": "classified draft"},
        "max_attempts": 2,
    }
    request.update(overrides)
    return value.enqueue(**request)


def test_tool_job_encrypts_arguments_and_results_and_is_idempotent() -> None:
    value = repository()
    created = enqueue(value)
    duplicate = enqueue(value)
    with value.engine.connect() as connection:
        row = connection.execute(select(TOOL_JOBS)).mappings().one()

    assert created.status is ToolJobStatus.QUEUED
    assert duplicate.job_id == created.job_id
    assert created.arguments is None
    assert b"private@example.test" not in bytes(row["arguments_ciphertext"])

    lease = value.claim_next(tenant_id="team-a", worker_id="tool-worker", lease_seconds=30)
    assert lease is not None
    assert lease.job.arguments == {
        "to": "private@example.test",
        "body": "classified draft",
    }
    value.start(
        tenant_id="team-a",
        job_id="job-1",
        worker_id="tool-worker",
        lease_token=lease.lease_token,
    )
    value.succeed(
        tenant_id="team-a",
        job_id="job-1",
        worker_id="tool-worker",
        lease_token=lease.lease_token,
        result={"provider_message_id": "secret-result-id"},
    )
    completed = value.get(tenant_id="team-a", job_id="job-1", include_payloads=True)
    with value.engine.connect() as connection:
        row = connection.execute(select(TOOL_JOBS)).mappings().one()
    assert completed.status is ToolJobStatus.SUCCEEDED
    assert completed.result == {"provider_message_id": "secret-result-id"}
    assert b"secret-result-id" not in bytes(row["result_ciphertext"])


def test_tool_job_idempotency_conflict_and_cross_tenant_hiding() -> None:
    value = repository()
    enqueue(value)
    with pytest.raises(IdempotencyConflict):
        enqueue(value, arguments={"to": "different@example.test"})
    with pytest.raises(ToolJobError, match="absent or hidden"):
        value.get(tenant_id="team-b", job_id="job-1")

    # A model call has exactly one durable execution identity even if a caller
    # accidentally generates a different idempotency key or job identifier.
    with pytest.raises(IdempotencyConflict):
        enqueue(value, job_id="job-2", idempotency_key="idem-2")


def test_tool_job_fencing_and_retry_budget_are_enforced() -> None:
    value = repository()
    enqueue(value)
    first = value.claim_next(tenant_id="team-a", worker_id="worker-1", lease_seconds=30)
    assert first is not None
    value.start(
        tenant_id="team-a",
        job_id="job-1",
        worker_id="worker-1",
        lease_token=first.lease_token,
    )
    assert (
        value.fail(
            tenant_id="team-a",
            job_id="job-1",
            worker_id="worker-1",
            lease_token=first.lease_token,
            error_code="provider_timeout",
            retryable=True,
            retry_delay_seconds=1,
        )
        is ToolJobStatus.RETRY_WAIT
    )
    with value.engine.begin() as connection:
        connection.execute(
            update(TOOL_JOBS)
            .where(TOOL_JOBS.c.job_id == "job-1")
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = value.claim_next(tenant_id="team-a", worker_id="worker-2", lease_seconds=30)
    assert second is not None
    with pytest.raises(ToolJobError, match="invalid or expired"):
        value.start(
            tenant_id="team-a",
            job_id="job-1",
            worker_id="worker-1",
            lease_token=first.lease_token,
        )
    value.start(
        tenant_id="team-a",
        job_id="job-1",
        worker_id="worker-2",
        lease_token=second.lease_token,
    )
    assert (
        value.fail(
            tenant_id="team-a",
            job_id="job-1",
            worker_id="worker-2",
            lease_token=second.lease_token,
            error_code="provider_timeout",
            retryable=True,
        )
        is ToolJobStatus.FAILED
    )


def test_tool_job_ciphertext_tampering_fails_closed() -> None:
    value = repository()
    enqueue(value)
    with value.engine.begin() as connection:
        row = connection.execute(select(TOOL_JOBS)).mappings().one()
        altered = bytearray(row["arguments_ciphertext"])
        altered[0] ^= 1
        connection.execute(update(TOOL_JOBS).values(arguments_ciphertext=bytes(altered)))
    with pytest.raises(IntegrityError, match="authentication failed"):
        value.get(tenant_id="team-a", job_id="job-1", include_payloads=True)


def test_expired_lease_is_recovered_and_old_worker_is_fenced() -> None:
    value = repository()
    enqueue(value)
    old = value.claim_next(tenant_id="team-a", worker_id="worker-1", lease_seconds=30)
    assert old is not None
    value.start(
        tenant_id="team-a",
        job_id="job-1",
        worker_id="worker-1",
        lease_token=old.lease_token,
    )
    with value.engine.begin() as connection:
        connection.execute(
            update(TOOL_JOBS)
            .where(TOOL_JOBS.c.job_id == "job-1")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert (
        value.recover_expired(tenant_id="team-a", actor_id="tool-reaper", retry_delay_seconds=1)
        == 1
    )
    recovered = value.get(tenant_id="team-a", job_id="job-1")
    assert recovered.status is ToolJobStatus.RETRY_WAIT
    assert recovered.error_code == "lease_expired"
    with pytest.raises(ToolJobError, match="invalid or expired"):
        value.succeed(
            tenant_id="team-a",
            job_id="job-1",
            worker_id="worker-1",
            lease_token=old.lease_token,
            result={"late": True},
        )


def test_tool_job_rejects_unbounded_retry_delay() -> None:
    value = repository()
    enqueue(value)
    lease = value.claim_next(tenant_id="team-a", worker_id="worker-1", lease_seconds=30)
    assert lease is not None
    value.start(
        tenant_id="team-a",
        job_id="job-1",
        worker_id="worker-1",
        lease_token=lease.lease_token,
    )
    with pytest.raises(ToolJobError, match="retry delay"):
        value.fail(
            tenant_id="team-a",
            job_id="job-1",
            worker_id="worker-1",
            lease_token=lease.lease_token,
            error_code="timeout",
            retryable=True,
            retry_delay_seconds=0,
        )
