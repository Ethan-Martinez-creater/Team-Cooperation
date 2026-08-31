from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    JSON,
    Boolean,
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
    DELIVERY_METADATA,
    INTEGRATION_RUNS,
    PROJECT_COMPLETION_CONTRACTS,
    PROJECT_COMPLETION_EVALUATIONS,
    PROJECT_DELIVERIES,
    PROJECT_DELIVERY_APPROVALS,
)

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260831_58_delivery_evidence.py"
)
_TABLES = (
    INTEGRATION_RUNS,
    PROJECT_DELIVERIES,
    PROJECT_COMPLETION_CONTRACTS,
    PROJECT_COMPLETION_EVALUATIONS,
)
_COLUMNS = {
    "integration_runs": (
        "integration_id",
        "project_id",
        "process_id",
        "version",
        "graph_digest",
        "subject_digest",
        "based_on_process_version",
        "based_on_event_sequence",
        "input_artifact_refs_json",
        "integration_policy_json",
        "result_artifact_refs_json",
        "verification_refs_json",
        "checks_json",
        "impacted_work_ids_json",
        "status",
        "initiated_by",
        "executed_as",
        "created_at",
        "updated_at",
        "completed_at",
    ),
    "project_deliveries": (
        "delivery_id",
        "project_id",
        "process_id",
        "integration_id",
        "version",
        "graph_digest",
        "artifact_refs_json",
        "artifact_version_manifest_json",
        "artifact_digest_manifest_json",
        "verification_refs_json",
        "acceptance_requirements_json",
        "release_notes_resource_id",
        "status",
        "created_by",
        "created_at",
        "updated_at",
        "approved_by",
        "accepted_at",
        "decision",
        "decision_key",
        "decision_digest",
        "decision_reason",
        "decided_by",
        "decided_at",
    ),
    "project_completion_contracts": (
        "contract_id",
        "project_id",
        "process_id",
        "version",
        "criteria_json",
        "required_human_approvers_json",
        "status",
        "content_sha256",
        "created_by",
        "created_at",
        "approved_by",
        "approved_at",
    ),
    "project_completion_evaluations": (
        "evaluation_id",
        "project_id",
        "process_id",
        "contract_id",
        "contract_version",
        "delivery_id",
        "delivery_version",
        "based_on_process_version",
        "based_on_event_sequence",
        "graph_digest",
        "subject_digest",
        "passed",
        "checks_json",
        "created_at",
    ),
}
_CHECK_NAMES = {
    "integration_runs": {
        "ck_integration_runs_version",
        "ck_integration_runs_graph_digest",
        "ck_integration_runs_subject_digest",
        "ck_integration_runs_based_on_process_version",
        "ck_integration_runs_based_on_event_sequence",
        "ck_integration_runs_status",
        "ck_integration_runs_completion",
    },
    "project_deliveries": {
        "ck_project_deliveries_version",
        "ck_project_deliveries_graph_digest",
        "ck_project_deliveries_status",
        "ck_project_deliveries_decision",
        "ck_project_deliveries_decision_digest",
        "ck_project_deliveries_decision_fields",
        "ck_project_deliveries_status_consistency",
    },
    "project_completion_contracts": {
        "ck_project_completion_contracts_version",
        "ck_project_completion_contracts_content_sha256",
        "ck_project_completion_contracts_status",
        "ck_project_completion_contracts_approval",
    },
    "project_completion_evaluations": {
        "ck_project_completion_evaluations_contract_version",
        "ck_project_completion_evaluations_delivery_version",
        "ck_project_completion_evaluations_based_on_process_version",
        "ck_project_completion_evaluations_based_on_event_sequence",
        "ck_project_completion_evaluations_graph_digest",
        "ck_project_completion_evaluations_subject_digest",
    },
}
_UNIQUE_NAMES = {
    "integration_runs": {"uq_integration_runs_process_subject"},
    "project_deliveries": {"uq_project_deliveries_integration"},
    "project_completion_contracts": {
        "uq_project_completion_contracts_process_version",
    },
    "project_completion_evaluations": {
        "uq_project_completion_evaluations_process_subject",
    },
}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "delivery_evidence_revision_58", _MIGRATION_PATH
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


def _base_integration(integration_id: str = "integration-1", **overrides):
    row = {
        "integration_id": integration_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "version": 1,
        "graph_digest": "a" * 64,
        "subject_digest": "b" * 64,
        "based_on_process_version": 1,
        "based_on_event_sequence": 0,
        "input_artifact_refs_json": [],
        "integration_policy_json": {},
        "result_artifact_refs_json": [],
        "verification_refs_json": [],
        "checks_json": {},
        "impacted_work_ids_json": [],
        "status": "PENDING",
        "initiated_by": "service:orchestrator",
        "executed_as": "service:integration",
        "created_at": _NOW,
        "updated_at": _NOW,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def _base_delivery(delivery_id: str = "delivery-1", **overrides):
    row = {
        "delivery_id": delivery_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "integration_id": "integration-1",
        "version": 1,
        "graph_digest": "a" * 64,
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
    row.update(overrides)
    return row


def _delivery_decision(status: str = "ACCEPTED", decision: str = "ACCEPT", **overrides):
    row = {
        "status": status,
        "approved_by": "human-approver-1" if decision == "ACCEPT" else None,
        "accepted_at": _NOW if decision == "ACCEPT" else None,
        "decision": decision,
        "decision_key": "delivery-decision-1",
        "decision_digest": "c" * 64,
        "decision_reason": "Reviewed against the delivery acceptance contract.",
        "decided_by": "human-reviewer-1",
        "decided_at": _NOW,
    }
    row.update(overrides)
    return row


def _base_contract(contract_id: str = "contract-1", **overrides):
    row = {
        "contract_id": contract_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "version": 1,
        "criteria_json": {},
        "required_human_approvers_json": [],
        "status": "DRAFT",
        "content_sha256": "d" * 64,
        "created_by": "service:orchestrator",
        "created_at": _NOW,
        "approved_by": None,
        "approved_at": None,
    }
    row.update(overrides)
    return row


def _base_evaluation(evaluation_id: str = "evaluation-1", **overrides):
    row = {
        "evaluation_id": evaluation_id,
        "project_id": "project-1",
        "process_id": "process-1",
        "contract_id": "contract-1",
        "contract_version": 1,
        "delivery_id": "delivery-1",
        "delivery_version": 1,
        "based_on_process_version": 1,
        "based_on_event_sequence": 0,
        "graph_digest": "a" * 64,
        "subject_digest": "b" * 64,
        "passed": True,
        "checks_json": {},
        "created_at": _NOW,
    }
    row.update(overrides)
    return row


def test_repository_metadata_matches_contract():
    assert DELIVERY_METADATA.tables == {
        "integration_runs": INTEGRATION_RUNS,
        "project_deliveries": PROJECT_DELIVERIES,
        "project_completion_contracts": PROJECT_COMPLETION_CONTRACTS,
        "project_completion_evaluations": PROJECT_COMPLETION_EVALUATIONS,
        "project_delivery_approvals": PROJECT_DELIVERY_APPROVALS,
    }
    assert not any(table.foreign_keys for table in _TABLES)

    for table in _TABLES:
        assert tuple(table.columns.keys()) == _COLUMNS[table.name]
        assert {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        } == _CHECK_NAMES[table.name]
        assert {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        } == _UNIQUE_NAMES[table.name]
        assert not table.indexes

    for name in (
        "integration_id",
        "project_id",
        "process_id",
        "contract_id",
        "delivery_id",
        "evaluation_id",
    ):
        table = next(table for table in _TABLES if name in table.c)
        assert table.c[name].type.length == 128
        assert table.c[name].nullable is False

    for table in (INTEGRATION_RUNS, PROJECT_DELIVERIES):
        assert table.c.version.type.__class__ is Integer
        assert table.c.version.nullable is False
        assert table.c.graph_digest.type.length == 64
        assert table.c.graph_digest.nullable is False

    assert isinstance(INTEGRATION_RUNS.c.input_artifact_refs_json.type, JSON)
    assert INTEGRATION_RUNS.c.input_artifact_refs_json.type.none_as_null is True
    assert isinstance(PROJECT_DELIVERIES.c.decision_reason.type, Text)
    assert PROJECT_DELIVERIES.c.decision_reason.nullable is True
    assert PROJECT_DELIVERIES.c.decision_digest.type.length == 64
    assert PROJECT_DELIVERIES.c.approved_by.type.length == 256
    assert PROJECT_DELIVERIES.c.decided_by.type.length == 256
    assert PROJECT_COMPLETION_CONTRACTS.c.content_sha256.type.length == 64
    assert PROJECT_COMPLETION_EVALUATIONS.c.passed.nullable is False
    assert isinstance(PROJECT_COMPLETION_EVALUATIONS.c.passed.type, Boolean)
    for table in _TABLES:
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True


def test_revision_58_upgrade_matches_runtime_metadata_and_empty_downgrade_roundtrip():
    runtime_engine = _engine()
    DELIVERY_METADATA.create_all(runtime_engine)
    migrated_engine = _engine()
    _migrate(migrated_engine, "upgrade")

    runtime_inspector = inspect(runtime_engine)
    migrated_inspector = inspect(migrated_engine)
    for table in _TABLES:
        name = table.name
        assert name in migrated_inspector.get_table_names()
        assert {
            column["name"]
            for column in migrated_inspector.get_columns(name)
        } == set(_COLUMNS[name])
        assert {
            column["name"]: (column["nullable"], str(column["type"]))
            for column in migrated_inspector.get_columns(name)
        } == {
            column["name"]: (column["nullable"], str(column["type"]))
            for column in runtime_inspector.get_columns(name)
        }
        assert {
            item["name"] for item in migrated_inspector.get_check_constraints(name)
        } == _CHECK_NAMES[name]
        assert {
            item["name"]
            for item in migrated_inspector.get_unique_constraints(name)
            if item["name"]
        } == _UNIQUE_NAMES[name]
        assert migrated_inspector.get_indexes(name) == []
        assert migrated_inspector.get_foreign_keys(name) == []

    _migrate(migrated_engine, "downgrade")
    assert set(inspect(migrated_engine).get_table_names()) == set()
    _migrate(migrated_engine, "upgrade")
    assert set(inspect(migrated_engine).get_table_names()) == set(_COLUMNS)
    runtime_engine.dispose()
    migrated_engine.dispose()


def test_valid_integration_delivery_contract_and_evaluation_rows_are_storable():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            INTEGRATION_RUNS.insert().values(
                _base_integration(
                    status="PASS",
                    completed_at=_NOW,
                )
            )
        )
        connection.execute(
            PROJECT_DELIVERIES.insert().values(
                _base_delivery(status="READY", integration_id="integration-1")
            )
        )
        connection.execute(
            PROJECT_DELIVERIES.insert().values(
                _base_delivery(
                    "delivery-2",
                    integration_id="integration-2",
                    **_delivery_decision(),
                )
            )
        )
        connection.execute(
            PROJECT_DELIVERIES.insert().values(
                _base_delivery(
                    "delivery-3",
                    integration_id="integration-3",
                    **_delivery_decision(status="REJECTED", decision="REJECT"),
                )
            )
        )
        connection.execute(
            PROJECT_COMPLETION_CONTRACTS.insert().values(_base_contract())
        )
        connection.execute(
            PROJECT_COMPLETION_CONTRACTS.insert().values(
                _base_contract(
                    "contract-2",
                    version=2,
                    status="APPROVED",
                    approved_by="human-approver-1",
                    approved_at=_NOW,
                )
            )
        )
        connection.execute(
            PROJECT_COMPLETION_EVALUATIONS.insert().values(_base_evaluation())
        )
    with engine.connect() as connection:
        assert connection.execute(
            select(INTEGRATION_RUNS.c.status)
        ).scalar_one() == "PASS"
        assert connection.execute(
            select(PROJECT_DELIVERIES.c.status).order_by(PROJECT_DELIVERIES.c.delivery_id)
        ).scalars().all() == ["READY", "ACCEPTED", "REJECTED"]
        assert connection.execute(
            select(PROJECT_COMPLETION_CONTRACTS.c.status).order_by(
                PROJECT_COMPLETION_CONTRACTS.c.contract_id
            )
        ).scalars().all() == ["DRAFT", "APPROVED"]
        assert connection.execute(
            select(PROJECT_COMPLETION_EVALUATIONS.c.passed)
        ).scalar_one() is True
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"graph_digest": "a" * 63},
        {"graph_digest": "a" * 65},
        {"subject_digest": "b" * 63},
        {"subject_digest": "b" * 65},
        {"based_on_process_version": 0},
        {"based_on_event_sequence": -1},
        {"status": "UNKNOWN"},
        {"status": "PENDING", "completed_at": _NOW},
        {"status": "PASS"},
    ],
)
def test_integration_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            INTEGRATION_RUNS.insert().values(
                _base_integration(integration_id="invalid", **overrides)
            )
        )
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"graph_digest": "a" * 63},
        {"graph_digest": "a" * 65},
        {"status": "UNKNOWN"},
        {"decision": "OTHER"},
        {"decision": "ACCEPT"},
        {"decision_digest": "c" * 63},
        {"decision_digest": "c" * 65},
        {**_delivery_decision(), "status": "READY"},
        {"status": "ACCEPTED", "approved_by": None, "accepted_at": None},
        {"status": "ACCEPTED", **_delivery_decision(decision="REJECT")},
        {**_delivery_decision(), "status": "REJECTED"},
        {
            "status": "REJECTED",
            **_delivery_decision(status="REJECTED", decision="REJECT"),
            "approved_by": "human-approver-1",
        },
        {"status": "ASSEMBLING", "decided_by": "human-reviewer-1"},
    ],
)
def test_delivery_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            PROJECT_DELIVERIES.insert().values(
                _base_delivery(delivery_id="invalid", **overrides)
            )
        )
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"content_sha256": "d" * 63},
        {"content_sha256": "d" * 65},
        {"status": "UNKNOWN"},
        {"status": "DRAFT", "approved_by": "human-approver-1"},
        {"status": "APPROVED", "approved_at": _NOW},
        {"status": "APPROVED", "approved_by": "human-approver-1"},
    ],
)
def test_completion_contract_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            PROJECT_COMPLETION_CONTRACTS.insert().values(
                _base_contract(contract_id="invalid", **overrides)
            )
        )
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract_version": 0},
        {"delivery_version": 0},
        {"based_on_process_version": 0},
        {"based_on_event_sequence": -1},
        {"graph_digest": "a" * 63},
        {"graph_digest": "a" * 65},
        {"subject_digest": "b" * 63},
        {"subject_digest": "b" * 65},
        {"passed": None},
    ],
)
def test_completion_evaluation_constraints_reject_invalid_rows(overrides):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            PROJECT_COMPLETION_EVALUATIONS.insert().values(
                _base_evaluation(evaluation_id="invalid", **overrides)
            )
        )
    engine.dispose()


def test_all_declared_uniqueness_constraints_are_enforced():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(INTEGRATION_RUNS.insert().values(_base_integration()))
        connection.execute(
            PROJECT_DELIVERIES.insert().values(_base_delivery(integration_id="integration-1"))
        )
        connection.execute(PROJECT_COMPLETION_CONTRACTS.insert().values(_base_contract()))
        connection.execute(PROJECT_COMPLETION_EVALUATIONS.insert().values(_base_evaluation()))

    duplicate_rows = (
        (INTEGRATION_RUNS, _base_integration("integration-2")),
        (PROJECT_DELIVERIES, _base_delivery("delivery-2")),
        (PROJECT_COMPLETION_CONTRACTS, _base_contract("contract-2")),
        (PROJECT_COMPLETION_EVALUATIONS, _base_evaluation("evaluation-2")),
    )
    for table, row in duplicate_rows:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(table.insert().values(row))
    engine.dispose()


@pytest.mark.parametrize(
    ("table", "row"),
    [
        (INTEGRATION_RUNS, _base_integration()),
        (PROJECT_DELIVERIES, _base_delivery()),
        (PROJECT_COMPLETION_CONTRACTS, _base_contract()),
        (PROJECT_COMPLETION_EVALUATIONS, _base_evaluation()),
    ],
)
def test_downgrade_refuses_any_existing_evidence_before_schema_mutation(table, row):
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(table.insert().values(row))

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260831_58"):
        _migrate(engine, "downgrade")

    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(_COLUMNS)
    with engine.connect() as connection:
        first_column = next(iter(table.c))
        assert connection.execute(select(first_column)).first() is not None
    engine.dispose()


def test_postgresql_create_table_ddl_has_expected_columns_and_no_cross_metadata_fks():
    expected_snippets = {
        "integration_runs": (
            "CREATE TABLE integration_runs",
            "integration_id VARCHAR(128) NOT NULL",
            "input_artifact_refs_json JSON NOT NULL",
            "completed_at TIMESTAMP WITH TIME ZONE",
        ),
        "project_deliveries": (
            "CREATE TABLE project_deliveries",
            "decision_reason TEXT",
            "decision_digest VARCHAR(64)",
            "accepted_at TIMESTAMP WITH TIME ZONE",
        ),
        "project_completion_contracts": (
            "CREATE TABLE project_completion_contracts",
            "criteria_json JSON NOT NULL",
            "content_sha256 VARCHAR(64) NOT NULL",
        ),
        "project_completion_evaluations": (
            "CREATE TABLE project_completion_evaluations",
            "passed BOOLEAN NOT NULL",
            "checks_json JSON NOT NULL",
        ),
    }
    for table in _TABLES:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert all(snippet in ddl for snippet in expected_snippets[table.name])
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
    for table in _COLUMNS:
        assert f"CREATE TABLE {table}" in sql
