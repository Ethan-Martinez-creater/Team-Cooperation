import asyncio

from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import (
    AGENT_RUNS,
    AgentCheckpointKeyring,
    AgentRunCheckpointCodec,
    AgentRunService,
    AgentWorkerOutcomeStatus,
    DurableAgentWorker,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.runtime import (
    AgentLoop,
    AgentRunRequest,
    AuthorizedTool,
    LLMResponse,
    Message,
    ToolAuthorization,
    ToolCall,
)
from coifesp_harness.security import PolicyEngine, Principal, RiskLevel
from coifesp_harness.tool_jobs import (
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolBatchCoordinator,
    ToolJobKeyring,
    ToolJobStatus,
)
from coifesp_harness.tools import ToolDefinition, ToolExecutor, ToolRegistry


class Provider:
    def __init__(self):
        self.calls = 0
        self.received = None

    async def complete(self, *, messages, **_):
        self.calls += 1
        self.received = messages
        if self.calls == 1:
            return LLMResponse(
                tool_calls=(
                    ToolCall("call-a", "office.echo", {"value": "alpha"}),
                    ToolCall("call-b", "office.echo", {"value": "beta"}),
                ),
                input_tokens=4,
                output_tokens=2,
            )
        return LLMResponse(text="all tools complete", input_tokens=5, output_tokens=3)


class Resolver:
    def __init__(self, owner):
        self.owner = owner

    async def resolve(self, **_):
        return self.owner


def stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    runs = SQLAlchemyAgentRunRepository(
        engine=engine,
        keyring=AgentCheckpointKeyring(master_key=b"a" * 32, key_id="test-v1"),
    )
    jobs = SQLAlchemyToolJobRepository(
        engine=engine,
        keyring=ToolJobKeyring(master_key=b"t" * 32, key_id="test-v1"),
    )
    runs.create_schema()
    jobs.create_schema()
    registry = ToolRegistry()
    calls = []

    async def echo(arguments):
        calls.append(arguments["value"])
        return {"echo": arguments["value"]}

    registry.register(
        ToolDefinition(
            name="office.echo",
            description="echo a value",
            handler=echo,
            parameters_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            risk=RiskLevel.LOW,
        )
    )
    provider = Provider()
    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
        audit=audit,
        durable_tools=True,
    )
    coordinator = ToolBatchCoordinator(engine=engine, agent_runs=runs, tool_jobs=jobs)
    return engine, runs, jobs, registry, provider, loop, coordinator, calls


def identities():
    owner = Principal("alice", "team-a")
    worker = Principal("agent-worker", "team-a", roles=frozenset({"agent_worker"}), is_service=True)
    return owner, worker


def create_run(runs, owner):
    request = AgentRunRequest(
        run_id="run-tools",
        correlation_id="corr-tools",
        principal=owner,
        messages=(Message("user", "use both tools"),),
        tool_authorization=ToolAuthorization(
            tools=(AuthorizedTool("office.echo", "1", "test-schema"),)
        ),
    )
    AgentRunService(runs).create(
        principal=owner,
        run_id=request.run_id,
        correlation_id=request.correlation_id,
        idempotency_key="idem-run-tools",
        checkpoint=AgentRunCheckpointCodec().initial(request),
    )


def test_parallel_tool_batch_dispatch_completion_and_agent_resume() -> None:
    _, runs, jobs, registry, provider, loop, coordinator, calls = stack()
    owner, worker_identity = identities()
    create_run(runs, owner)
    service = AgentRunService(runs)
    agent_worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        tool_dispatcher=coordinator,
        heartbeat_interval_seconds=1,
    )

    first = asyncio.run(agent_worker.process_once(worker=worker_identity))
    waiting = runs.get(tenant_id="team-a", run_id="run-tools")
    batch = jobs.list_for_run(tenant_id="team-a", run_id="run-tools")
    assert first.status is AgentWorkerOutcomeStatus.AWAITING_TOOL
    assert waiting.status is DurableRunStatus.AWAITING_TOOL
    assert len(batch) == 2
    assert {job.status for job in batch} == {ToolJobStatus.QUEUED}

    tool_worker = DurableToolWorker(
        repository=jobs,
        registry=registry,
        tenant_id="team-a",
        worker_id="tool-worker",
        lease_seconds=5,
        heartbeat_seconds=1,
        reconciler=coordinator,
    )
    assert asyncio.run(tool_worker.run_once()) is True
    assert runs.get(tenant_id="team-a", run_id="run-tools").status is DurableRunStatus.AWAITING_TOOL
    assert asyncio.run(tool_worker.run_once()) is True
    assert runs.get(tenant_id="team-a", run_id="run-tools").status is DurableRunStatus.QUEUED
    assert coordinator.reconcile(tenant_id="team-a", actor_id="reaper") == 0

    second = asyncio.run(agent_worker.process_once(worker=worker_identity))
    completed = runs.get(tenant_id="team-a", run_id="run-tools")
    assert second.status is AgentWorkerOutcomeStatus.COMPLETED
    assert completed.status is DurableRunStatus.COMPLETED
    assert calls == ["alpha", "beta"]
    tool_messages = [message for message in provider.received if message.role == "tool"]
    assert len(tool_messages) == 2
    assert all('"status":"succeeded"' in message.content for message in tool_messages)


def test_dispatch_is_atomic_when_one_job_conflicts() -> None:
    engine, runs, jobs, _, _, loop, coordinator, _ = stack()
    owner, worker_identity = identities()
    create_run(runs, owner)
    service = AgentRunService(runs)
    lease = service.claim(worker=worker_identity)
    assert lease is not None
    service.start(worker=worker_identity, run_id="run-tools", lease_token=lease.lease_token)
    request = AgentRunCheckpointCodec().request(lease=lease, principal=owner)
    result = asyncio.run(loop.run(request))
    checkpoint = AgentRunCheckpointCodec().result(request=request, result=result)

    # Occupy the second Run/Call identity with a different deterministic Job ID.
    jobs.enqueue(
        tenant_id="team-a",
        actor_id="agent-worker",
        job_id="conflicting-job",
        run_id="run-tools",
        call_id="call-b",
        tool_name="office.echo",
        idempotency_key="other-idem",
        arguments={"value": "different"},
    )
    try:
        coordinator.dispatch(
            tenant_id="team-a",
            run_id="run-tools",
            worker_id="agent-worker",
            lease_token=lease.lease_token,
            checkpoint=checkpoint,
            result=result,
        )
    except Exception:
        pass
    else:
        raise AssertionError("conflicting batch dispatch must fail")

    with engine.connect() as connection:
        run_status = connection.execute(
            select(AGENT_RUNS.c.status).where(AGENT_RUNS.c.run_id == "run-tools")
        ).scalar_one()
    assert run_status == DurableRunStatus.RUNNING.value
    remaining = jobs.list_for_run(tenant_id="team-a", run_id="run-tools")
    assert [(job.job_id, job.call_id) for job in remaining] == [("conflicting-job", "call-b")]


def test_second_batch_ignores_terminal_jobs_from_first_batch() -> None:
    _, runs, jobs, registry, _, _, coordinator, _ = stack()
    owner, worker_identity = identities()
    create_run(runs, owner)
    service = AgentRunService(runs)

    class TwoBatchProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, **_):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=(ToolCall("first", "office.echo", {"value": "1"}),))
            if self.calls == 2:
                return LLMResponse(tool_calls=(ToolCall("second", "office.echo", {"value": "2"}),))
            return LLMResponse(text="done")

    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=TwoBatchProvider(),
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
        audit=audit,
        durable_tools=True,
    )
    agent = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        tool_dispatcher=coordinator,
        heartbeat_interval_seconds=1,
    )
    tool = DurableToolWorker(
        repository=jobs,
        registry=registry,
        tenant_id="team-a",
        worker_id="tool-worker",
        lease_seconds=5,
        heartbeat_seconds=1,
        reconciler=coordinator,
    )

    assert (
        asyncio.run(agent.process_once(worker=worker_identity)).status
        is AgentWorkerOutcomeStatus.AWAITING_TOOL
    )
    assert asyncio.run(tool.run_once())
    assert (
        asyncio.run(agent.process_once(worker=worker_identity)).status
        is AgentWorkerOutcomeStatus.AWAITING_TOOL
    )
    assert len(jobs.list_for_run(tenant_id="team-a", run_id="run-tools")) == 2
    assert asyncio.run(tool.run_once())
    assert (
        asyncio.run(agent.process_once(worker=worker_identity)).status
        is AgentWorkerOutcomeStatus.COMPLETED
    )
