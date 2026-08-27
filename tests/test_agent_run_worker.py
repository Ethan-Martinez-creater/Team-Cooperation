import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import (
    AGENT_RUNS,
    AgentCheckpointKeyring,
    AgentRunCheckpointCodec,
    AgentRunService,
    AgentWorkerOutcomeStatus,
    DurableAgentWorker,
    DurableAgentWorkerRunner,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.approvals import ApprovalService, SQLAlchemyApprovalRepository
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.context import (
    ContentTrust,
    ContextBudget,
    ContextItem,
    ContextSource,
)
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.runtime import (
    AgentLoop,
    AuthorizedTool,
    AgentRunRequest,
    LLMResponse,
    Message,
    ModelRoutePolicy,
    RunBudget,
    ToolAuthorization,
    ToolCall,
)
from coifesp_harness.security import (
    Classification,
    DisclosureGrant,
    PolicyEngine,
    Principal,
    ResourceLabel,
    RiskLevel,
)
from coifesp_harness.errors import IdentityProviderUnavailable
from coifesp_harness.tools import (
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ReviewDisclosure,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
)


class Resolver:
    def __init__(self, principal):
        self.principal = principal

    async def resolve(self, *, tenant_id, principal_id):
        assert tenant_id == self.principal.tenant_id
        assert principal_id == self.principal.principal_id
        return self.principal


class MismatchedResolver:
    async def resolve(self, **_):
        return Principal("mallory", "team-a")


class Provider:
    def __init__(self, response=None, error=None, delay=0):
        self.response = response or LLMResponse(text="done", input_tokens=7, output_tokens=3)
        self.error = error
        self.delay = delay

    async def complete(self, **_):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.response


class CountingService(AgentRunService):
    def __init__(self, repository):
        super().__init__(repository)
        self.heartbeats = 0

    def heartbeat(self, **kwargs):
        self.heartbeats += 1
        return super().heartbeat(**kwargs)


def stack(*, provider, registry=None, service_class=AgentRunService):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyAgentRunRepository(
        engine=engine,
        keyring=AgentCheckpointKeyring(master_key=b"k" * 32, key_id="test-v1"),
    )
    repository.create_schema()
    approval_repository = SQLAlchemyApprovalRepository(engine=engine)
    approval_repository.create_schema()
    approvals = ApprovalService(approval_repository)
    service = service_class(repository)
    service.approval_service = approvals
    tools = registry or ToolRegistry()
    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=provider,
        registry=tools,
        executor=ToolExecutor(
            registry=tools,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
            approval_service=approvals,
        ),
        audit=audit,
    )
    return service, repository, loop


def request(
    principal,
    *,
    run_id="run-worker",
    context_items=(),
    model_route_policy=None,
    tool_authorization=None,
):
    return AgentRunRequest(
        run_id=run_id,
        correlation_id=f"corr-{run_id}",
        principal=principal,
        messages=(Message("user", "complete the assigned work"),),
        context_items=context_items,
        context_budget=ContextBudget(
            max_input_tokens=4000,
            reserved_output_tokens=500,
            max_item_tokens=1000,
            max_items=10,
        ),
        context_purpose="task.delivery",
        model_route_policy=model_route_policy or ModelRoutePolicy(),
        tool_authorization=tool_authorization,
    )


def enqueue(service, principal, value, *, max_failures=3):
    codec = AgentRunCheckpointCodec()
    return service.create(
        principal=principal,
        run_id=value.run_id,
        correlation_id=value.correlation_id,
        idempotency_key=f"idem-{value.run_id}",
        checkpoint=codec.initial(value),
        max_failures=max_failures,
    )


def worker_principal():
    return Principal(
        "worker-1",
        "team-a",
        roles=frozenset({"agent_worker"}),
        is_service=True,
    )


def test_worker_round_trips_labeled_context_and_heartbeats_to_completion() -> None:
    owner = Principal("alice", "team-a")
    grant = DisclosureGrant(
        grant_id="grant-1",
        owner_tenant_id="team-b",
        recipient_tenant_id="team-a",
        resource_id="artifact-1",
        purpose="task.delivery",
        approved_by="lead-b",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        max_classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    item = ContextItem(
        item_id="context-1",
        content="explicitly shared interface contract",
        source=ContextSource.GOVERNANCE,
        source_id="artifact-1",
        label=ResourceLabel(
            "team-b",
            Classification.CONFIDENTIAL,
            frozenset({"project-x"}),
            "artifact-1",
        ),
        content_trust=ContentTrust.VERIFIED,
        disclosure_grant=grant,
    )
    service, repository, loop = stack(
        provider=Provider(delay=0.06),
        service_class=CountingService,
    )
    value = request(
        owner,
        context_items=(item,),
        model_route_policy=ModelRoutePolicy(
            data_classification=Classification.CONFIDENTIAL,
        ),
    )
    enqueue(service, owner, value)
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        lease_seconds=5,
        heartbeat_interval_seconds=0.01,
    )
    outcome = asyncio.run(worker.process_once(worker=worker_principal()))
    run = service.get(principal=owner, run_id=value.run_id)
    restored = AgentRunCheckpointCodec().decode(
        repository.load_checkpoint(tenant_id="team-a", run_id=value.run_id)
    )
    assert outcome.status is AgentWorkerOutcomeStatus.COMPLETED
    assert run.status is DurableRunStatus.COMPLETED
    assert run.total_tokens == 10
    assert service.heartbeats >= 1
    assert restored["context_items"][0].content_digest == item.content_digest
    assert restored["context_items"][0].disclosure_grant == grant


def test_worker_bounds_transient_retries_and_exhausts_failure_budget() -> None:
    owner = Principal("alice", "team-a")
    service, repository, loop = stack(provider=Provider(error=TimeoutError()))
    value = request(owner, run_id="run-retry")
    enqueue(service, owner, value, max_failures=2)
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        heartbeat_interval_seconds=1,
    )
    first = asyncio.run(worker.process_once(worker=worker_principal()))
    scheduled = service.get(principal=owner, run_id=value.run_id)
    assert first.status is AgentWorkerOutcomeStatus.RETRY_SCHEDULED
    assert scheduled.failure_count == 1
    assert scheduled.next_attempt_at is not None
    with repository.engine.begin() as connection:
        connection.execute(
            update(AGENT_RUNS)
            .where(AGENT_RUNS.c.run_id == value.run_id)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = asyncio.run(worker.process_once(worker=worker_principal()))
    failed = service.get(principal=owner, run_id=value.run_id)
    assert second.status is AgentWorkerOutcomeStatus.FAILED
    assert failed.status is DurableRunStatus.FAILED
    assert failed.failure_count == failed.max_failures == 2
    assert failed.last_error_code == "dependency_timeout"
    assert failed.next_attempt_at is None


def test_worker_persists_approval_pause_without_exposing_arguments_in_events() -> None:
    owner = Principal("alice", "team-a")
    registry = ToolRegistry()

    async def handler(arguments):
        return {"sent": arguments["destination"]}

    registry.register(
        ToolDefinition(
            name="send_external",
            description="send externally",
            handler=handler,
            parameters_schema={
                "type": "object",
                "properties": {"destination": {"type": "string"}},
                "required": ["destination"],
                "additionalProperties": False,
            },
            risk=RiskLevel.HIGH,
            approval_review=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField(
                        "destination",
                        "/destination",
                        ReviewDisclosure.HASH,
                    ),
                )
            ),
        )
    )
    response = LLMResponse(
        tool_calls=(ToolCall("call-send", "send_external", {"destination": "private-customer"}),),
        input_tokens=4,
        output_tokens=2,
    )
    service, repository, loop = stack(provider=Provider(response=response), registry=registry)
    value = request(
        owner,
        run_id="run-approval-worker",
        tool_authorization=ToolAuthorization(
            tools=(AuthorizedTool("send_external", "1", "test-schema"),)
        ),
    )
    enqueue(service, owner, value)
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        heartbeat_interval_seconds=1,
    )
    outcome = asyncio.run(worker.process_once(worker=worker_principal()))
    run = service.get(principal=owner, run_id=value.run_id)
    events = service.events(principal=owner, run_id=value.run_id)
    assert outcome.status is AgentWorkerOutcomeStatus.AWAITING_APPROVAL
    assert run.status is DurableRunStatus.AWAITING_APPROVAL
    assert run.pending_call_id == "call-send"
    assert run.pending_approval_id
    assert "private-customer" not in repr(events)


def test_worker_persists_actual_token_usage_when_budget_is_exceeded() -> None:
    owner = Principal("alice", "team-a")
    service, _, loop = stack(
        provider=Provider(
            response=LLMResponse(text="too expensive", input_tokens=8, output_tokens=5)
        )
    )
    value = AgentRunRequest(
        run_id="run-budget",
        correlation_id="corr-run-budget",
        principal=owner,
        messages=(Message("user", "bounded work"),),
        budget=RunBudget(max_turns=2, max_tool_calls=2, max_total_tokens=10),
    )
    enqueue(service, owner, value)
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        heartbeat_interval_seconds=1,
    )
    outcome = asyncio.run(worker.process_once(worker=worker_principal()))
    run = service.get(principal=owner, run_id=value.run_id)
    assert outcome.status is AgentWorkerOutcomeStatus.FAILED
    assert outcome.error_code == "token_budget_exceeded"
    assert run.total_tokens == 13
    assert run.last_error_code == "token_budget_exceeded"


def test_worker_rejects_resolver_identity_mismatch_without_retrying() -> None:
    owner = Principal("alice", "team-a")
    service, _, loop = stack(provider=Provider())
    value = request(owner, run_id="run-principal-mismatch")
    enqueue(service, owner, value)
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=MismatchedResolver(),
        heartbeat_interval_seconds=1,
    )
    outcome = asyncio.run(worker.process_once(worker=worker_principal()))
    run = service.get(principal=owner, run_id=value.run_id)
    assert outcome.status is AgentWorkerOutcomeStatus.FAILED
    assert outcome.error_code == "checkpoint_integrity_failed"
    assert run.failure_count == 0
    assert run.last_error_code == "checkpoint_integrity_failed"


def test_worker_runner_revalidates_identity_and_stops_during_idle_wait() -> None:
    class Identity:
        def __init__(self):
            self.calls = 0

        async def resolve(self):
            self.calls += 1
            return worker_principal()

    class Worker:
        def __init__(self):
            self.calls = 0

        async def process_once(self, *, worker):
            assert worker.is_service
            self.calls += 1
            return type("Outcome", (), {"status": AgentWorkerOutcomeStatus.IDLE})()

    async def run():
        identity = Identity()
        worker = Worker()
        runner = DurableAgentWorkerRunner(
            worker=worker,
            identity_provider=identity,
            idle_poll_seconds=10,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(runner.run(stop=stop))
        while worker.calls == 0:
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)
        return identity.calls, worker.calls

    identity_calls, worker_calls = asyncio.run(run())
    assert identity_calls == worker_calls == 1


def test_worker_runner_bounds_identity_failure_backoff() -> None:
    class Identity:
        def __init__(self):
            self.calls = 0

        async def resolve(self):
            self.calls += 1
            raise IdentityProviderUnavailable("temporarily unavailable")

    class Worker:
        async def process_once(self, **_):
            raise AssertionError("worker must not run without a verified identity")

    class FixedRandom:
        @staticmethod
        def uniform(_minimum, _maximum):
            return 1.0

    async def run():
        identity = Identity()
        runner = DurableAgentWorkerRunner(
            worker=Worker(),
            identity_provider=identity,
            identity_backoff_base_seconds=0.1,
            identity_backoff_cap_seconds=0.2,
            random_source=FixedRandom(),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(runner.run(stop=stop))

        async def wait_for_retries():
            while identity.calls < 2:
                if task.done():
                    await task
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_retries(), timeout=0.5)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)
        return identity.calls

    assert asyncio.run(run()) == 2
