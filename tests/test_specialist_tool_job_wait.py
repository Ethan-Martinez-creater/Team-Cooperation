import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.security import RiskLevel
from coifesp_harness.tool_jobs import (
    AwaitingSpecialistTool,
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolJobError,
    ToolJobKeyring,
    ToolJobStatus,
)
from coifesp_harness.tools import ToolDefinition, ToolRegistry


def _repository():
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


def _enqueue(repository, *, tool_name="specialist.delegate"):
    return repository.enqueue(
        tenant_id="team-a",
        actor_id="team-agent:team-a",
        job_id="job-specialist",
        run_id="run-parent",
        call_id="call-specialist",
        tool_name=tool_name,
        idempotency_key="run-parent:call-specialist",
        arguments={"kind": "code_review"},
        max_attempts=2,
    )


def _start(repository):
    lease = repository.claim_next(
        tenant_id="team-a", worker_id="tool-worker", lease_seconds=30
    )
    assert lease is not None
    repository.start(
        tenant_id="team-a",
        job_id=lease.job.job_id,
        worker_id="tool-worker",
        lease_token=lease.lease_token,
    )
    return lease


def test_repository_suspends_without_spending_another_attempt_or_retaining_lease():
    repository = _repository()
    _enqueue(repository)
    lease = _start(repository)
    repository.await_specialist(
        tenant_id="team-a",
        job_id="job-specialist",
        worker_id="tool-worker",
        lease_token=lease.lease_token,
        delegation_id="delegation-1",
    )

    waiting = repository.get(tenant_id="team-a", job_id="job-specialist")
    assert waiting.status is ToolJobStatus.AWAITING_SPECIALIST
    assert waiting.attempt_count == 1
    assert waiting.error_code == "delegation-1"
    assert repository.claim_next(tenant_id="team-a", worker_id="other") is None
    assert repository.recover_expired(tenant_id="team-a", actor_id="reaper") == 0


def test_only_specialist_tool_and_matching_delegation_can_use_wait_lifecycle():
    repository = _repository()
    _enqueue(repository, tool_name="project.publish_artifact")
    lease = _start(repository)
    with pytest.raises(ToolJobError, match="only the specialist"):
        repository.await_specialist(
            tenant_id="team-a",
            job_id="job-specialist",
            worker_id="tool-worker",
            lease_token=lease.lease_token,
            delegation_id="delegation-1",
        )

    repository = _repository()
    _enqueue(repository)
    lease = _start(repository)
    repository.await_specialist(
        tenant_id="team-a",
        job_id="job-specialist",
        worker_id="tool-worker",
        lease_token=lease.lease_token,
        delegation_id="delegation-1",
    )
    with pytest.raises(ToolJobError, match="not awaiting this"):
        repository.complete_specialist(
            tenant_id="team-a",
            job_id="job-specialist",
            delegation_id="delegation-2",
            actor_id="specialist-agent:team-a:code_review",
            result={"verdict": "pass"},
        )


def test_specialist_completion_is_encrypted_idempotent_and_conflicts_fail_closed():
    repository = _repository()
    _enqueue(repository)
    lease = _start(repository)
    repository.await_specialist(
        tenant_id="team-a",
        job_id="job-specialist",
        worker_id="tool-worker",
        lease_token=lease.lease_token,
        delegation_id="delegation-1",
    )
    result = {"schema": "coifesp.specialist-result.v1", "verdict": "pass"}
    completion = {
        "tenant_id": "team-a",
        "job_id": "job-specialist",
        "delegation_id": "delegation-1",
        "actor_id": "specialist-agent:team-a:code_review",
        "result": result,
    }
    repository.complete_specialist(**completion)
    repository.complete_specialist(**completion)
    completed = repository.get(
        tenant_id="team-a", job_id="job-specialist", include_payloads=True
    )
    assert completed.status is ToolJobStatus.SUCCEEDED
    assert completed.result == result
    with pytest.raises(ToolJobError, match="conflicts"):
        repository.complete_specialist(
            tenant_id="team-a",
            job_id="job-specialist",
            delegation_id="delegation-1",
            actor_id="specialist-agent:team-a:code_review",
            error_code="invalid_specialist_output",
        )


@pytest.mark.asyncio
async def test_worker_converts_specialist_signal_into_durable_wait_state():
    repository = _repository()
    registry = ToolRegistry()

    async def handler(_arguments):
        raise AwaitingSpecialistTool("delegation-1")

    registry.register(
        ToolDefinition(
            name="specialist.delegate",
            description="bounded specialist",
            handler=handler,
            parameters_schema={"type": "object"},
            risk=RiskLevel.LOW,
        )
    )
    worker = DurableToolWorker(
        repository=repository,
        registry=registry,
        tenant_id="team-a",
        worker_id="tool-worker",
        lease_seconds=5,
        heartbeat_seconds=1,
    )
    _enqueue(repository)

    assert await worker.run_once()
    waiting = repository.get(tenant_id="team-a", job_id="job-specialist")
    assert waiting.status is ToolJobStatus.AWAITING_SPECIALIST
    assert waiting.attempt_count == 1


def test_wait_signal_rejects_unbounded_identifiers():
    with pytest.raises(ValueError, match="identifier"):
        AwaitingSpecialistTool("bad id")
