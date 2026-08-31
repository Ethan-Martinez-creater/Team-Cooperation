import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.security import Principal, RiskLevel
from coifesp_harness.tool_jobs import (
    DurableToolWorker,
    DurableToolWorkerRunner,
    PermanentToolError,
    SQLAlchemyToolJobRepository,
    ToolJobError,
    ToolJobKeyring,
    ToolJobStatus,
    current_tool_execution_context,
)
from coifesp_harness.tools import ToolDefinition, ToolRegistry


def runtime(handler, *, timeout=1.0, schema=None):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyToolJobRepository(
        engine=engine,
        keyring=ToolJobKeyring(master_key=b"k" * 32, key_id="test-v1"),
    )
    repository.create_schema()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="office.send_message",
            description="send a collaboration message",
            handler=handler,
            parameters_schema=schema
            or {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            risk=RiskLevel.LOW,
            timeout_seconds=timeout,
            max_output_chars=20,
        )
    )
    worker = DurableToolWorker(
        repository=repository,
        registry=registry,
        tenant_id="team-a",
        worker_id="tool-worker",
        lease_seconds=5,
        heartbeat_seconds=1,
        retry_delay_seconds=1,
    )
    return repository, worker


def enqueue(repository, *, tool_name="office.send_message", arguments=None):
    return repository.enqueue(
        tenant_id="team-a",
        actor_id="lead",
        job_id="job-1",
        run_id="run-1",
        call_id="call-1",
        tool_name=tool_name,
        idempotency_key="provider-idem-1",
        arguments=arguments or {"message": "hello"},
        max_attempts=2,
    )


@pytest.mark.asyncio
async def test_worker_executes_with_stable_connector_context_and_limits_output() -> None:
    observed = None

    async def handler(arguments):
        nonlocal observed
        observed = current_tool_execution_context()
        return "x" * 40

    repository, worker = runtime(handler)
    enqueue(repository)
    assert await worker.run_once() is True
    completed = repository.get(tenant_id="team-a", job_id="job-1", include_payloads=True)
    assert completed.status is ToolJobStatus.SUCCEEDED
    assert completed.result.startswith("x" * 20)
    assert "truncated" in completed.result
    assert observed is not None
    assert observed.idempotency_key == "provider-idem-1"


@pytest.mark.asyncio
async def test_worker_rejects_unknown_tool_and_invalid_arguments() -> None:
    async def handler(arguments):
        return arguments

    repository, worker = runtime(handler)
    enqueue(repository, tool_name="unregistered")
    assert await worker.run_once() is True
    assert repository.get(tenant_id="team-a", job_id="job-1").error_code == "unknown_tool"

    repository, worker = runtime(handler)
    enqueue(repository, arguments={"message": 123})
    assert await worker.run_once() is True
    assert repository.get(tenant_id="team-a", job_id="job-1").error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_worker_timeout_is_retryable_and_errors_are_sanitized() -> None:
    async def slow(arguments):
        await asyncio.sleep(1)

    repository, worker = runtime(slow, timeout=0.01)
    enqueue(repository)
    assert await worker.run_once() is True
    timed_out = repository.get(tenant_id="team-a", job_id="job-1")
    assert timed_out.status is ToolJobStatus.RETRY_WAIT
    assert timed_out.error_code == "tool_timeout"

    async def rejected(arguments):
        raise PermanentToolError("provider_rejected")

    repository, worker = runtime(rejected)
    enqueue(repository)
    assert await worker.run_once() is True
    failed = repository.get(tenant_id="team-a", job_id="job-1")
    assert failed.status is ToolJobStatus.FAILED
    assert failed.error_code == "provider_rejected"


@pytest.mark.asyncio
async def test_idle_worker_returns_without_claim() -> None:
    async def handler(arguments):
        return arguments

    _, worker = runtime(handler)
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_expired_preparation_lease_does_not_stop_worker_or_execute_handler():
    from datetime import UTC, datetime, timedelta

    from coifesp_harness.tool_jobs.repository import TOOL_JOBS

    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return "ok"

    repository, worker = runtime(handler)
    enqueue(repository)

    class SlowPreparation:
        def prepare(self, **kwargs):
            with repository.engine.begin() as connection:
                connection.execute(TOOL_JOBS.update().values(
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))

    worker.workspace_manager = SlowPreparation()
    assert await worker.run_once()
    assert calls == []
    assert repository.get(tenant_id="team-a", job_id="job-1").status is ToolJobStatus.LEASED
    repository.recover_expired(tenant_id="team-a", actor_id="recovery", retry_delay_seconds=1)
    with repository.engine.begin() as connection:
        connection.execute(TOOL_JOBS.update().values(available_at=datetime.now(UTC) - timedelta(seconds=1)))
    worker.workspace_manager = None
    assert await worker.run_once()
    assert calls == [{"message": "hello"}]
    assert repository.get(tenant_id="team-a", job_id="job-1").status is ToolJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_worker_prepares_job_scoped_workspace_before_handler() -> None:
    prepared = []
    executed = []

    async def handler(arguments):
        executed.append(arguments)
        return arguments

    repository, base = runtime(handler)
    enqueue(repository)

    class Workspaces:
        def prepare(self, **values):
            prepared.append(values)

    worker = DurableToolWorker(
        repository=repository,
        registry=base.registry,
        tenant_id="team-a",
        worker_id="tool-worker",
        lease_seconds=5,
        heartbeat_seconds=1,
        workspace_manager=Workspaces(),
    )
    assert await worker.run_once()
    assert prepared == [{"tenant_id": "team-a", "job_id": "job-1"}]
    assert executed == [{"message": "hello"}]


@pytest.mark.asyncio
async def test_runner_revalidates_dedicated_identity_and_rejects_agent_worker_role() -> None:
    async def handler(arguments):
        return arguments

    repository, worker = runtime(handler)

    class Identity:
        def __init__(self, principal):
            self.principal = principal
            self.calls = 0

        async def resolve(self):
            self.calls += 1
            return self.principal

    class Reconciler:
        def reconcile(self, **_):
            return 0

    identity = Identity(
        Principal(
            "tool-service",
            "team-a",
            roles=frozenset({"tool_worker"}),
            is_service=True,
        )
    )
    runner = DurableToolWorkerRunner(
        repository=repository,
        registry=worker.registry,
        reconciler=Reconciler(),
        identity_provider=identity,
        tenant_id="team-a",
        idle_poll_seconds=0.05,
        lease_seconds=5,
        heartbeat_seconds=1,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop=stop))
    while identity.calls < 2:
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert identity.calls >= 2

    bad = Identity(
        Principal(
            "agent-service",
            "team-a",
            roles=frozenset({"agent_worker"}),
            is_service=True,
        )
    )
    runner = DurableToolWorkerRunner(
        repository=repository,
        registry=worker.registry,
        reconciler=Reconciler(),
        identity_provider=bad,
        tenant_id="team-a",
        lease_seconds=5,
        heartbeat_seconds=1,
    )
    with pytest.raises(ToolJobError, match="identity"):
        await runner.run(stop=asyncio.Event())
