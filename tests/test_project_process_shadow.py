from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.product.demo_bootstrap import DEMO_PROJECT_ID, ensure_local_demo
from coifesp_harness.project_process import (
    ProjectProcessPhase,
    ProjectProcessShadowAdapter,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.project_process.gates import ProjectInputRequestStatus
from coifesp_harness.project_process.repository import (
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESS_EVENTS,
    PROJECT_PROCESS_OUTBOX,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    accounts.register_team(team_id="team-a", team_handle="team-a", team_name="Team A")
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    repository = SQLAlchemyProjectProcessRepository(engine)
    repository.create_schema()
    shadow = ProjectProcessShadowAdapter(repository, clock=lambda: NOW)
    directory = ProjectDirectoryService(engine, process_shadow=shadow)
    project = directory.create_project(
        project_id="project-a",
        name="Project A",
        description="Build the collaboration harness",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    return engine, repository, shadow, project


def test_project_creation_atomically_seeds_shadow_process_input_event_and_outbox():
    _, repository, _, project = _stack()
    with repository.transaction() as connection:
        process = repository.process(connection, f"process:{project.project_id}")
        request = repository.input_request(
            connection, f"process:{project.project_id}:goal-input"
        )
        events = repository.events(connection, process.process_id)
        outbox_count = connection.execute(
            select(func.count()).select_from(PROJECT_PROCESS_OUTBOX)
        ).scalar_one()
    assert (process.phase, process.status, process.wait_reason) == (
        ProjectProcessPhase.INTAKE,
        ProjectProcessStatus.WAITING,
        ProjectProcessWaitReason.HUMAN_INPUT,
    )
    assert request.status is ProjectInputRequestStatus.OPEN
    assert [event.event_type for event in events] == ["project.input.requested"]
    assert outbox_count == 1


def test_local_demo_project_uses_the_same_shadow_process_creation_path():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProductAccountService(engine).create_schema()
    repository = SQLAlchemyProjectProcessRepository(engine)
    repository.create_schema()
    shadow = ProjectProcessShadowAdapter(repository, clock=lambda: NOW)

    ensure_local_demo(engine=engine, process_shadow=shadow)

    with repository.transaction() as connection:
        process = repository.process(connection, f"process:{DEMO_PROJECT_ID}")
        events = repository.events(connection, process.process_id)
    assert (process.phase, process.status, process.wait_reason) == (
        ProjectProcessPhase.INTAKE,
        ProjectProcessStatus.WAITING,
        ProjectProcessWaitReason.HUMAN_INPUT,
    )
    assert [event.event_type for event in events] == ["project.input.requested"]


def test_team_task_fact_is_appended_without_inventing_a_process_transition():
    _, repository, shadow, project = _stack()
    with repository.transaction() as connection:
        before = repository.process(connection, f"process:{project.project_id}")
        after, event = shadow.on_team_task_changed(
            connection,
            project_id=project.project_id,
            task_id="task-a",
            actor_id="lead-a",
            activity_type="task.accepted",
            occurred_at=NOW,
        )
    assert event.event_type == "team_task.accepted"
    assert event.transition_key is None
    assert event.subject_id == "task-a"
    assert after.version == before.version
    assert after.last_event_sequence == before.last_event_sequence + 1

    with repository.transaction() as connection:
        scheduled, schedule_event = shadow.on_team_task_changed(
            connection,
            project_id=project.project_id,
            task_id="task-a",
            actor_id="lead-a",
            activity_type="task_schedule_changed",
            occurred_at=NOW + timedelta(seconds=1),
            source_aggregate_version=2,
        )
    assert schedule_event.event_type == "task.schedule.changed"
    assert schedule_event.source_aggregate_version == 2
    assert scheduled.version == before.version
    assert scheduled.last_event_sequence == before.last_event_sequence + 2


def test_plan_approval_shadow_projection_reaches_execution_ready_idempotently():
    engine, _, shadow, project = _stack()
    with engine.begin() as connection:
        projected = shadow.on_plan_approved(
            connection,
            project_id=project.project_id,
            draft_id="draft-a",
            actor_id="lead-a",
            goal_summary="Deliver the project harness",
        )
    assert (projected.phase, projected.status, projected.wait_reason) == (
        ProjectProcessPhase.EXECUTION,
        ProjectProcessStatus.READY,
        ProjectProcessWaitReason.NONE,
    )
    assert projected.version == 5
    assert projected.last_event_sequence == 6
    assert projected.active_plan_id == "draft-a"
    with engine.begin() as connection:
        replayed = shadow.on_plan_approved(
            connection,
            project_id=project.project_id,
            draft_id="draft-a",
            actor_id="lead-a",
            goal_summary="Deliver the project harness",
        )
        request_status = connection.execute(
            select(PROJECT_INPUT_REQUESTS.c.status).where(
                PROJECT_INPUT_REQUESTS.c.request_id
                == f"process:{project.project_id}:goal-input"
            )
        ).scalar_one()
        event_count = connection.execute(
            select(func.count()).select_from(PROJECT_PROCESS_EVENTS)
        ).scalar_one()
        outbox_count = connection.execute(
            select(func.count()).select_from(PROJECT_PROCESS_OUTBOX)
        ).scalar_one()
    assert replayed.version == 5
    assert request_status == ProjectInputRequestStatus.ANSWERED.value
    assert event_count == 6
    assert outbox_count == 6
