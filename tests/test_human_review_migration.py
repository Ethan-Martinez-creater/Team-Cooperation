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

from coifesp_harness.verification.repository import HUMAN_REVIEWS

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260831_57_human_reviews.py"
)
_COLUMNS = (
    "review_id",
    "verification_id",
    "project_id",
    "process_id",
    "task_id",
    "source_run_id",
    "reviewer_team_id",
    "criterion_id",
    "criterion_key",
    "subject_digest",
    "contract_version",
    "status",
    "version",
    "created_at",
    "updated_at",
    "completed_at",
    "decision",
    "decision_key",
    "decision_digest",
    "decided_by",
    "reason",
    "decided_at",
)
_CHECK_NAMES = {
    "ck_task_human_reviews_criterion_id",
    "ck_task_human_reviews_criterion_key",
    "ck_task_human_reviews_subject_digest",
    "ck_task_human_reviews_contract_version",
    "ck_task_human_reviews_status",
    "ck_task_human_reviews_version",
    "ck_task_human_reviews_completion",
    "ck_task_human_reviews_decision",
    "ck_task_human_reviews_decision_fields",
    "ck_task_human_reviews_decision_digest",
    "ck_task_human_reviews_open_decision",
    "ck_task_human_reviews_accepted_decision",
    "ck_task_human_reviews_rejected_decision",
}
_UNIQUE_NAMES = {"uq_task_human_reviews_verification_criterion"}
_INDEX_NAMES = {
    "ix_task_human_reviews_reviewer_status_created",
    "ix_task_human_reviews_verification_id",
}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "human_reviews_revision_57", _MIGRATION_PATH
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


def _base_row(review_id: str = "human-review-1", **overrides):
    criterion_id = "security-review"
    row = {
        "review_id": review_id,
        "verification_id": "verification-1",
        "project_id": "project-1",
        "process_id": "process-1",
        "task_id": "task-1",
        "source_run_id": "source-run-1",
        "reviewer_team_id": "team-source",
        "criterion_id": criterion_id,
        "criterion_key": _criterion_key(criterion_id),
        "subject_digest": "a" * 64,
        "contract_version": 1,
        "status": "OPEN",
        "version": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
        "completed_at": None,
        "decision": None,
        "decision_key": None,
        "decision_digest": None,
        "decided_by": None,
        "reason": None,
        "decided_at": None,
    }
    row.update(overrides)
    return row


def _decision(status: str = "ACCEPTED", decision: str = "ACCEPT", **overrides):
    row = {
        "status": status,
        "completed_at": _NOW,
        "decision": decision,
        "decision_key": "decision-key-1",
        "decision_digest": "b" * 64,
        "decided_by": "human-source-1",
        "reason": "Reviewed against the submitted evidence.",
        "decided_at": _NOW,
    }
    row.update(overrides)
    return row


def test_repository_metadata_matches_contract():
    assert HUMAN_REVIEWS.metadata.tables["task_human_reviews"] is HUMAN_REVIEWS
    assert tuple(HUMAN_REVIEWS.columns.keys()) == _COLUMNS
    assert not HUMAN_REVIEWS.foreign_keys
    assert {
        constraint.name
        for constraint in HUMAN_REVIEWS.constraints
        if isinstance(constraint, CheckConstraint)
    } == _CHECK_NAMES
    assert {
        constraint.name
        for constraint in HUMAN_REVIEWS.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    } == _UNIQUE_NAMES

    for name in (
        "review_id",
        "verification_id",
        "project_id",
        "process_id",
        "task_id",
        "source_run_id",
        "reviewer_team_id",
    ):
        assert HUMAN_REVIEWS.c[name].type.length == 128
        assert HUMAN_REVIEWS.c[name].nullable is False
    assert isinstance(HUMAN_REVIEWS.c.criterion_id.type, Text)
    assert HUMAN_REVIEWS.c.criterion_id.nullable is False
    assert HUMAN_REVIEWS.c.criterion_key.type.length == 64
    assert HUMAN_REVIEWS.c.subject_digest.type.length == 64
    assert HUMAN_REVIEWS.c.status.type.length == 16
    assert HUMAN_REVIEWS.c.version.nullable is False
    assert isinstance(HUMAN_REVIEWS.c.version.type, Integer)
    assert isinstance(HUMAN_REVIEWS.c.contract_version.type, Integer)
    assert HUMAN_REVIEWS.c.contract_version.nullable is False
    assert HUMAN_REVIEWS.c.decision.type.length == 16
    assert HUMAN_REVIEWS.c.decision_key.type.length == 128
    assert HUMAN_REVIEWS.c.decision_digest.type.length == 64
    assert HUMAN_REVIEWS.c.decided_by.type.length == 256
    assert isinstance(HUMAN_REVIEWS.c.reason.type, Text)
    for name in ("created_at", "updated_at", "completed_at", "decided_at"):
        assert isinstance(HUMAN_REVIEWS.c[name].type, DateTime)
        assert HUMAN_REVIEWS.c[name].type.timezone is True
    for name in (
        "completed_at",
        "decision",
        "decision_key",
        "decision_digest",
        "decided_by",
        "reason",
        "decided_at",
    ):
        assert HUMAN_REVIEWS.c[name].nullable is True

    assert {index.name for index in HUMAN_REVIEWS.indexes} == _INDEX_NAMES
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in HUMAN_REVIEWS.indexes
    } == {
        "ix_task_human_reviews_reviewer_status_created": (
            "reviewer_team_id",
            "status",
            "created_at",
        ),
        "ix_task_human_reviews_verification_id": ("verification_id",),
    }


def test_revision_57_upgrade_matches_runtime_metadata_and_empty_downgrade_roundtrip():
    runtime_engine = _engine()
    HUMAN_REVIEWS.create(runtime_engine)
    migrated_engine = _engine()
    _migrate(migrated_engine, "upgrade")

    runtime_inspector = inspect(runtime_engine)
    migrated_inspector = inspect(migrated_engine)
    assert "task_human_reviews" in migrated_inspector.get_table_names()
    assert {
        column["name"]
        for column in migrated_inspector.get_columns("task_human_reviews")
    } == set(_COLUMNS)
    assert {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in migrated_inspector.get_columns("task_human_reviews")
    } == {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in runtime_inspector.get_columns("task_human_reviews")
    }
    assert {
        item["name"]
        for item in migrated_inspector.get_check_constraints("task_human_reviews")
    } == _CHECK_NAMES
    assert {
        item["name"]
        for item in migrated_inspector.get_unique_constraints("task_human_reviews")
        if item["name"]
    } == _UNIQUE_NAMES
    assert {
        item["name"] for item in migrated_inspector.get_indexes("task_human_reviews")
    } == _INDEX_NAMES
    assert migrated_inspector.get_foreign_keys("task_human_reviews") == []

    _migrate(migrated_engine, "downgrade")
    assert "task_human_reviews" not in inspect(migrated_engine).get_table_names()
    _migrate(migrated_engine, "upgrade")
    assert "task_human_reviews" in inspect(migrated_engine).get_table_names()
    runtime_engine.dispose()
    migrated_engine.dispose()


def test_valid_open_decisions_and_stale_history_are_storable():
    engine = _engine()
    _migrate(engine, "upgrade")
    rows = [
        _base_row(),
        _base_row(
            "human-review-2",
            verification_id="verification-2",
            criterion_id="criterion-2",
            criterion_key=_criterion_key("criterion-2"),
            **_decision(),
        ),
        _base_row(
            "human-review-3",
            verification_id="verification-3",
            criterion_id="criterion-3",
            criterion_key=_criterion_key("criterion-3"),
            **_decision(status="REJECTED", decision="REJECT"),
        ),
        _base_row(
            "human-review-4",
            verification_id="verification-4",
            criterion_id="criterion-4",
            criterion_key=_criterion_key("criterion-4"),
            status="STALE",
            completed_at=_NOW,
        ),
        _base_row(
            "human-review-5",
            verification_id="verification-5",
            criterion_id="criterion-5",
            criterion_key=_criterion_key("criterion-5"),
            **_decision(status="STALE"),
        ),
    ]
    with engine.begin() as connection:
        connection.execute(HUMAN_REVIEWS.insert(), rows)
    with engine.connect() as connection:
        assert connection.execute(
            select(HUMAN_REVIEWS.c.status).order_by(HUMAN_REVIEWS.c.review_id)
        ).scalars().all() == ["OPEN", "ACCEPTED", "REJECTED", "STALE", "STALE"]
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
        {"status": "UNKNOWN"},
        {"version": 0},
        {"status": "OPEN", "completed_at": _NOW},
        dict(_decision(status="ACCEPTED"), completed_at=None),
        dict(_decision(status="REJECTED", decision="REJECT"), completed_at=None),
        {"status": "STALE"},
        {"status": "STALE", "completed_at": _NOW, "decision": "ACCEPT"},
        {"decision": "OTHER"},
        {"decision_digest": "b" * 63},
        {"decision_digest": "b" * 65},
        {"decision": "ACCEPT", "decision_key": "decision-key-1"},
        {"decided_by": "human-source-1"},
        {"status": "ACCEPTED", **_decision(status="ACCEPTED", decision="REJECT")},
        {"status": "REJECTED", **_decision(status="REJECTED", decision="ACCEPT")},
        {"status": "OPEN", **_decision(status="OPEN")},
    ],
)
def test_sqlite_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            HUMAN_REVIEWS.insert().values(_base_row(review_id="invalid", **overrides))
        )
    engine.dispose()


def test_verification_and_criterion_are_unique():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(HUMAN_REVIEWS.insert().values(_base_row()))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            HUMAN_REVIEWS.insert().values(
                _base_row("human-review-2", verification_id="verification-1")
            )
        )
    engine.dispose()


def test_downgrade_refuses_any_existing_evidence_before_schema_mutation():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(HUMAN_REVIEWS.insert().values(_base_row()))
    before_indexes = {
        item["name"] for item in inspect(engine).get_indexes("task_human_reviews")
    }

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260831_57"):
        _migrate(engine, "downgrade")

    inspector = inspect(engine)
    assert "task_human_reviews" in inspector.get_table_names()
    assert {
        item["name"] for item in inspector.get_indexes("task_human_reviews")
    } == before_indexes
    with engine.connect() as connection:
        assert (
            connection.execute(select(HUMAN_REVIEWS.c.review_id)).scalar_one()
            == "human-review-1"
        )
    engine.dispose()


def test_postgresql_create_table_ddl_has_constraints_and_no_cross_metadata_fks():
    ddl = str(CreateTable(HUMAN_REVIEWS).compile(dialect=postgresql.dialect()))
    assert "CREATE TABLE task_human_reviews" in ddl
    assert "review_id VARCHAR(128) NOT NULL" in ddl
    assert "criterion_id TEXT NOT NULL" in ddl
    assert "criterion_key VARCHAR(64) NOT NULL" in ddl
    assert "decision_digest VARCHAR(64)" in ddl
    assert "reason TEXT" in ddl
    assert "decided_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "CONSTRAINT ck_task_human_reviews_completion CHECK" in ddl
    assert "CONSTRAINT ck_task_human_reviews_decision_fields CHECK" in ddl
    assert "CONSTRAINT uq_task_human_reviews_verification_criterion UNIQUE" in ddl
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
    assert "CREATE TABLE task_human_reviews" in sql
    assert "CREATE INDEX ix_task_human_reviews_reviewer_status_created" in sql
    assert "CREATE INDEX ix_task_human_reviews_verification_id" in sql
