from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from coifesp_harness.verification.repository import (
    TASK_VERIFICATIONS,
    VERIFICATION_METADATA,
)

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260830_55_task_verifications.py"
)
_COLUMNS = (
    "verification_id",
    "project_id",
    "process_id",
    "task_id",
    "source_run_id",
    "contract_version",
    "subject_digest",
    "policy_json",
    "artifacts_json",
    "checks_json",
    "status",
    "initiated_by",
    "executed_as",
    "version",
    "created_at",
    "updated_at",
    "completed_at",
)
_CHECK_NAMES = {
    "ck_task_verifications_contract_version",
    "ck_task_verifications_subject_digest",
    "ck_task_verifications_status",
    "ck_task_verifications_version",
    "ck_task_verifications_completion",
}
_UNIQUE_NAMES = {"uq_task_verifications_source_subject"}
_INDEX_NAMES = {
    "ix_task_verifications_project_task_created",
    "ix_task_verifications_status_created",
}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "task_verifications_revision_55", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _migrate(engine, direction: str) -> None:
    migration = _migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, direction)()


def _engine():
    return create_engine("sqlite+pysqlite://", poolclass=StaticPool)


def _base_row(verification_id: str = "verification-1", **overrides):
    row = {
        "verification_id": verification_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "task_id": "task-1",
        "source_run_id": "run-1",
        "contract_version": 1,
        "subject_digest": "a" * 64,
        "policy_json": {"required": ["unit"]},
        "artifacts_json": [{"artifact_id": "artifact-1", "digest": "b" * 64}],
        "checks_json": {"unit": {"status": "PASS"}},
        "status": "PENDING",
        "initiated_by": "service:project-orchestrator",
        "executed_as": "team-agent:quality-1",
        "version": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def test_repository_metadata_is_standalone_and_matches_contract():
    assert TASK_VERIFICATIONS.metadata is VERIFICATION_METADATA
    assert tuple(TASK_VERIFICATIONS.columns.keys()) == _COLUMNS
    assert not TASK_VERIFICATIONS.foreign_keys
    assert {
        constraint.name
        for constraint in TASK_VERIFICATIONS.constraints
        if isinstance(constraint, CheckConstraint)
    } == _CHECK_NAMES
    assert {
        constraint.name
        for constraint in TASK_VERIFICATIONS.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    } == _UNIQUE_NAMES

    for name in ("project_id", "process_id", "task_id", "source_run_id"):
        assert TASK_VERIFICATIONS.c[name].type.length == 128
        assert TASK_VERIFICATIONS.c[name].nullable is False
    assert TASK_VERIFICATIONS.c.verification_id.type.length == 128
    assert TASK_VERIFICATIONS.c.subject_digest.type.length == 64
    assert TASK_VERIFICATIONS.c.status.type.length == 16
    assert TASK_VERIFICATIONS.c.initiated_by.type.length == 256
    assert TASK_VERIFICATIONS.c.executed_as.type.length == 256
    for name in ("contract_version", "version"):
        assert isinstance(TASK_VERIFICATIONS.c[name].type, Integer)
        assert TASK_VERIFICATIONS.c[name].nullable is False
    for name in ("policy_json", "artifacts_json", "checks_json"):
        assert isinstance(TASK_VERIFICATIONS.c[name].type, JSON)
        assert TASK_VERIFICATIONS.c[name].type.none_as_null is True
        assert TASK_VERIFICATIONS.c[name].nullable is False
    for name in ("created_at", "updated_at", "completed_at"):
        assert isinstance(TASK_VERIFICATIONS.c[name].type, DateTime)
        assert TASK_VERIFICATIONS.c[name].type.timezone is True
    assert TASK_VERIFICATIONS.c.completed_at.nullable is True

    assert {index.name for index in TASK_VERIFICATIONS.indexes} == _INDEX_NAMES
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in TASK_VERIFICATIONS.indexes
    } == {
        "ix_task_verifications_project_task_created": (
            "project_id",
            "task_id",
            "created_at",
        ),
        "ix_task_verifications_status_created": ("status", "created_at"),
    }


def test_revision_55_upgrade_matches_runtime_metadata_and_empty_downgrade_roundtrip():
    runtime_engine = _engine()
    VERIFICATION_METADATA.create_all(runtime_engine)
    migrated_engine = _engine()
    _migrate(migrated_engine, "upgrade")

    runtime_inspector = inspect(runtime_engine)
    migrated_inspector = inspect(migrated_engine)
    assert "task_verifications" in migrated_inspector.get_table_names()
    assert {
        column["name"]
        for column in migrated_inspector.get_columns("task_verifications")
    } == set(_COLUMNS)
    assert {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in migrated_inspector.get_columns("task_verifications")
    } == {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in runtime_inspector.get_columns("task_verifications")
    }
    assert {
        item["name"]
        for item in migrated_inspector.get_check_constraints("task_verifications")
    } == _CHECK_NAMES
    assert {
        item["name"]
        for item in migrated_inspector.get_unique_constraints("task_verifications")
        if item["name"]
    } == _UNIQUE_NAMES
    assert {
        item["name"] for item in migrated_inspector.get_indexes("task_verifications")
    } == _INDEX_NAMES
    assert migrated_inspector.get_foreign_keys("task_verifications") == []

    _migrate(migrated_engine, "downgrade")
    assert "task_verifications" not in inspect(migrated_engine).get_table_names()
    _migrate(migrated_engine, "upgrade")
    assert "task_verifications" in inspect(migrated_engine).get_table_names()
    runtime_engine.dispose()
    migrated_engine.dispose()


def test_valid_pending_and_terminal_rows_are_storable():
    engine = _engine()
    _migrate(engine, "upgrade")
    terminal_statuses = ("PASS", "FAIL", "STALE")
    with engine.begin() as connection:
        connection.execute(TASK_VERIFICATIONS.insert().values(_base_row()))
        for index, status in enumerate(terminal_statuses, start=1):
            connection.execute(
                TASK_VERIFICATIONS.insert().values(
                    _base_row(
                        f"verification-{index + 1}",
                        source_run_id=f"run-{index + 1}",
                        subject_digest=chr(96 + index + 1) * 64,
                        status=status,
                        completed_at=_NOW,
                    )
                )
            )
    with engine.connect() as connection:
        assert connection.execute(
            TASK_VERIFICATIONS.select()
            .with_only_columns(TASK_VERIFICATIONS.c.status)
            .order_by(TASK_VERIFICATIONS.c.verification_id)
        ).scalars().all() == ["PENDING", "PASS", "FAIL", "STALE"]
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract_version": 0},
        {"contract_version": -1},
        {"subject_digest": "a" * 63},
        {"subject_digest": "a" * 65},
        {"status": "UNKNOWN"},
        {"status": "PENDING", "completed_at": _NOW},
        {"status": "PASS", "completed_at": None},
        {"status": "FAIL", "completed_at": None},
        {"status": "STALE", "completed_at": None},
        {"version": 0},
        {"version": -1},
        {"policy_json": None},
        {"artifacts_json": None},
        {"checks_json": None},
    ],
)
def test_sqlite_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            TASK_VERIFICATIONS.insert().values(
                _base_row(
                    verification_id="invalid",
                    **overrides,
                )
            )
        )
    engine.dispose()


def test_source_run_and_subject_digest_are_unique():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(TASK_VERIFICATIONS.insert().values(_base_row()))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            TASK_VERIFICATIONS.insert().values(_base_row("verification-2"))
        )
    engine.dispose()


def test_downgrade_refuses_any_existing_evidence_before_schema_mutation():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(TASK_VERIFICATIONS.insert().values(_base_row()))
    before_indexes = {
        item["name"] for item in inspect(engine).get_indexes("task_verifications")
    }

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260830_55"):
        _migrate(engine, "downgrade")

    inspector = inspect(engine)
    assert "task_verifications" in inspector.get_table_names()
    assert {
        item["name"] for item in inspector.get_indexes("task_verifications")
    } == before_indexes
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(TASK_VERIFICATIONS.c.verification_id)
            ).scalar_one()
            == "verification-1"
        )
    engine.dispose()


def test_postgresql_create_table_ddl_has_constraints_and_no_cross_metadata_fks():
    ddl = str(CreateTable(TASK_VERIFICATIONS).compile(dialect=postgresql.dialect()))
    assert "CREATE TABLE task_verifications" in ddl
    assert "verification_id VARCHAR(128) NOT NULL" in ddl
    assert "policy_json JSON NOT NULL" in ddl
    assert "completed_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "CONSTRAINT ck_task_verifications_completion CHECK" in ddl
    assert "CONSTRAINT uq_task_verifications_source_subject UNIQUE" in ddl
    assert "FOREIGN KEY" not in ddl


def test_postgresql_upgrade_compiles_offline():
    from io import StringIO

    output = StringIO()
    migration = _migration()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name="postgresql",
            opts={"as_sql": True, "output_buffer": output},
        )
    )
    migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE task_verifications" in sql
    assert "CREATE INDEX ix_task_verifications_project_task_created" in sql
    assert "CREATE INDEX ix_task_verifications_status_created" in sql
