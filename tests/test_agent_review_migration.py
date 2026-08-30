from __future__ import annotations

import hashlib
import importlib.util
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    Text,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from coifesp_harness.verification.repository import AGENT_REVIEWS

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260830_56_agent_reviews.py"
)
_COLUMNS = (
    "review_id",
    "verification_id",
    "source_run_id",
    "run_id",
    "project_id",
    "process_id",
    "task_id",
    "owner_team_id",
    "criterion_id",
    "criterion_key",
    "subject_digest",
    "contract_version",
    "attempt",
    "budget_reservation_id",
    "status",
    "result_json",
    "error_code",
    "initiated_by",
    "executed_as",
    "created_at",
    "updated_at",
    "completed_at",
)
_CHECK_NAMES = {
    "ck_task_agent_reviews_criterion_id",
    "ck_task_agent_reviews_criterion_key",
    "ck_task_agent_reviews_subject_digest",
    "ck_task_agent_reviews_contract_version",
    "ck_task_agent_reviews_attempt",
    "ck_task_agent_reviews_status",
    "ck_task_agent_reviews_completion",
    "ck_task_agent_reviews_result",
}
_UNIQUE_NAMES = {
    "uq_task_agent_reviews_run_id",
    "uq_task_agent_reviews_budget_reservation_id",
    "uq_task_agent_reviews_verification_criterion_attempt",
}
_INDEX_NAMES = {
    "ix_task_agent_reviews_verification_criterion_attempt",
    "ix_task_agent_reviews_status_created",
}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "agent_reviews_revision_56", _MIGRATION_PATH
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


def _criterion_key(criterion_id: str) -> str:
    return hashlib.sha256(criterion_id.encode("utf-8")).hexdigest()


def _base_row(review_id: str = "review-1", **overrides):
    criterion_id = "security-review"
    row = {
        "review_id": review_id,
        "verification_id": "verification-1",
        "source_run_id": "source-run-1",
        "run_id": "review-run-1",
        "project_id": "project-1",
        "process_id": "process-1",
        "task_id": "task-1",
        "owner_team_id": "team-quality",
        "criterion_id": criterion_id,
        "criterion_key": _criterion_key(criterion_id),
        "subject_digest": "a" * 64,
        "contract_version": 1,
        "attempt": 1,
        "budget_reservation_id": "budget-1",
        "status": "QUEUED",
        "result_json": None,
        "error_code": None,
        "initiated_by": "service:verification",
        "executed_as": "team-agent:quality-1",
        "created_at": _NOW,
        "updated_at": _NOW,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def test_repository_metadata_matches_contract():
    assert AGENT_REVIEWS.metadata.tables["task_agent_reviews"] is AGENT_REVIEWS
    assert tuple(AGENT_REVIEWS.columns.keys()) == _COLUMNS
    assert not AGENT_REVIEWS.foreign_keys
    assert {
        constraint.name
        for constraint in AGENT_REVIEWS.constraints
        if isinstance(constraint, CheckConstraint)
    } == _CHECK_NAMES
    assert {
        constraint.name
        for constraint in AGENT_REVIEWS.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    } == _UNIQUE_NAMES

    for name in (
        "review_id",
        "verification_id",
        "source_run_id",
        "run_id",
        "project_id",
        "process_id",
        "task_id",
        "owner_team_id",
        "budget_reservation_id",
    ):
        assert AGENT_REVIEWS.c[name].type.length == 128
        assert AGENT_REVIEWS.c[name].nullable is False
    assert isinstance(AGENT_REVIEWS.c.criterion_id.type, Text)
    assert AGENT_REVIEWS.c.criterion_id.nullable is False
    assert AGENT_REVIEWS.c.criterion_key.type.length == 64
    assert AGENT_REVIEWS.c.subject_digest.type.length == 64
    assert AGENT_REVIEWS.c.status.type.length == 16
    for name in ("initiated_by", "executed_as"):
        assert AGENT_REVIEWS.c[name].type.length == 256
        assert AGENT_REVIEWS.c[name].nullable is False
    for name in ("contract_version", "attempt"):
        assert isinstance(AGENT_REVIEWS.c[name].type, Integer)
        assert AGENT_REVIEWS.c[name].nullable is False
    assert isinstance(AGENT_REVIEWS.c.result_json.type, JSON)
    assert AGENT_REVIEWS.c.result_json.type.none_as_null is True
    assert AGENT_REVIEWS.c.result_json.nullable is True
    assert AGENT_REVIEWS.c.error_code.nullable is True
    for name in ("created_at", "updated_at", "completed_at"):
        assert isinstance(AGENT_REVIEWS.c[name].type, DateTime)
        assert AGENT_REVIEWS.c[name].type.timezone is True
    assert AGENT_REVIEWS.c.completed_at.nullable is True

    assert {index.name for index in AGENT_REVIEWS.indexes} == _INDEX_NAMES
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in AGENT_REVIEWS.indexes
    } == {
        "ix_task_agent_reviews_verification_criterion_attempt": (
            "verification_id",
            "criterion_key",
            "attempt",
        ),
        "ix_task_agent_reviews_status_created": ("status", "created_at"),
    }


def test_revision_56_upgrade_matches_runtime_metadata_and_empty_downgrade_roundtrip():
    runtime_engine = _engine()
    AGENT_REVIEWS.create(runtime_engine)
    migrated_engine = _engine()
    _migrate(migrated_engine, "upgrade")

    runtime_inspector = inspect(runtime_engine)
    migrated_inspector = inspect(migrated_engine)
    assert "task_agent_reviews" in migrated_inspector.get_table_names()
    assert {
        column["name"]
        for column in migrated_inspector.get_columns("task_agent_reviews")
    } == set(_COLUMNS)
    assert {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in migrated_inspector.get_columns("task_agent_reviews")
    } == {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in runtime_inspector.get_columns("task_agent_reviews")
    }
    assert {
        item["name"]
        for item in migrated_inspector.get_check_constraints("task_agent_reviews")
    } == _CHECK_NAMES
    assert {
        item["name"]
        for item in migrated_inspector.get_unique_constraints("task_agent_reviews")
        if item["name"]
    } == _UNIQUE_NAMES
    assert {
        item["name"] for item in migrated_inspector.get_indexes("task_agent_reviews")
    } == _INDEX_NAMES
    assert migrated_inspector.get_foreign_keys("task_agent_reviews") == []

    _migrate(migrated_engine, "downgrade")
    assert "task_agent_reviews" not in inspect(migrated_engine).get_table_names()
    _migrate(migrated_engine, "upgrade")
    assert "task_agent_reviews" in inspect(migrated_engine).get_table_names()
    runtime_engine.dispose()
    migrated_engine.dispose()


def test_valid_queued_and_terminal_rows_are_storable():
    engine = _engine()
    _migrate(engine, "upgrade")
    terminal_rows = (
        ("PASS", {"decision": "approve"}),
        ("FAIL", {"decision": "reject"}),
        ("UNAVAILABLE", None),
        ("STALE", None),
    )
    with engine.begin() as connection:
        connection.execute(AGENT_REVIEWS.insert().values(_base_row()))
        for index, (status, result_json) in enumerate(terminal_rows, start=1):
            criterion_id = f"criterion-{index}"
            connection.execute(
                AGENT_REVIEWS.insert().values(
                    _base_row(
                        f"review-{index + 1}",
                        verification_id=f"verification-{index + 1}",
                        source_run_id=f"source-run-{index + 1}",
                        run_id=f"review-run-{index + 1}",
                        budget_reservation_id=f"budget-{index + 1}",
                        criterion_id=criterion_id,
                        criterion_key=_criterion_key(criterion_id),
                        status=status,
                        result_json=result_json,
                        completed_at=_NOW,
                    )
                )
            )
    with engine.connect() as connection:
        assert connection.execute(
            select(AGENT_REVIEWS.c.status).order_by(AGENT_REVIEWS.c.review_id)
        ).scalars().all() == ["QUEUED", "PASS", "FAIL", "UNAVAILABLE", "STALE"]
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"criterion_id": ""},
        {"criterion_key": "a" * 63},
        {"criterion_key": "a" * 65},
        {"subject_digest": "a" * 63},
        {"subject_digest": "a" * 65},
        {"contract_version": 0},
        {"attempt": 0},
        {"status": "UNKNOWN"},
        {"status": "QUEUED", "completed_at": _NOW},
        {"status": "PASS", "completed_at": None, "result_json": {"ok": True}},
        {"status": "FAIL", "completed_at": _NOW, "result_json": None},
        {"status": "PASS", "completed_at": _NOW, "result_json": None},
        {"status": "UNAVAILABLE", "completed_at": None},
        {"status": "STALE", "completed_at": None},
    ],
)
def test_sqlite_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            AGENT_REVIEWS.insert().values(
                _base_row(review_id="invalid", **overrides)
            )
        )
    engine.dispose()


def test_run_budget_and_verification_criterion_attempt_are_unique():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(AGENT_REVIEWS.insert().values(_base_row()))

    duplicate_cases = (
        {"review_id": "review-2", "run_id": "review-run-1"},
        {"review_id": "review-3", "run_id": "review-run-3", "budget_reservation_id": "budget-1"},
        {
            "review_id": "review-4",
            "run_id": "review-run-4",
            "budget_reservation_id": "budget-4",
        },
    )
    for overrides in duplicate_cases:
        if overrides["review_id"] == "review-4":
            overrides.update(
                {
                    "verification_id": "verification-1",
                    "criterion_key": _criterion_key("security-review"),
                    "attempt": 1,
                }
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(AGENT_REVIEWS.insert().values(_base_row(**overrides)))
    engine.dispose()


def test_downgrade_refuses_any_existing_evidence_before_schema_mutation():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(AGENT_REVIEWS.insert().values(_base_row()))
    before_indexes = {
        item["name"] for item in inspect(engine).get_indexes("task_agent_reviews")
    }

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260830_56"):
        _migrate(engine, "downgrade")

    inspector = inspect(engine)
    assert "task_agent_reviews" in inspector.get_table_names()
    assert {
        item["name"] for item in inspector.get_indexes("task_agent_reviews")
    } == before_indexes
    with engine.connect() as connection:
        assert (
            connection.execute(select(AGENT_REVIEWS.c.review_id)).scalar_one()
            == "review-1"
        )
    engine.dispose()


def test_postgresql_create_table_ddl_has_constraints_and_no_cross_metadata_fks():
    ddl = str(CreateTable(AGENT_REVIEWS).compile(dialect=postgresql.dialect()))
    assert "CREATE TABLE task_agent_reviews" in ddl
    assert "review_id VARCHAR(128) NOT NULL" in ddl
    assert "criterion_id TEXT NOT NULL" in ddl
    assert "criterion_key VARCHAR(64) NOT NULL" in ddl
    assert "result_json JSON" in ddl
    assert "completed_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "CONSTRAINT ck_task_agent_reviews_completion CHECK" in ddl
    assert "CONSTRAINT ck_task_agent_reviews_result CHECK" in ddl
    assert "CONSTRAINT uq_task_agent_reviews_run_id UNIQUE" in ddl
    assert "CONSTRAINT uq_task_agent_reviews_budget_reservation_id UNIQUE" in ddl
    assert "FOREIGN KEY" not in ddl


def test_postgresql_upgrade_compiles_offline():
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
    assert "CREATE TABLE task_agent_reviews" in sql
    assert "CREATE INDEX ix_task_agent_reviews_verification_criterion_attempt" in sql
    assert "CREATE INDEX ix_task_agent_reviews_status_created" in sql
