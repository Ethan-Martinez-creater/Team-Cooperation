import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import (
    AgentCheckpointKeyring,
    AgentRunCheckpointCodec,
    AgentRunService,
    AgentWorkerOutcomeStatus,
    DurableAgentWorker,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.errors import IntegrityError, PolicyDenied
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    ProjectWorkspaceService,
    TeamAccountRole,
)
from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_TEAMS,
    TEAM_PROJECT_AGENTS,
    TEAM_TASKS,
)
from coifesp_harness.project_process.repository import (
    PROJECT_PLANNER_INTENTS,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.runtime import (
    AgentLoop,
    AgentRunRequest,
    LLMResponse,
    Message,
    RunBudget,
)
from coifesp_harness.security import PolicyEngine, Principal
from coifesp_harness.team_agents import TeamAgentPrincipalResolver
from coifesp_harness.tools import ToolExecutor, ToolRegistry


class _HumanResolver:
    async def resolve(self, *, tenant_id, principal_id):
        return Principal(principal_id, tenant_id)


class _Provider:
    async def complete(self, **_):
        return LLMResponse(text="done", input_tokens=1, output_tokens=1)


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProductAccountService(engine).create_schema()
    accounts = ProductAccountService(engine)
    for team_id in ("team-a", "team-b"):
        accounts.register_team(team_id=team_id, team_handle=team_id, team_name=team_id)
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="delegated worker test",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    ProjectWorkspaceService(engine).ensure_team_project_agent(
        project_id="project-a",
        team_id="team-a",
    )
    SQLAlchemyProjectProcessRepository(engine).create_schema()
    run_repository = SQLAlchemyAgentRunRepository(
        engine=engine,
        keyring=AgentCheckpointKeyring(master_key=b"k" * 32, key_id="test-v1"),
    )
    run_repository.create_schema()
    service = AgentRunService(run_repository)
    audit = InMemoryAuditSink()
    registry = ToolRegistry()
    loop = AgentLoop(
        provider=_Provider(),
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
        audit=audit,
    )
    return engine, service, loop


def _run_owner(principal_id, tenant_id="team-a"):
    return Principal(
        principal_id,
        tenant_id,
        roles=frozenset({"project_orchestrator"})
        if principal_id == "service:project-orchestrator"
        else frozenset({"team_agent"}),
        is_service=True,
    )


def _enqueue(service, owner, run_id):
    request = AgentRunRequest(
        run_id=run_id,
        correlation_id=f"corr-{run_id}",
        principal=owner,
        messages=(Message("user", "complete the assigned work"),),
        budget=RunBudget(max_turns=2, max_tool_calls=1, max_total_tokens=100),
    )
    return service.create(
        principal=owner,
        run_id=run_id,
        correlation_id=request.correlation_id,
        idempotency_key=f"idem-{run_id}",
        checkpoint=AgentRunCheckpointCodec().initial(request),
    )


def _insert_planner_intent(engine, *, run_id, status="RUNNING"):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            PROJECT_PLANNER_INTENTS.insert().values(
                planner_intent_id=f"intent-{run_id}",
                process_id=f"process-{run_id}",
                project_id="project-a",
                owner_team_id="team-a",
                reason="ANALYSIS",
                based_on_process_version=1,
                based_on_event_sequence=0,
                graph_snapshot_digest="g" * 64,
                status=status,
                run_id=run_id,
                decision_id=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                projected_at=None if status in {"PENDING", "RUNNING"} else now,
            )
        )


def _insert_task_run(engine, *, run_id="run-task"):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        agent_id = connection.execute(
            select(TEAM_PROJECT_AGENTS.c.agent_id).where(
                TEAM_PROJECT_AGENTS.c.project_id == "project-a",
                TEAM_PROJECT_AGENTS.c.team_id == "team-a",
            )
        ).scalar_one()
        connection.execute(
            TEAM_TASKS.insert().values(
                task_id=f"task-{run_id}",
                project_id="project-a",
                source_team_id="team-b",
                target_team_id="team-a",
                created_by="lead-a",
                title="Task",
                description="delegated task",
                acceptance_criteria="done",
                status="accepted",
                assigned_account_id=None,
                artifact_resource_ids="[]",
                review_note="",
                priority="normal",
                due_at=None,
                schedule_version=1,
                due_changed_at=None,
                due_changed_by=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            PROJECT_AGENT_RUNS.insert().values(
                project_id="project-a",
                run_id=run_id,
                team_id="team-a",
                created_by=None,
                mode=None,
                conversation_id=None,
                turn_id=None,
                process_id=f"process-{run_id}",
                team_agent_id=agent_id,
                work_node_id=f"node-{run_id}",
                team_task_id=f"task-{run_id}",
                parent_run_id=None,
                orchestration_decision_id=f"decision-{run_id}",
                run_kind="task_execution",
                initiated_by_principal_id="service:project-orchestrator",
                executed_as_principal_id="team-agent:team-a",
                delegation_scope_digest="d" * 64,
                execution_attempt=1,
                capacity_reservation_id=f"capacity-{run_id}",
                project_budget_reservation_id=f"budget-{run_id}",
                created_at=now,
            )
        )


def _worker(engine, service, loop, resolver=None):
    return DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=resolver
        or TeamAgentPrincipalResolver(engine=engine, human_resolver=_HumanResolver()),
        lease_seconds=5,
        heartbeat_interval_seconds=0.05,
    )


def _worker_principal():
    return Principal(
        "worker-1",
        "team-a",
        roles=frozenset({"agent_worker"}),
        is_service=True,
    )


def test_bound_planner_run_executes_with_real_worker_and_loop():
    engine, service, loop = _stack()
    owner = _run_owner("service:project-orchestrator")
    _enqueue(service, owner, "run-planner")
    _insert_planner_intent(engine, run_id="run-planner")

    outcome = asyncio.run(
        _worker(engine, service, loop).process_once(worker=_worker_principal())
    )

    assert outcome.status is AgentWorkerOutcomeStatus.COMPLETED
    assert service.get(principal=owner, run_id="run-planner").status is DurableRunStatus.COMPLETED


def test_bound_task_run_executes_with_only_its_project_compartment():
    engine, service, loop = _stack()
    owner = _run_owner("team-agent:team-a")
    _enqueue(service, owner, "run-task")
    _insert_task_run(engine)
    resolver = TeamAgentPrincipalResolver(engine=engine, human_resolver=_HumanResolver())

    resolved = asyncio.run(
        resolver.resolve_for_run(
            tenant_id="team-a",
            principal_id="team-agent:team-a",
            run_id="run-task",
        )
    )
    outcome = asyncio.run(
        _worker(engine, service, loop, resolver).process_once(worker=_worker_principal())
    )

    assert resolved.compartments == frozenset({"project:project-a"})
    assert outcome.status is AgentWorkerOutcomeStatus.COMPLETED
    assert service.get(principal=owner, run_id="run-task").status is DurableRunStatus.COMPLETED


@pytest.mark.parametrize(
    "case",
    ["unbound", "wrong_tenant", "wrong_run", "terminal_intent", "paused_team_agent"],
)
def test_invalid_delegated_bindings_are_rejected(case):
    engine, service, loop = _stack()
    resolver = TeamAgentPrincipalResolver(engine=engine, human_resolver=_HumanResolver())
    owner = _run_owner("team-agent:team-a")
    run_id = f"run-{case}"
    _enqueue(service, owner, run_id)
    if case != "unbound":
        _insert_task_run(engine, run_id=run_id)
    if case == "wrong_tenant":
        with pytest.raises(IntegrityError):
            asyncio.run(
                resolver.resolve_for_run(
                    tenant_id="team-b",
                    principal_id="team-agent:team-a",
                    run_id=run_id,
                )
            )
        return
    if case == "wrong_run":
        with pytest.raises(PolicyDenied):
            asyncio.run(
                resolver.resolve_for_run(
                    tenant_id="team-a",
                    principal_id="team-agent:team-a",
                    run_id="run-never-bound",
                )
            )
        return
    if case == "terminal_intent":
        _insert_planner_intent(engine, run_id=run_id, status="PROJECTED")
        with pytest.raises(PolicyDenied):
            asyncio.run(
                TeamAgentPrincipalResolver(
                    engine=engine,
                    human_resolver=_HumanResolver(),
                ).resolve_for_run(
                    tenant_id="team-a",
                    principal_id="service:project-orchestrator",
                    run_id=run_id,
                )
            )
        return
    if case == "paused_team_agent":
        with engine.begin() as connection:
            connection.execute(
                update(TEAM_PROJECT_AGENTS)
                .where(
                    TEAM_PROJECT_AGENTS.c.project_id == "project-a",
                    TEAM_PROJECT_AGENTS.c.team_id == "team-a",
                )
                .values(status="archived")
            )
    with pytest.raises(PolicyDenied):
        asyncio.run(
            resolver.resolve_for_run(
                tenant_id="team-a",
                principal_id="team-agent:team-a",
                run_id=run_id,
            )
        )


def test_legacy_fake_service_resolver_cannot_bypass_codec():
    engine, service, loop = _stack()
    owner = _run_owner("team-agent:team-a")
    _enqueue(service, owner, "run-fake")

    class FakeServiceResolver:
        async def resolve(self, **_):
            return owner

    outcome = asyncio.run(
        _worker(
            engine,
            service,
            loop,
            resolver=FakeServiceResolver(),
        ).process_once(worker=_worker_principal())
    )

    assert outcome.status is AgentWorkerOutcomeStatus.FAILED
    assert outcome.error_code == "checkpoint_integrity_failed"


def test_task_run_is_rejected_after_team_leaves_the_project():
    engine, service, loop = _stack()
    owner = _run_owner("team-agent:team-a")
    _enqueue(service, owner, "run-team-left")
    _insert_task_run(engine, run_id="run-team-left")
    with engine.begin() as connection:
        connection.execute(
            delete(PROJECT_TEAMS).where(
                PROJECT_TEAMS.c.project_id == "project-a",
                PROJECT_TEAMS.c.team_id == "team-a",
            )
        )

    with pytest.raises(PolicyDenied):
        asyncio.run(
            TeamAgentPrincipalResolver(
                engine=engine,
                human_resolver=_HumanResolver(),
            ).resolve_for_run(
                tenant_id="team-a",
                principal_id="team-agent:team-a",
                run_id="run-team-left",
            )
        )
