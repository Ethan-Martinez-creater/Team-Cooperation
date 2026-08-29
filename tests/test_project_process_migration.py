from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from coifesp_harness.control_plane.bootstrap import SCHEMA_REVISION
from coifesp_harness.project_process.repository import PROJECT_PROCESS_METADATA

MIGRATION = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260829_47_project_process_runtime.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("project_process_revision_47", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_47_upgrade_matches_runtime_tables_and_downgrades_cleanly():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    migration = _module()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    inspector = inspect(engine)
    expected_tables = set(PROJECT_PROCESS_METADATA.tables) - {
        "project_orchestration_decisions"
    }
    assert expected_tables.issubset(inspector.get_table_names())
    for name in sorted(expected_tables):
        table = PROJECT_PROCESS_METADATA.tables[name]
        migrated_columns = {column["name"] for column in inspector.get_columns(name)}
        runtime_columns = set(table.columns.keys())
        if name == "project_process_commands":
            runtime_columns.remove("request_json")
        assert migrated_columns == runtime_columns, name
        runtime_checks = {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint" and constraint.name
        }
        migrated_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints(name)
        }
        assert migrated_checks == runtime_checks, name
        runtime_uniques = {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint" and constraint.name
        }
        migrated_uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(name)
            if constraint["name"]
        }
        assert runtime_uniques.issubset(migrated_uniques), name
        assert {index.name for index in table.indexes} == {
            index["name"] for index in inspector.get_indexes(name)
        }, name

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
    assert not expected_tables.intersection(inspect(engine).get_table_names())


def test_bootstrap_revision_includes_project_orchestration_decision_head():
    migration = _module()
    assert migration.revision == "20260829_47"
    assert migration.down_revision == "20260829_46"
    assert SCHEMA_REVISION == "20260829_49"
