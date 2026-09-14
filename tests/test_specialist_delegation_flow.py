import asyncio
import json

import pytest
from sqlalchemy import select, update
from test_persistent_team_task_contracts import accept, propose
from test_team_agent_dispatcher import dispatch, record, stack

from coifesp_harness.agent_runs import (
    AgentRunCheckpointCodec,
    AgentWorkerOutcomeStatus,
    DurableAgentWorker,
    DurableRunStatus,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.errors import PolicyDenied
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    SPECIALIST_DELEGATIONS,
    TEAM_TASKS,
)
from coifesp_harness.project_process.repository import PROJECT_EXECUTION_RESERVATIONS
from coifesp_harness.runtime import AgentLoop, AuthorizedTool, LLMResponse, ToolCall
from coifesp_harness.security import PolicyEngine, Principal
from coifesp_harness.team_agents.identity import TeamAgentPrincipalResolver
from coifesp_harness.team_agents.profiles import TeamAgentCapabilityResolver
from coifesp_harness.team_agents.specialists import (
    SpecialistDelegationService,
    SpecialistDelegationTool,
    SpecialistRunProjection,
    specialist_delegation_manifest,
)
from coifesp_harness.tool_catalog import project_context_manifests
from coifesp_harness.tool_jobs import (
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolBatchCoordinator,
    ToolJobKeyring,
    ToolJobStatus,
)
from coifesp_harness.tools import ToolExecutor, ToolRegistry


class _NoHumanResolver:
    async def resolve(self, **_):
        raise AssertionError("automated specialist flow cannot resolve a human principal")


def _loop(provider, manifests):
    registry = ToolRegistry()
    for manifest in manifests:
        registry.register(manifest.declaration())
    audit = InMemoryAuditSink()
    return AgentLoop(
        provider=provider,
        registry=registry,
        durable_tools=True,
        audit=audit,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
    )


def _prepare():
    value = stack(accepted=False)
    manifest = specialist_delegation_manifest()
    manifests = (*project_context_manifests(), manifest)
    value.dispatcher.runtime_resolver = TeamAgentCapabilityResolver(
        engine=value.engine,
        tool_policies={
            "default": tuple(
                AuthorizedTool(item.tool_id, item.version, item.schema_digest)
                for item in manifests
            )
        },
    )
    propose(value)
    accept(value)
    record(value, "decision-specialist")
    value.dispatched = dispatch(value, decision_id="decision-specialist")
    value.jobs = SQLAlchemyToolJobRepository(
        engine=value.engine,
        keyring=ToolJobKeyring(master_key=b"s" * 32, key_id="test"),
    )
    value.jobs.create_schema()
    value.coordinator = ToolBatchCoordinator(
        engine=value.engine,
        agent_runs=value.runs,
        tool_jobs=value.jobs,
    )
    checkpoint = AgentRunCheckpointCodec().decode(
        value.runs.load_checkpoint(
            tenant_id="team-b", run_id=value.dispatched.run_id
        )
    )
    value.arguments = {
        "kind": "code_review",
        "request": "Review the accepted task context for concrete defects.",
        "context_item_ids": [item.item_id for item in checkpoint["context_items"]],
    }

    class ParentProvider:
        async def complete(self, **_):
            return LLMResponse(
                tool_calls=(
                    ToolCall("delegate-review", manifest.tool_id, value.arguments),
                ),
                input_tokens=3,
                output_tokens=2,
            )

    resolver = TeamAgentPrincipalResolver(
        engine=value.engine, human_resolver=_NoHumanResolver()
    )
    value.parent_worker = DurableAgentWorker(
        service=value.dispatcher.run_service,
        loop=_loop(ParentProvider(), manifests),
        tool_dispatcher=value.coordinator,
        principal_resolver=resolver,
    )
    value.agent_worker_identity = Principal(
        "agent-worker", "team-b", roles=frozenset({"agent_worker"}), is_service=True
    )
    value.delegations = SpecialistDelegationService(
        repository=value.repository,
        runs=value.runs,
        jobs=value.jobs,
    )
    value.projection = SpecialistRunProjection(
        repository=value.repository,
        runs=value.runs,
        jobs=value.jobs,
        coordinator=value.coordinator,
    )
    registry = ToolRegistry()
    registry.register(SpecialistDelegationTool(value.delegations).definition())
    value.tool_worker = DurableToolWorker(
        repository=value.jobs,
        registry=registry,
        tenant_id="team-b",
        worker_id="tool-worker",
        reconciler=value.coordinator,
    )
    return value, resolver


def _start_specialist(value):
    outcome = asyncio.run(
        value.parent_worker.process_once(worker=value.agent_worker_identity)
    )
    assert outcome.status is AgentWorkerOutcomeStatus.AWAITING_TOOL
    assert asyncio.run(value.tool_worker.run_once())
    job = value.jobs.list_for_run(
        tenant_id="team-b", run_id=value.dispatched.run_id
    )[0]
    assert job.status is ToolJobStatus.AWAITING_SPECIALIST
    with value.engine.connect() as connection:
        delegation = connection.execute(select(SPECIALIST_DELEGATIONS)).mappings().one()
        binding = connection.execute(
            select(PROJECT_AGENT_RUNS).where(
                PROJECT_AGENT_RUNS.c.run_id == delegation["child_run_id"]
            )
        ).mappings().one()
        reservation = connection.execute(
            select(PROJECT_EXECUTION_RESERVATIONS).where(
                PROJECT_EXECUTION_RESERVATIONS.c.reservation_id
                == delegation["project_budget_reservation_id"]
            )
        ).mappings().one()
    assert binding["run_kind"] == "specialist"
    assert binding["parent_run_id"] == value.dispatched.run_id
    assert binding["execution_attempt"] is None
    assert reservation["specialist_depth"] == 1
    assert reservation["status"] == "RESERVED"
    return delegation


def test_specialist_agent_as_tool_completes_and_wakes_parent_once():
    value, resolver = _prepare()
    delegation = _start_specialist(value)

    resolved = asyncio.run(
        resolver.resolve_for_run(
            tenant_id="team-b",
            principal_id="specialist-agent:team-b:code_review",
            run_id=delegation["child_run_id"],
        )
    )
    assert resolved.roles == frozenset({"specialist_agent"})
    assert resolved.compartments == frozenset({"project:project-a"})
    with value.engine.connect() as connection:
        assert connection.execute(
            select(SPECIALIST_DELEGATIONS.c.status)
        ).scalar_one() == "RUNNING"

    class SpecialistProvider:
        async def complete(self, **_):
            return LLMResponse(
                text=json.dumps(
                    {"summary": "No blocking defects.", "verdict": "pass", "findings": []}
                ),
                input_tokens=5,
                output_tokens=4,
            )

    value.dispatcher.run_service.terminal_callback = value.projection.on_run_terminal
    child_worker = DurableAgentWorker(
        service=value.dispatcher.run_service,
        loop=_loop(SpecialistProvider(), project_context_manifests()),
        principal_resolver=resolver,
    )
    outcome = asyncio.run(
        child_worker.process_once(worker=value.agent_worker_identity)
    )
    assert outcome.status is AgentWorkerOutcomeStatus.COMPLETED
    job = value.jobs.get(
        tenant_id="team-b", job_id=delegation["tool_job_id"], include_payloads=True
    )
    assert job.status is ToolJobStatus.SUCCEEDED
    assert job.result["result"]["verdict"] == "pass"
    owner = Principal(
        "team-agent:team-b",
        "team-b",
        roles=frozenset({"team_agent"}),
        is_service=True,
    )
    assert value.dispatcher.run_service.get(
        principal=owner, run_id=value.dispatched.run_id
    ).status is DurableRunStatus.QUEUED
    assert value.projection.replay_pending(tenant_id="team-b") == 0


def test_terminal_specialist_projection_recovers_after_callback_loss():
    value, resolver = _prepare()
    delegation = _start_specialist(value)

    class SpecialistProvider:
        async def complete(self, **_):
            return LLMResponse(
                text=json.dumps(
                    {"summary": "Needs fixes.", "verdict": "changes_requested", "findings": []}
                ),
                input_tokens=2,
                output_tokens=2,
            )

    child_worker = DurableAgentWorker(
        service=value.dispatcher.run_service,
        loop=_loop(SpecialistProvider(), project_context_manifests()),
        principal_resolver=resolver,
    )
    assert asyncio.run(
        child_worker.process_once(worker=value.agent_worker_identity)
    ).status is AgentWorkerOutcomeStatus.COMPLETED
    assert value.jobs.get(
        tenant_id="team-b", job_id=delegation["tool_job_id"]
    ).status is ToolJobStatus.AWAITING_SPECIALIST

    assert value.projection.replay_pending(tenant_id="other-team") == 0
    from test_control_plane import StubVerifier, settings

    from coifesp_harness.control_plane import create_app

    value.projection.coordinator = value.coordinator
    app = create_app(settings=settings(), verifier=StubVerifier({}), readiness_probe=lambda: True)
    app.state.agent_run_service = value.dispatcher.run_service
    app.state.specialist_run_projection = value.projection

    async def restart():
        async with app.router.lifespan_context(app):
            assert value.jobs.get(
                tenant_id="team-b", job_id=delegation["tool_job_id"]
            ).status is ToolJobStatus.SUCCEEDED
            assert value.runs.get(
                tenant_id="team-b", run_id=value.dispatched.run_id
            ).status is DurableRunStatus.QUEUED

    asyncio.run(restart())
    asyncio.run(restart())
    assert value.projection.replay_all_tenants() == 0
    with value.engine.connect() as connection:
        stored = connection.execute(select(SPECIALIST_DELEGATIONS)).mappings().one()
        reservation = connection.execute(
            select(PROJECT_EXECUTION_RESERVATIONS).where(
                PROJECT_EXECUTION_RESERVATIONS.c.reservation_id
                == stored["project_budget_reservation_id"]
            )
        ).mappings().one()
    assert stored["status"] == "COMPLETED"
    assert reservation["status"] == "SETTLED"


def test_specialist_replay_requires_explicit_tenant_scope():
    value, _ = _prepare()
    with pytest.raises(ValueError, match="explicit tenant"):
        value.projection.replay_pending()


def test_replay_cancels_queued_specialist_after_parent_task_invalidated():
    value, _resolver = _prepare()
    delegation = _start_specialist(value)
    with value.engine.begin() as connection:
        connection.execute(
            update(TEAM_TASKS)
            .where(TEAM_TASKS.c.task_id == delegation["team_task_id"])
            .values(status="changes_requested")
        )

    assert value.projection.replay_pending(tenant_id="team-b") == 1
    child = value.dispatcher.run_service.get(
        principal=Principal(
            "specialist-agent:team-b:code_review",
            "team-b",
            roles=frozenset({"specialist_agent"}),
            is_service=True,
        ),
        run_id=delegation["child_run_id"],
    )
    assert child.status is DurableRunStatus.CANCELLED
    assert value.jobs.get(
        tenant_id="team-b", job_id=delegation["tool_job_id"]
    ).status is ToolJobStatus.FAILED
    with value.engine.connect() as connection:
        stored = connection.execute(select(SPECIALIST_DELEGATIONS)).mappings().one()
    assert stored["status"] == "CANCELLED"
    assert value.projection.project(run_id=delegation["child_run_id"])
    assert value.projection.project(run_id=delegation["child_run_id"])
    assert value.projection.replay_pending(tenant_id="team-b") == 0


    value, resolver = _prepare()
    delegation = _start_specialist(value)
    with value.engine.begin() as connection:
        connection.execute(
            update(TEAM_TASKS)
            .where(TEAM_TASKS.c.task_id == delegation["team_task_id"])
            .values(status="changes_requested")
        )
    with pytest.raises(PolicyDenied):
        asyncio.run(
            resolver.resolve_for_run(
                tenant_id="team-b",
                principal_id="specialist-agent:team-b:code_review",
                run_id=delegation["child_run_id"],
            )
        )

    class UnreachableProvider:
        async def complete(self, **_):
            raise AssertionError("cancelled parent must fence specialist execution")

    value.dispatcher.run_service.terminal_callback = value.projection.on_run_terminal
    child_worker = DurableAgentWorker(
        service=value.dispatcher.run_service,
        loop=_loop(UnreachableProvider(), project_context_manifests()),
        principal_resolver=resolver,
    )
    outcome = asyncio.run(
        child_worker.process_once(worker=value.agent_worker_identity)
    )
    assert outcome.status is AgentWorkerOutcomeStatus.FAILED
    assert value.jobs.get(
        tenant_id="team-b", job_id=delegation["tool_job_id"]
    ).status is ToolJobStatus.FAILED
    with value.engine.connect() as connection:
        stored = connection.execute(select(SPECIALIST_DELEGATIONS)).mappings().one()
        reservation = connection.execute(
            select(PROJECT_EXECUTION_RESERVATIONS).where(
                PROJECT_EXECUTION_RESERVATIONS.c.reservation_id
                == stored["project_budget_reservation_id"]
            )
        ).mappings().one()
    assert stored["status"] == "FAILED"
    assert reservation["status"] == "SETTLED"
