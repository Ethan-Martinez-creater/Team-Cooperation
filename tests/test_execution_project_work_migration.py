from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex

from coifesp_harness.control_plane.bootstrap import SCHEMA_REVISION
from coifesp_harness.execution.repository import EXECUTION_TASKS

MIGRATION = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260901_62_execution_project_work_binding.py"
)
PROJECT_COLUMNS = {
    "project_id",
    "process_id",
    "team_task_id",
    "work_node_id",
    "contract_version",
}
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _module():
    spec = spec_from_file_location("execution_project_work_62", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(engine, direction):
    with engine.begin() as connection:
        module = _module()
        module.op = Operations(MigrationContext.configure(connection))
        getattr(module, direction)()


def _legacy_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    Table(
        "execution_tasks",
        metadata,
        Column("tenant_id", String(128), primary_key=True),
        Column("task_id", String(128), primary_key=True),
        Column("program_id", String(128)),
        Column("assignment_id", String(128)),
        Column("queue", String(128), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("request_digest", String(64), nullable=False),
        Column("idempotency_key", String(128), nullable=False),
        Column("status", String(32), nullable=False),
        Column("priority", Integer, nullable=False),
        Column("max_attempts", Integer, nullable=False),
        Column("attempt_count", Integer, nullable=False),
        Column("available_at", DateTime(timezone=True), nullable=False),
        Column("lease_owner", String(128)),
        Column("lease_token", String(128)),
        Column("lease_expires_at", DateTime(timezone=True)),
        Column("cancel_requested", Boolean, nullable=False),
        Column("result", JSON),
        Column("error_code", String(128)),
        Column("created_by", String(128), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("completed_at", DateTime(timezone=True)),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["execution_tasks"].insert().values(
                tenant_id="team-a",
                task_id="legacy-task",
                program_id="program-a",
                assignment_id="assignment-a",
                queue="legacy",
                payload={},
                request_digest="a" * 64,
                idempotency_key="legacy-key",
                status="queued",
                priority=0,
                max_attempts=3,
                attempt_count=0,
                available_at=NOW,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                cancel_requested=False,
                result=None,
                error_code=None,
                created_by="account-a",
                created_at=NOW,
                updated_at=NOW,
                completed_at=None,
            )
        )
    return engine


def _project_values(task_id="project-work:a", idempotency_key="project-key"):
    return {
        "tenant_id": "team-a",
        "task_id": task_id,
        "program_id": None,
        "assignment_id": None,
        "project_id": "project-a",
        "process_id": "process-a",
        "team_task_id": "team-task-a",
        "work_node_id": "node-a",
        "contract_version": 1,
        "queue": "team-a",
        "payload": {},
        "request_digest": "b" * 64,
        "idempotency_key": idempotency_key,
        "status": "queued",
        "priority": 0,
        "max_attempts": 3,
        "attempt_count": 0,
        "available_at": NOW,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "cancel_requested": False,
        "result": None,
        "error_code": None,
        "created_by": "account-a",
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
    }


def test_upgrade_preserves_legacy_rows_and_matches_runtime_metadata():
    engine = _legacy_engine()
    _migrate(engine, "upgrade")
    inspector = inspect(engine)
    assert PROJECT_COLUMNS <= {item["name"] for item in inspector.get_columns("execution_tasks")}
    assert "ck_execution_tasks_project_work_binding" in {
        item["name"] for item in inspector.get_check_constraints("execution_tasks")
    }
    assert {
        "uq_execution_tasks_project_contract",
        "ix_execution_tasks_project_work",
    } <= {item["name"] for item in inspector.get_indexes("execution_tasks")}
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT * FROM execution_tasks WHERE task_id='legacy-task'")
        ).mappings().one()
    assert row["program_id"] == "program-a"
    assert all(row[name] is None for name in PROJECT_COLUMNS)


def test_project_binding_check_and_unique_contract_are_enforced():
    engine = _legacy_engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(EXECUTION_TASKS.insert().values(_project_values()))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            EXECUTION_TASKS.insert().values(
                _project_values("project-work:b", "project-key-b")
            )
        )
    invalid = _project_values("project-work:c", "project-key-c") | {
        "work_node_id": None
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(EXECUTION_TASKS.insert().values(invalid))


def test_project_binding_cannot_mix_governance_assignment():
    engine = _legacy_engine()
    _migrate(engine, "upgrade")
    mixed = _project_values() | {
        "program_id": "program-a",
        "assignment_id": "assignment-a",
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(EXECUTION_TASKS.insert().values(mixed))


def test_downgrade_is_lossless_without_project_rows_and_guarded_with_them():
    engine = _legacy_engine()
    _migrate(engine, "upgrade")
    _migrate(engine, "downgrade")
    assert PROJECT_COLUMNS.isdisjoint(
        {item["name"] for item in inspect(engine).get_columns("execution_tasks")}
    )
    guarded = _legacy_engine()
    _migrate(guarded, "upgrade")
    with guarded.begin() as connection:
        connection.execute(EXECUTION_TASKS.insert().values(_project_values()))
    with pytest.raises(RuntimeError, match="cannot be represented"):
        _migrate(guarded, "downgrade")


def test_postgresql_runtime_indexes_include_partial_project_contract_uniqueness():
    indexes = {item.name: item for item in EXECUTION_TASKS.indexes}
    unique = str(
        CreateIndex(indexes["uq_execution_tasks_project_contract"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "UNIQUE INDEX" in unique
    assert "WHERE process_id IS NOT NULL" in unique


def test_revision_62_is_the_single_bootstrap_head():
    module = _module()
    assert module.revision == "20260901_62"
    assert module.down_revision == "20260831_61"
    assert ScriptDirectory(str(Path(__file__).parents[1] / "alembic")).get_heads() == [
        SCHEMA_REVISION
    ]
