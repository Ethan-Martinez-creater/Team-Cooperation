from __future__ import annotations

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

from coifesp_harness.delivery.repository import (
    PROJECT_DELIVERIES,
    PROJECT_DELIVERY_APPROVALS,
)

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260831_60_delivery_approvals.py"
)
_COLUMNS = (
    "approval_id",
    "project_id",
    "process_id",
    "delivery_id",
    "contract_id",
    "contract_version",
    "actor_id",
    "decision",
    "decision_key",
    "decision_digest",
    "reason",
    "expected_delivery_version",
    "expected_process_version",
    "created_at",
)
_CHECK_NAMES = {
    "ck_project_delivery_approvals_approval_id",
    "ck_project_delivery_approvals_project_id",
    "ck_project_delivery_approvals_process_id",
    "ck_project_delivery_approvals_delivery_id",
    "ck_project_delivery_approvals_contract_id",
    "ck_project_delivery_approvals_contract_version",
    "ck_project_delivery_approvals_actor_id",
    "ck_project_delivery_approvals_decision",
    "ck_project_delivery_approvals_decision_key",
    "ck_project_delivery_approvals_decision_digest",
    "ck_project_delivery_approvals_reason",
    "ck_project_delivery_approvals_expected_delivery_version",
    "ck_project_delivery_approvals_expected_process_version",
}
_UNIQUE_NAMES = {"uq_project_delivery_approvals_delivery_actor"}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "delivery_approvals_revision_60", _MIGRATION_PATH
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


def _base_approval(approval_id: str = "approval-1", **overrides):
    row = {
        "approval_id": approval_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "delivery_id": "delivery-1",
        "contract_id": "contract-1",
        "contract_version": 1,
        "actor_id": "human-1",
        "decision": "ACCEPT",
        "decision_key": "decision-key-1",
        "decision_digest": "a" * 64,
        "reason": "Accepted against the completion contract.",
        "expected_delivery_version": 1,
        "expected_process_version": 1,
        "created_at": _NOW,
    }
    row.update(overrides)
    return row


def _base_delivery(delivery_id: str = "delivery-1"):
    return {
        "delivery_id": delivery_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "integration_id": "integration-1",
        "version": 1,
        "graph_digest": "b" * 64,
        "artifact_refs_json": [],
        "artifact_version_manifest_json": {},
        "artifact_digest_manifest_json": {},
        "verification_refs_json": [],
        "acceptance_requirements_json": {},
        "release_notes_resource_id": None,
        "status": "ASSEMBLING",
        "created_by": "service:orchestrator",
        "created_at": _NOW,
        "updated_at": _NOW,
        "approved_by": None,
        "accepted_at": None,
        "decision": None,
        "decision_key": None,
        "decision_digest": None,
        "decision_reason": None,
        "decided_by": None,
        "decided_at": None,
    }


def test_repository_metadata_matches_contract():
    assert PROJECT_DELIVERY_APPROVALS.metadata.tables["project_delivery_approvals"] is (
        PROJECT_DELIVERY_APPROVALS
    )
    assert tuple(PROJECT_DELIVERY_APPROVALS.columns.keys()) == _COLUMNS
    assert not PROJECT_DELIVERY_APPROVALS.foreign_keys
    assert {
        constraint.name
        for constraint in PROJECT_DELIVERY_APPROVALS.constraints
        if isinstance(constraint, CheckConstraint)
    } == _CHECK_NAMES
    assert {
        constraint.name
        for constraint in PROJECT_DELIVERY_APPROVALS.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    } == _UNIQUE_NAMES

    for name in (
        "approval_id",
        "project_id",
        "process_id",
        "delivery_id",
        "contract_id",
        "actor_id",
    ):
        assert PROJECT_DELIVERY_APPROVALS.c[name].type.length == 128
        assert PROJECT_DELIVERY_APPROVALS.c[name].nullable is False
    assert isinstance(PROJECT_DELIVERY_APPROVALS.c.contract_version.type, Integer)
    assert PROJECT_DELIVERY_APPROVALS.c.contract_version.nullable is False
    assert PROJECT_DELIVERY_APPROVALS.c.decision.type.length == 16
    assert PROJECT_DELIVERY_APPROVALS.c.decision.nullable is False
    assert PROJECT_DELIVERY_APPROVALS.c.decision_key.type.length == 128
    assert PROJECT_DELIVERY_APPROVALS.c.decision_key.nullable is False
    assert PROJECT_DELIVERY_APPROVALS.c.decision_digest.type.length == 64
    assert PROJECT_DELIVERY_APPROVALS.c.decision_digest.nullable is False
    assert isinstance(PROJECT_DELIVERY_APPROVALS.c.reason.type, Text)
    assert PROJECT_DELIVERY_APPROVALS.c.reason.nullable is False
    for name in ("expected_delivery_version", "expected_process_version"):
        assert isinstance(PROJECT_DELIVERY_APPROVALS.c[name].type, Integer)
        assert PROJECT_DELIVERY_APPROVALS.c[name].nullable is False
    assert isinstance(PROJECT_DELIVERY_APPROVALS.c.created_at.type, DateTime)
    assert PROJECT_DELIVERY_APPROVALS.c.created_at.type.timezone is True
    assert PROJECT_DELIVERY_APPROVALS.c.created_at.nullable is False


def test_revision_60_upgrade_matches_runtime_metadata_and_preserves_old_rows():
    runtime_engine = _engine()
    PROJECT_DELIVERY_APPROVALS.create(runtime_engine)
    migrated_engine = _engine()
    PROJECT_DELIVERIES.create(migrated_engine)
    with migrated_engine.begin() as connection:
        connection.execute(PROJECT_DELIVERIES.insert().values(_base_delivery()))

    _migrate(migrated_engine, "upgrade")

    runtime_inspector = inspect(runtime_engine)
    migrated_inspector = inspect(migrated_engine)
    assert "project_delivery_approvals" in migrated_inspector.get_table_names()
    assert {
        column["name"]
        for column in migrated_inspector.get_columns("project_delivery_approvals")
    } == set(_COLUMNS)
    assert {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in migrated_inspector.get_columns("project_delivery_approvals")
    } == {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in runtime_inspector.get_columns("project_delivery_approvals")
    }
    assert {
        item["name"]
        for item in migrated_inspector.get_check_constraints("project_delivery_approvals")
    } == _CHECK_NAMES
    assert {
        item["name"]
        for item in migrated_inspector.get_unique_constraints("project_delivery_approvals")
        if item["name"]
    } == _UNIQUE_NAMES
    assert migrated_inspector.get_foreign_keys("project_delivery_approvals") == []
    with migrated_engine.connect() as connection:
        assert connection.execute(
            select(PROJECT_DELIVERIES.c.delivery_id)
        ).scalar_one() == "delivery-1"

    runtime_engine.dispose()
    migrated_engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"approval_id": ""},
        {"project_id": ""},
        {"process_id": ""},
        {"delivery_id": ""},
        {"contract_id": ""},
        {"contract_version": 0},
        {"actor_id": ""},
        {"decision": "OTHER"},
        {"decision_key": ""},
        {"decision_digest": "a" * 63},
        {"decision_digest": "a" * 65},
        {"reason": ""},
        {"expected_delivery_version": 0},
        {"expected_process_version": 0},
    ],
)
def test_sqlite_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    row = _base_approval(approval_id="invalid")
    row.update(overrides)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(PROJECT_DELIVERY_APPROVALS.insert().values(row))
    engine.dispose()


def test_delivery_and_actor_are_unique_but_multiple_actors_can_approve():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(PROJECT_DELIVERY_APPROVALS.insert().values(_base_approval()))
        connection.execute(
            PROJECT_DELIVERY_APPROVALS.insert().values(
                _base_approval(
                    approval_id="approval-2",
                    actor_id="human-2",
                    decision="REJECT",
                )
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            PROJECT_DELIVERY_APPROVALS.insert().values(
                _base_approval("approval-3", decision_key="decision-key-3")
            )
        )
    with engine.connect() as connection:
        assert connection.execute(
            select(PROJECT_DELIVERY_APPROVALS.c.actor_id).order_by(
                PROJECT_DELIVERY_APPROVALS.c.approval_id
            )
        ).scalars().all() == ["human-1", "human-2"]
    engine.dispose()


def test_empty_downgrade_roundtrips_and_data_downgrade_preserves_evidence():
    engine = _engine()
    _migrate(engine, "upgrade")
    _migrate(engine, "downgrade")
    assert "project_delivery_approvals" not in inspect(engine).get_table_names()

    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(PROJECT_DELIVERY_APPROVALS.insert().values(_base_approval()))
    with pytest.raises(RuntimeError, match="Cannot downgrade 20260831_60"):
        _migrate(engine, "downgrade")
    assert "project_delivery_approvals" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(
            select(PROJECT_DELIVERY_APPROVALS.c.approval_id)
        ).scalar_one() == "approval-1"
    engine.dispose()


def test_postgresql_create_table_ddl_has_constraints_and_no_cross_metadata_fks():
    ddl = str(
        CreateTable(PROJECT_DELIVERY_APPROVALS).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "CREATE TABLE project_delivery_approvals" in ddl
    assert "approval_id VARCHAR(128) NOT NULL" in ddl
    assert "decision VARCHAR(16) NOT NULL" in ddl
    assert "decision_digest VARCHAR(64) NOT NULL" in ddl
    assert "reason TEXT NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "CONSTRAINT ck_project_delivery_approvals_decision CHECK" in ddl
    assert "CONSTRAINT uq_project_delivery_approvals_delivery_actor UNIQUE" in ddl
    assert "FOREIGN KEY" not in ddl


def test_postgresql_upgrade_compiles_offline_and_revision_has_expected_head():
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
    assert "CREATE TABLE project_delivery_approvals" in sql
    assert "FOREIGN KEY" not in sql
    assert migration.revision == "20260831_60"
    assert migration.down_revision == "20260831_59"
