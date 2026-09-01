"""Test-first contract for execution of accepted Project Work Contracts.

The project-work enqueue seam is intentionally kept separate from the legacy
Governance Assignment path.  The tests use the real Product, ProjectProcess,
WorkGraph, and execution repositories so the eventual implementation cannot
pass by checking only a copied request payload.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import (
    GovernanceConflictError,
    PolicyDenied,
    ResourceNotFound,
)
from coifesp_harness.execution import (
    ExecutionTask,
    SQLAlchemyTaskRepository,
    TaskExecutionService,
)
from coifesp_harness.execution.repository import EXECUTION_TASKS
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
    TeamCollaborationService,
)
from coifesp_harness.product.repository import PROJECT_TEAMS, TEAM_TASKS
from coifesp_harness.project_process import (
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.project_process.repository import (
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESSES,
)
from coifesp_harness.security import Principal
from coifesp_harness.team_agents.task_contracts import TeamTaskContractService
from coifesp_harness.work_graph import (
    ProjectWorkGraphService,
    SQLAlchemyWorkGraphRepository,
    WorkRelationType,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)
PROJECT_API_MISSING = not callable(
    getattr(TaskExecutionService, "enqueue_project_work", None)
)
project_api_required = pytest.mark.xfail(
    condition=PROJECT_API_MISSING,
    strict=True,
    reason="Phase 8 production enqueue_project_work seam is not implemented yet",
)

PROJECT_ERRORS = (GovernanceConflictError, PolicyDenied, ResourceNotFound)
TARGET_PRINCIPAL = Principal("lead-b", "team-b")
SOURCE_PRINCIPAL = Principal("lead-a", "team-a")


class _UnusedGovernance:
    """Make accidental delegation to the legacy governance path observable."""

    def read_program(self, **_kwargs):
        raise AssertionError("Project Work Contract path must not read Governance assignments")


def _project_stack(tmp_path: Path | None = None) -> SimpleNamespace:
    if tmp_path is None:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            f"sqlite+pysqlite:///{tmp_path / 'project-work-execution.sqlite3'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

    accounts = ProductAccountService(engine)
    accounts.create_schema()
    for team_id in ("team-a", "team-b", "team-c"):
        accounts.register_team(
            team_id=team_id,
            team_handle=team_id,
            team_name=team_id.upper(),
        )
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    accounts.ensure_active_account(
        account_id="lead-b",
        username="lead-b",
        display_name="Lead B",
        email="lead-b@example.invalid",
        team_id="team-b",
        team_role=TeamAccountRole.ADMIN,
    )
    project = ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="Project work execution contract test",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    # ProjectDirectoryService creates the owner team.  Add the provider team
    # through the same authoritative participation table used by admission.
    with engine.begin() as connection:
        connection.execute(
            insert(PROJECT_TEAMS).values(
                project_id=project.project_id,
                team_id="team-b",
                name="Provider",
                kind=ProjectTeamKind.ENGINEERING.value,
                assigned_by="lead-a",
                created_at=NOW,
            )
        )

    collaboration = TeamCollaborationService(engine)
    collaboration.create_task(
        task_id="task-a",
        project_id="project-a",
        actor_id="lead-a",
        target_team_id="team-b",
        title="Review",
        description="Review the contracted project input",
        acceptance_criteria="Produce the contracted review output",
    )

    graph_repository = SQLAlchemyWorkGraphRepository(engine)
    graph_repository.create_schema()
    graph = ProjectWorkGraphService(graph_repository)
    graph.register_existing_subject(
        node_id="node:task:task-a",
        project_id="project-a",
        node_type="task",
        subject_id="task-a",
    )

    process_repository = SQLAlchemyProjectProcessRepository(engine)
    process_repository.create_schema()
    process_service = ProjectProcessService(process_repository, clock=lambda: NOW)
    process_service.create_policy(
        policy_id="policy-a",
        project_id="project-a",
        max_agent_runs=10,
        max_total_tokens=200_000,
        max_model_cost_microusd=20_000_000,
        max_replans=3,
        max_generated_tasks=20,
        max_active_agent_runs=4,
        max_active_runs_per_team=2,
        max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=NOW + timedelta(days=30),
        version=1,
    )
    process = process_service.start_process(
        process_id="process-a",
        project_id="project-a",
        execution_policy_id="policy-a",
        started_by="lead-a",
    )
    _activate_process(process_repository, process.process_id)

    contract = TeamTaskContractService(engine)
    contract.propose(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-a",
        expected_version=0,
        process_id="process-a",
        work_node_id="node:task:task-a",
        requested_capability={
            "tags": ["review"],
            "protocol": "a2a-1.0",
            "input_contract_ref": "urn:contract:review-input:v1",
            "output_contract_ref": "urn:contract:review-output:v1",
            "verification_policy_ref": "verification:review:v1",
        },
        input_manifest={"resources": [], "work_nodes": []},
        output_contract={
            "artifact_types": ["text/plain"],
            "required": True,
            "max_count": 2,
        },
        verification_policy={
            "criteria": [
                {"criterion_id": "review", "type": "agent_review", "required": True}
            ]
        },
        autonomy_requirement="supervised",
    )
    collaboration.respond_task(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-b",
        accept=True,
        expected_contract_version=1,
    )
    # Contract acceptance alone is not executable.  The project-work boundary
    # consumes the authoritative in-progress TeamTask state.
    collaboration.assign_internal(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-b",
        account_id="lead-b",
    )
    collaboration.start_task(
        project_id="project-a", task_id="task-a", actor_id="lead-b"
    )

    execution_repository = SQLAlchemyTaskRepository(engine=engine)
    execution_repository.create_schema()
    execution = TaskExecutionService(
        repository=execution_repository,
        governance=_UnusedGovernance(),  # type: ignore[arg-type]
        project_repository=process_repository,
        work_graph_repository=graph_repository,
    )
    return SimpleNamespace(
        engine=engine,
        accounts=accounts,
        collaboration=collaboration,
        contract=contract,
        execution=execution,
        execution_repository=execution_repository,
        graph=graph,
        graph_repository=graph_repository,
        process=process,
        process_repository=process_repository,
    )


def _activate_process(repository: SQLAlchemyProjectProcessRepository, process_id: str) -> None:
    with repository.transaction() as connection:
        connection.execute(
            PROJECT_INPUT_REQUESTS.update()
            .where(PROJECT_INPUT_REQUESTS.c.process_id == process_id)
            .values(
                status="CANCELLED",
                answered_at=NOW,
                answered_by="lead-a",
                resolution_idempotency_key=f"fixture:{process_id}:input",
                resolution_event_id=f"fixture:{process_id}:input:closed",
                resolution_sha256="a" * 64,
            )
        )
        connection.execute(
            PROJECT_PROCESSES.update()
            .where(PROJECT_PROCESSES.c.process_id == process_id)
            .values(phase="EXECUTION", status="READY", wait_reason="NONE")
        )


def _enqueue(value: SimpleNamespace, *, principal=TARGET_PRINCIPAL, **overrides):
    arguments = {
        "principal": principal,
        "idempotency_key": "project-work:enqueue-1",
        "process_id": "process-a",
        "team_task_id": "task-a",
        "work_node_id": "node:task:task-a",
        "contract_version": 1,
    }
    arguments.update(overrides)
    return value.execution.enqueue_project_work(**arguments)


def _assert_project_execution_task(task: ExecutionTask) -> None:
    assert isinstance(task, ExecutionTask)
    assert task.tenant_id == "team-b"
    assert task.created_by == "lead-b"
    assert task.assignment_id is None
    assert task.project_id == "project-a"
    assert task.process_id == "process-a"
    assert task.team_task_id == "task-a"
    assert task.work_node_id == "node:task:task-a"
    assert task.contract_version == 1
    assert task.queue == "team-b"
    assert task.payload["project_id"] == "project-a"
    assert task.payload["process_id"] == "process-a"
    assert task.payload["team_task_id"] == "task-a"
    assert task.payload["work_node_id"] == "node:task:task-a"
    assert task.payload["contract_version"] == 1


def _set_task_status(value: SimpleNamespace, status: str) -> None:
    with value.engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.update()
            .where(TEAM_TASKS.c.task_id == "task-a")
            .values(status=status)
        )


def _execution_row_count(value: SimpleNamespace) -> int:
    with value.engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(EXECUTION_TASKS)).scalar_one())


@project_api_required
@pytest.mark.parametrize("task_state", ["accepted", "in_progress"])
def test_project_work_accepts_only_the_authoritative_runnable_task_states(task_state: str):
    value = _project_stack()
    _set_task_status(value, task_state)
    if task_state == "in_progress":
        task = _enqueue(value, idempotency_key=f"project-work:{task_state}")
        _assert_project_execution_task(task)
        return

    with pytest.raises(
        PROJECT_ERRORS,
        match="project work status must be in_progress",
    ):
        _enqueue(value, idempotency_key=f"project-work:{task_state}")
    assert _execution_row_count(value) == 0


@project_api_required
@pytest.mark.parametrize(
    "task_state",
    ["proposed", "submitted", "verified", "changes_requested", "rejected"],
)
def test_non_runnable_task_states_are_rejected(task_state: str):
    value = _project_stack()
    _set_task_status(value, task_state)

    with pytest.raises(
        PROJECT_ERRORS,
        match="project work status must be in_progress",
    ):
        _enqueue(value, idempotency_key=f"project-work:invalid:{task_state}")

    assert _execution_row_count(value) == 0


@project_api_required
def test_project_work_requires_every_work_graph_dependency_to_be_verified():
    value = _project_stack()
    value.collaboration.create_task(
        task_id="task-dependency",
        project_id="project-a",
        actor_id="lead-a",
        target_team_id="team-b",
        title="Prerequisite",
        description="Prepare the prerequisite",
        acceptance_criteria="Prerequisite is verified",
    )
    value.graph.register_existing_subject(
        node_id="node:task:task-dependency",
        project_id="project-a",
        node_type="task",
        subject_id="task-dependency",
    )
    value.graph.add_relation(
        relation_id="relation:task-dependency",
        project_id="project-a",
        source_node_id="node:task:task-a",
        relation_type=WorkRelationType.DEPENDS_ON,
        target_node_id="node:task:task-dependency",
        created_by_type="human",
        created_by_id="lead-a",
    )

    with pytest.raises(PROJECT_ERRORS, match="project work dependencies are not verified"):
        _enqueue(value, idempotency_key="project-work:dependency-blocked")

    with value.engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.update()
            .where(TEAM_TASKS.c.task_id == "task-dependency")
            .values(status="verified")
        )
    task = _enqueue(value, idempotency_key="project-work:dependency-ready")
    _assert_project_execution_task(task)


@project_api_required
def test_project_work_checks_exact_task_node_and_accepted_contract_version():
    value = _project_stack()

    with pytest.raises(PROJECT_ERRORS, match="project work process or node binding changed"):
        _enqueue(value, work_node_id="node:task:other")

    with pytest.raises(
        PROJECT_ERRORS,
        match="version-pinned accepted project work contract",
    ):
        _enqueue(value, contract_version=2)


@project_api_required
def test_project_work_requires_target_team_to_match_principal_tenant():
    value = _project_stack()

    with pytest.raises(
        (PolicyDenied, GovernanceConflictError),
        match="only the target team may enqueue project work",
    ):
        _enqueue(value, principal=SOURCE_PRINCIPAL)


@project_api_required
def test_project_work_rejects_a_process_from_another_project():
    value = _project_stack()
    directory = ProjectDirectoryService(value.engine)
    directory.create_project(
        project_id="project-b",
        name="Project B",
        description="Unrelated project",
        actor_id="lead-a",
        owner_assignment_name="Owner B",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    process_service = ProjectProcessService(value.process_repository, clock=lambda: NOW)
    process_service.create_policy(
        policy_id="policy-b",
        project_id="project-b",
        max_agent_runs=10,
        max_total_tokens=200_000,
        max_model_cost_microusd=20_000_000,
        max_replans=3,
        max_generated_tasks=20,
        max_active_agent_runs=4,
        max_active_runs_per_team=2,
        max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=NOW + timedelta(days=30),
        version=1,
    )
    process_service.start_process(
        process_id="process-b",
        project_id="project-b",
        execution_policy_id="policy-b",
        started_by="lead-a",
    )
    _activate_process(value.process_repository, "process-b")

    with pytest.raises(PROJECT_ERRORS, match="project work is absent or hidden"):
        _enqueue(value, process_id="process-b")


@project_api_required
@pytest.mark.parametrize(
    ("status", "wait_reason", "phase"),
    [
        ("WAITING", "HUMAN_APPROVAL", "EXECUTION"),
        ("BLOCKED", "DEPENDENCY", "EXECUTION"),
        ("COMPLETED", "NONE", "TERMINAL"),
        ("FAILED", "NONE", "TERMINAL"),
        ("CANCELLED", "NONE", "TERMINAL"),
    ],
)
def test_project_work_requires_an_active_non_waiting_process(
    status: str, wait_reason: str, phase: str
):
    value = _project_stack()
    with value.engine.begin() as connection:
        connection.execute(
            PROJECT_PROCESSES.update()
            .where(PROJECT_PROCESSES.c.process_id == "process-a")
            .values(
                status=status,
                wait_reason=wait_reason,
                phase=phase,
                completed_at=NOW if phase == "TERMINAL" else None,
            )
        )

    with pytest.raises(
        PROJECT_ERRORS,
        match="project process cannot admit execution work",
    ):
        _enqueue(value, idempotency_key=f"project-work:process:{status.lower()}")


@project_api_required
@pytest.mark.parametrize(
    ("status", "phase"),
    [
        ("READY", "PLANNING"),
        ("READY", "INTEGRATION"),
        ("RUNNING", "PLANNING"),
        ("RUNNING", "INTEGRATION"),
    ],
)
def test_project_work_requires_execution_phase_even_when_process_status_is_active(
    status: str, phase: str
):
    value = _project_stack()
    with value.engine.begin() as connection:
        connection.execute(
            PROJECT_PROCESSES.update()
            .where(PROJECT_PROCESSES.c.process_id == "process-a")
            .values(status=status, phase=phase, wait_reason="NONE")
        )

    with pytest.raises(
        PROJECT_ERRORS,
        match="project work execution requires the EXECUTION process phase",
    ):
        _enqueue(value, idempotency_key=f"project-work:phase:{status.lower()}:{phase.lower()}")


@project_api_required
def test_project_work_idempotent_retry_returns_the_same_execution_task():
    value = _project_stack()
    first = _enqueue(value)
    second = _enqueue(value)

    assert second == first
    assert second.task_id == first.task_id
    assert _execution_row_count(value) == 1


@project_api_required
def test_project_work_concurrent_retries_create_one_task(tmp_path: Path):
    value = _project_stack(tmp_path)
    barrier = Barrier(2)

    def enqueue_from_replica(_index: int) -> ExecutionTask:
        replica_repository = SQLAlchemyTaskRepository(engine=value.engine)
        replica = TaskExecutionService(
            repository=replica_repository,
            governance=_UnusedGovernance(),  # type: ignore[arg-type]
            project_repository=value.process_repository,
            work_graph_repository=value.graph_repository,
        )
        barrier.wait()
        return replica.enqueue_project_work(
            principal=TARGET_PRINCIPAL,
            idempotency_key="project-work:concurrent",
            process_id="process-a",
            team_task_id="task-a",
            work_node_id="node:task:task-a",
            contract_version=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(enqueue_from_replica, (1, 2)))

    assert first == second
    _assert_project_execution_task(first)
    assert _execution_row_count(value) == 1


@project_api_required
def test_project_work_concurrent_submissions_with_different_idempotency_keys_converge(
    tmp_path: Path,
):
    value = _project_stack(tmp_path)
    barrier = Barrier(2)

    def enqueue_from_replica(idempotency_key: str) -> ExecutionTask:
        replica_repository = SQLAlchemyTaskRepository(engine=value.engine)
        replica = TaskExecutionService(
            repository=replica_repository,
            governance=_UnusedGovernance(),  # type: ignore[arg-type]
            project_repository=value.process_repository,
            work_graph_repository=value.graph_repository,
        )
        barrier.wait()
        return replica.enqueue_project_work(
            principal=TARGET_PRINCIPAL,
            idempotency_key=idempotency_key,
            process_id="process-a",
            team_task_id="task-a",
            work_node_id="node:task:task-a",
            contract_version=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(
            pool.map(
                enqueue_from_replica,
                ("project-work:contract-a", "project-work:contract-b"),
            )
        )

    assert first == second
    _assert_project_execution_task(first)
    assert _execution_row_count(value) == 1


@project_api_required
def test_project_work_path_does_not_delegate_to_legacy_assignment_enqueue(monkeypatch):
    value = _project_stack()

    def fail_legacy(*_args, **_kwargs):
        raise AssertionError("legacy assignment enqueue was called")

    monkeypatch.setattr(value.execution, "enqueue_assignment", fail_legacy)
    task = _enqueue(value, idempotency_key="project-work:no-legacy")
    _assert_project_execution_task(task)


def test_legacy_enqueue_assignment_remains_compatible():
    from test_execution_service import GovernanceView, in_progress_board

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyTaskRepository(engine=engine)
    repository.create_schema()
    service = TaskExecutionService(
        repository=repository,
        governance=GovernanceView(in_progress_board()),  # type: ignore[arg-type]
    )

    task = service.enqueue_assignment(
        principal=Principal("worker-b", "team-b"),
        idempotency_key="legacy:assignment",
        task_id="legacy-execution-1",
        program_id="program-1",
        assignment_id="assignment-1",
        queue="coding",
        payload={"operation": "legacy-compatible"},
    )

    assert task.assignment_id == "assignment-1"
    assert task.program_id == "program-1"
