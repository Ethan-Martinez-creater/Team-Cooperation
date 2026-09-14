import asyncio

import pytest
from test_agent_run_worker import Provider, request, stack
from test_agent_run_worker import enqueue as enqueue_run
from test_tool_job_worker import runtime

from coifesp_harness.agent_runs import AgentWorkerOutcomeStatus, DurableAgentWorker
from coifesp_harness.security import Principal, RiskLevel
from coifesp_harness.tool_jobs import (
    DurableToolWorkerRunner,
    ToolJobStatus,
    current_tool_execution_context,
)
from coifesp_harness.tools import ToolDefinition, ToolRegistry

TENANTS = ("team-product", "team-engineering", "team-quality")


class Resolver:
    def __init__(self, principals):
        self.principals = {(item.tenant_id, item.principal_id): item for item in principals}

    async def resolve(self, *, tenant_id, principal_id):
        return self.principals[(tenant_id, principal_id)]


def test_one_agent_worker_processes_three_tenants_without_cross_scope() -> None:
    service, repository, loop = stack(provider=Provider())
    owners = [Principal(f"owner-{index}", tenant) for index, tenant in enumerate(TENANTS)]
    for index, owner in enumerate(owners):
        value = request(owner, run_id=f"run-shared-{index}")
        enqueue_run(service, owner, value)
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owners),
        lease_seconds=5,
        heartbeat_interval_seconds=1,
        allowed_tenant_ids=TENANTS,
    )
    service_identity = Principal(
        "service:local-agent-worker", "platform",
        frozenset({"agent_worker"}), is_service=True,
    )
    outcomes = [asyncio.run(worker.process_once(worker=service_identity)) for _ in TENANTS]
    assert all(item.status is AgentWorkerOutcomeStatus.COMPLETED for item in outcomes)
    assert [item.run_id for item in outcomes] == [f"run-shared-{index}" for index in range(3)]
    for index, tenant_id in enumerate(TENANTS):
        assert repository.get(tenant_id=tenant_id, run_id=f"run-shared-{index}").status.value == "completed"


@pytest.mark.asyncio
async def test_one_tool_runner_processes_three_tenants_and_preserves_context() -> None:
    observed = []
    stop = asyncio.Event()

    async def handler(_arguments):
        observed.append(current_tool_execution_context().tenant_id)
        if len(observed) == len(TENANTS):
            stop.set()
        return {"ok": True}

    repository, _ = runtime(handler)
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="shared.echo", description="test", handler=handler,
        parameters_schema={"type": "object", "additionalProperties": False},
        risk=RiskLevel.LOW,
    ))
    for index, tenant_id in enumerate(TENANTS):
        repository.enqueue(
            tenant_id=tenant_id, actor_id="owner", job_id=f"job-shared-{index}",
            run_id=f"run-shared-{index}", call_id=f"call-shared-{index}",
            tool_name="shared.echo", idempotency_key=f"idem-shared-{index}",
            arguments={}, max_attempts=1,
        )

    class Identity:
        async def resolve(self):
            return Principal("service:local-tool-worker", "platform",
                             frozenset({"tool_worker"}), is_service=True)

    class Reconciler:
        def reconcile(self, **_):
            return 0

    runner = DurableToolWorkerRunner(
        repository=repository, registry=registry, reconciler=Reconciler(),
        identity_provider=Identity(), allowed_tenant_ids=TENANTS,
        idle_poll_seconds=0.05, lease_seconds=5, heartbeat_seconds=1,
    )
    await asyncio.wait_for(runner.run(stop=stop), timeout=5)
    assert observed == list(TENANTS)
    for index, tenant_id in enumerate(TENANTS):
        assert repository.get(tenant_id=tenant_id, job_id=f"job-shared-{index}").status is ToolJobStatus.SUCCEEDED


def test_allowed_claim_never_touches_unlisted_tenant() -> None:
    async def handler(arguments):
        return arguments

    repository, _ = runtime(handler)
    repository.enqueue(
        tenant_id="team-private", actor_id="owner", job_id="job-private",
        run_id="run-private", call_id="call-private", tool_name="office.send_message",
        idempotency_key="idem-private", arguments={"message": "private"}, max_attempts=1,
    )
    assert repository.claim_next_allowed(
        allowed_tenant_ids=TENANTS, worker_id="shared-worker", lease_seconds=5,
    ) is None
    assert repository.get(tenant_id="team-private", job_id="job-private").status is ToolJobStatus.QUEUED
