from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
)
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.project_process import (
    ProjectOrchestrationDecisionStatus,
    ProjectProcessCommandService,
    ProjectProcessCommandType,
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)
GRAPH = "sha256:" + "a" * 64


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
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="Decision persistence test",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    repository = SQLAlchemyProjectProcessRepository(engine)
    repository.create_schema()
    service = ProjectProcessService(repository, clock=lambda: NOW)
    service.create_policy(
        policy_id="policy-a",
        project_id="project-a",
        max_agent_runs=10,
        max_total_tokens=10000,
        max_model_cost_microusd=100000,
        max_replans=3,
        max_generated_tasks=20,
        max_active_agent_runs=4,
        max_active_runs_per_team=2,
        max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=NOW + timedelta(days=30),
        version=1,
    )
    process = service.start_process(
        process_id="process-a",
        project_id="project-a",
        execution_policy_id="policy-a",
        started_by="lead-a",
    )
    return engine, repository, process


def _command(command_id="command-a", request=None):
    return {
        "command_id": command_id,
        "command_type": ProjectProcessCommandType.PROPOSE_TASK,
        "request": request or {"task_id": "task-a", "title": "Build"},
    }


def _record(service, **overrides):
    values = {
        "decision_id": "decision-a",
        "process_id": "process-a",
        "reason": "dispatch ready work",
        "based_on_process_version": 1,
        "based_on_event_sequence": 1,
        "graph_snapshot_digest": GRAPH,
        "decision_json": {
            "action": "dispatch_work",
            "reason": "dispatch ready work",
            "work_id": "task-a",
            "transition_key": "work.dispatched",
        },
        "commands": [_command()],
    }
    values.update(overrides)
    return service.record_decision(**values)


def test_fresh_batch_persists_decision_payload_commands_and_advances_cursor():
    _, repository, process = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)

    decision = _record(service)

    assert decision.status is ProjectOrchestrationDecisionStatus.PENDING
    assert decision.decision_json["action"] == "dispatch_work"
    assert len(decision.decision_digest) == 64
    with repository.transaction() as connection:
        stored_process = repository.process(connection, process.process_id)
        commands = repository.commands_for_decision(connection, decision.decision_id)
        stored_decision = repository.decision(connection, decision.decision_id)
    assert stored_process.last_orchestration_sequence == 1
    assert len(commands) == 1
    assert commands[0].request_json == {"task_id": "task-a", "title": "Build"}
    assert stored_decision == decision


def test_empty_batch_is_valid_and_advances_cursor_once():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)

    decision = _record(service, decision_id="decision-empty", commands=[])
    replay = _record(service, decision_id="decision-empty", commands=[])

    assert decision == replay
    with repository.transaction() as connection:
        assert repository.commands_for_decision(connection, decision.decision_id) == ()
        assert repository.process(connection, "process-a").last_orchestration_sequence == 1


def test_stale_batch_persists_only_terminal_decision_and_does_not_move_cursor():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)

    stale = _record(
        service,
        decision_id="decision-stale",
        based_on_event_sequence=0,
        commands=[_command("command-stale")],
    )

    assert stale.status is ProjectOrchestrationDecisionStatus.STALE
    with repository.transaction() as connection:
        assert repository.commands_for_decision(connection, stale.decision_id) == ()
        assert repository.process(connection, "process-a").last_orchestration_sequence == 0


def test_duplicate_batch_converges_without_rewriting_decision_or_cursor():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)
    first = _record(service)

    replay = _record(service)

    assert replay == first
    with repository.transaction() as connection:
        assert len(repository.decisions_for_process(connection, "process-a")) == 1
        assert len(repository.commands_for_decision(connection, "decision-a")) == 1


def test_decision_or_command_payload_conflicts_fail_closed():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)
    _record(service)

    with pytest.raises(GovernanceConflictError):
        _record(service, reason="changed reason")
    with pytest.raises(GovernanceConflictError):
        _record(
            service,
            decision_id="decision-b",
            commands=[_command("command-a", {"task_id": "other"})],
        )
    with repository.transaction() as connection:
        assert len(repository.decisions_for_process(connection, "process-a")) == 1
        assert len(repository.commands_for_decision(connection, "decision-a")) == 1


def test_invalid_second_command_rolls_back_entire_batch_and_cursor():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)

    with pytest.raises(ValueError, match="command type"):
        _record(
            service,
            decision_id="decision-invalid",
            commands=[
                _command("command-valid"),
                {"command_id": "command-invalid", "command_type": "not-a-command", "request": {}},
            ],
        )
    with repository.transaction() as connection:
        assert repository.decision(connection, "decision-invalid") is None
        assert repository.process(connection, "process-a").last_orchestration_sequence == 0


def test_decision_finish_is_idempotent_and_terminal_conflict_is_rejected():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)
    _record(service)

    applied = service.finish_decision(
        decision_id="decision-a", status=ProjectOrchestrationDecisionStatus.APPLIED
    )
    replay = service.finish_decision(
        decision_id="decision-a", status=ProjectOrchestrationDecisionStatus.APPLIED
    )
    assert applied == replay
    with pytest.raises(GovernanceConflictError):
        service.finish_decision(
            decision_id="decision-a", status=ProjectOrchestrationDecisionStatus.REJECTED
        )


def test_decision_finish_fence_failure_keeps_decision_pending():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)
    _record(service)

    def fence(_connection):
        raise GovernanceConflictError("stale wakeup lease")

    with pytest.raises(GovernanceConflictError, match="stale wakeup lease"):
        service.finish_decision(
            decision_id="decision-a",
            status=ProjectOrchestrationDecisionStatus.APPLIED,
            mutation_fence=fence,
        )
    with repository.transaction() as connection:
        assert repository.decision(connection, "decision-a").status is ProjectOrchestrationDecisionStatus.PENDING


def test_mutation_fence_failure_rolls_back_decision_and_cursor():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)

    def fence(_connection):
        raise GovernanceConflictError("stale wakeup lease")

    with pytest.raises(GovernanceConflictError, match="stale wakeup lease"):
        _record(service, mutation_fence=fence)
    with repository.transaction() as connection:
        assert repository.decision(connection, "decision-a") is None
        assert repository.process(connection, "process-a").last_orchestration_sequence == 0


def test_decision_json_forbidden_fields_are_rejected_without_writes():
    _, repository, _ = _stack()
    service = ProjectProcessCommandService(repository, clock=lambda: NOW)

    with pytest.raises(ValueError, match="forbidden"):
        _record(
            service,
            decision_id="decision-secret",
            decision_json={"action": "dispatch", "prompt": "do not persist"},
        )
    with repository.transaction() as connection:
        assert repository.decision(connection, "decision-secret") is None


def test_migration_49_upgrade_and_downgrade_on_sqlite():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    metadata = MetaData()
    Table(
        "project_processes",
        metadata,
        Column("process_id", String(128), primary_key=True),
        Column("project_id", String(128), nullable=False),
        UniqueConstraint("process_id", "project_id"),
    )
    Table(
        "project_process_commands",
        metadata,
        Column("command_id", String(128), primary_key=True),
        Column("process_id", String(128), nullable=False),
        Column("project_id", String(128), nullable=False),
        Column("decision_id", String(128), nullable=False),
        Column("command_type", String(64), nullable=False),
        Column("request_digest", String(64), nullable=False),
        Column("based_on_process_version", Integer, nullable=False),
        Column("based_on_event_sequence", Integer, nullable=False),
        Column("graph_snapshot_digest", String(71), nullable=False),
        Column("status", String(16), nullable=False),
        Column("result_subject_id", String(128)),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("applied_at", DateTime(timezone=True)),
    )
    metadata.create_all(engine)
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260829_49_project_orchestration_decisions.py"
    )
    spec = spec_from_file_location("project_migration_49", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        names = inspect(connection).get_table_names()
        assert "project_orchestration_decisions" in names
        assert "request_json" in {
            column["name"] for column in inspect(connection).get_columns("project_process_commands")
        }
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        names = inspect(connection).get_table_names()
        assert "project_orchestration_decisions" not in names
        assert "request_json" not in {
            column["name"] for column in inspect(connection).get_columns("project_process_commands")
        }
