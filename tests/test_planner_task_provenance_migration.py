from __future__ import annotations

from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from coifesp_harness.product.repository import PRODUCT_METADATA, TEAM_TASKS

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260831_61_planner_task_provenance.py"
)
_LEGACY_COLUMNS = (
    "task_id",
    "project_id",
    "source_team_id",
    "target_team_id",
    "created_by",
    "title",
    "description",
    "acceptance_criteria",
    "status",
    "assigned_account_id",
    "artifact_resource_ids",
    "review_note",
    "priority",
    "due_at",
    "schedule_version",
    "due_changed_at",
    "due_changed_by",
    "completed_at",
    "process_id",
    "work_node_id",
    "requested_capability",
    "input_manifest_json",
    "output_contract_json",
    "verification_policy_json",
    "source_decision_id",
    "source_contract_version",
    "autonomy_requirement",
    "accepted_contract_version",
    "created_at",
    "updated_at",
)
_PROVENANCE_COLUMNS = (
    "produced_by_principal_id",
    "source_planner_run_id",
    "source_planner_command_id",
)
_CONTRACT_CHECK = (
    "(source_contract_version IS NULL AND "
    "process_id IS NULL AND work_node_id IS NULL AND "
    "requested_capability IS NULL AND input_manifest_json IS NULL AND "
    "output_contract_json IS NULL AND verification_policy_json IS NULL AND "
    "source_decision_id IS NULL AND autonomy_requirement IS NULL AND "
    "accepted_contract_version IS NULL) OR "
    "(source_contract_version IS NOT NULL AND source_contract_version >= 1 AND "
    "process_id IS NOT NULL AND work_node_id IS NOT NULL AND "
    "requested_capability IS NOT NULL AND input_manifest_json IS NOT NULL AND "
    "output_contract_json IS NOT NULL AND verification_policy_json IS NOT NULL AND "
    "autonomy_requirement IS NOT NULL AND "
    "(accepted_contract_version IS NULL OR "
    "(accepted_contract_version = source_contract_version AND "
    "accepted_contract_version >= 1)))"
)
_CREATOR_CHECK_NAME = "ck_product_team_tasks_creator"
_SOURCE_COMMAND_INDEX = "uq_product_team_tasks_source_planner_command_id"


def _migration():
    spec = spec_from_file_location("planner_task_provenance_61", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _migrate(engine, direction: str) -> None:
    with engine.begin() as connection:
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, direction)()


def _legacy_metadata():
    metadata = MetaData()
    Table("product_teams", metadata, Column("team_id", String(128), primary_key=True))
    Table("product_accounts", metadata, Column("account_id", String(128), primary_key=True))
    Table("product_projects", metadata, Column("project_id", String(128), primary_key=True))
    Table(
        "product_team_tasks",
        metadata,
        Column("task_id", String(128), primary_key=True),
        Column("project_id", String(128), ForeignKey("product_projects.project_id"), nullable=False),
        Column("source_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
        Column("target_team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
        Column("created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False),
        Column("title", String(256), nullable=False),
        Column("description", Text, nullable=False),
        Column("acceptance_criteria", Text, nullable=False),
        Column("status", String(32), nullable=False),
        Column("assigned_account_id", String(128), ForeignKey("product_accounts.account_id")),
        Column("artifact_resource_ids", Text, nullable=False),
        Column("review_note", Text, nullable=False),
        Column("priority", String(16), nullable=False, server_default="normal"),
        Column("due_at", DateTime(timezone=True), nullable=True),
        Column("schedule_version", Integer, nullable=False, server_default="1"),
        Column("due_changed_at", DateTime(timezone=True), nullable=True),
        Column("due_changed_by", String(128), ForeignKey("product_accounts.account_id")),
        Column("completed_at", DateTime(timezone=True), nullable=True),
        Column("process_id", String(128), nullable=True),
        Column("work_node_id", String(128), nullable=True),
        Column("requested_capability", JSON(none_as_null=True), nullable=True),
        Column("input_manifest_json", JSON(none_as_null=True), nullable=True),
        Column("output_contract_json", JSON(none_as_null=True), nullable=True),
        Column("verification_policy_json", JSON(none_as_null=True), nullable=True),
        Column("source_decision_id", String(128), nullable=True),
        Column("source_contract_version", Integer, nullable=True),
        Column("autonomy_requirement", String(32), nullable=True),
        Column(
            "accepted_contract_version",
            Integer,
            CheckConstraint(_CONTRACT_CHECK, name="ck_product_team_tasks_contract"),
            nullable=True,
        ),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("source_team_id <> target_team_id", name="different_task_teams"),
        CheckConstraint(
            "status IN ('proposed','accepted','in_progress','submitted','verified',"
            "'changes_requested','rejected')",
            name="status",
        ),
        CheckConstraint("priority IN ('low','normal','high','urgent')", name="priority"),
        CheckConstraint("schedule_version >= 1", name="positive_schedule_version"),
    )
    Index(
        "ix_product_tasks_project_status",
        metadata.tables["product_team_tasks"].c.project_id,
        metadata.tables["product_team_tasks"].c.status,
    )
    Index(
        "ix_product_tasks_team_status_due",
        metadata.tables["product_team_tasks"].c.target_team_id,
        metadata.tables["product_team_tasks"].c.status,
        metadata.tables["product_team_tasks"].c.due_at,
    )
    Index(
        "ix_product_tasks_project_priority_due",
        metadata.tables["product_team_tasks"].c.project_id,
        metadata.tables["product_team_tasks"].c.priority,
        metadata.tables["product_team_tasks"].c.due_at,
    )
    return metadata


def _base_task(task_id: str = "task-legacy", **overrides):
    row = {
        "task_id": task_id,
        "project_id": "project-a",
        "source_team_id": "team-b",
        "target_team_id": "team-a",
        "created_by": "account-human",
        "title": "Legacy task",
        "description": "Existing task remains unchanged",
        "acceptance_criteria": "Migration preserves it",
        "status": "accepted",
        "assigned_account_id": None,
        "artifact_resource_ids": "[]",
        "review_note": "",
        "priority": "normal",
        "due_at": None,
        "schedule_version": 1,
        "due_changed_at": None,
        "due_changed_by": None,
        "completed_at": None,
        "process_id": None,
        "work_node_id": None,
        "requested_capability": None,
        "input_manifest_json": None,
        "output_contract_json": None,
        "verification_policy_json": None,
        "source_decision_id": None,
        "source_contract_version": None,
        "autonomy_requirement": None,
        "accepted_contract_version": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    row.update(overrides)
    return row


def _contract_values():
    return {
        "process_id": "process-a",
        "work_node_id": "node-a",
        "requested_capability": {"name": "backend-api"},
        "input_manifest_json": {"items": []},
        "output_contract_json": {"media_type": "application/json"},
        "verification_policy_json": {"checks": ["unit"]},
        "source_decision_id": "decision-a",
        "source_contract_version": 1,
        "autonomy_requirement": "bounded",
        "accepted_contract_version": None,
    }


def _machine_task(task_id: str = "task-machine", **overrides):
    row = _base_task(task_id) | _contract_values()
    row.update(
        {
            "created_by": None,
            "status": "proposed",
            "produced_by_principal_id": "service:project-orchestrator",
            "source_planner_run_id": "planner-run-a",
            "source_planner_command_id": "planner-command-a",
        }
    )
    row.update(overrides)
    return row


def _raw_row(engine, task_id: str, selected_columns=_LEGACY_COLUMNS):
    columns = ", ".join(selected_columns)
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    f"SELECT {columns} FROM product_team_tasks "
                    "WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            ).mappings().one()
        )


def _engine():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    metadata = _legacy_metadata()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["product_teams"].insert(),
            [{"team_id": "team-a"}, {"team_id": "team-b"}],
        )
        connection.execute(
            metadata.tables["product_accounts"].insert(),
            [{"account_id": "account-human"}, {"account_id": "account-planner"}],
        )
        connection.execute(
            metadata.tables["product_projects"].insert(),
            {"project_id": "project-a"},
        )
        connection.execute(
            metadata.tables["product_team_tasks"].insert(),
            _base_task(),
        )
    return engine


@pytest.fixture
def upgraded_engine():
    engine = _engine()
    _migrate(engine, "upgrade")
    yield engine
    engine.dispose()


def test_repository_metadata_has_nullable_fk_and_provenance_contract():
    assert TEAM_TASKS.c.created_by.nullable is True
    assert {
        foreign_key.column.table.metadata
        for foreign_key in TEAM_TASKS.c.created_by.foreign_keys
    } == {PRODUCT_METADATA}
    assert TEAM_TASKS.c.source_planner_command_id.unique is True
    for name, length in (
        ("produced_by_principal_id", 256),
        ("source_planner_run_id", 128),
        ("source_planner_command_id", 128),
    ):
        assert TEAM_TASKS.c[name].type.length == length
        assert TEAM_TASKS.c[name].nullable is True
        assert not TEAM_TASKS.c[name].foreign_keys
    check_names = {
        constraint.name
        for constraint in TEAM_TASKS.constraints
        if isinstance(constraint, CheckConstraint)
    }
    check_names.update(
        constraint.name
        for constraint in TEAM_TASKS.c.source_planner_command_id.constraints
        if isinstance(constraint, CheckConstraint)
    )
    check_names.update(
        constraint.name
        for constraint in TEAM_TASKS.c.accepted_contract_version.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert _CREATOR_CHECK_NAME in check_names
    assert "ck_product_team_tasks_contract" in check_names


def test_revision_and_upgrade_preserve_legacy_row_and_multiple_inbound_fks():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE task_child_a (id INTEGER PRIMARY KEY, task_id VARCHAR(128) "
            "NOT NULL REFERENCES product_team_tasks(task_id))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE task_child_b (id INTEGER PRIMARY KEY, task_id VARCHAR(128) "
            "NOT NULL REFERENCES product_team_tasks(task_id))"
        )
        connection.exec_driver_sql("INSERT INTO task_child_a VALUES (1, 'task-legacy')")
        connection.exec_driver_sql("INSERT INTO task_child_b VALUES (1, 'task-legacy')")
    before = _raw_row(engine, "task-legacy")
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    _migrate(engine, "upgrade")

    columns = {item["name"]: item for item in inspect(engine).get_columns(TEAM_TASKS.name)}
    assert columns["created_by"]["nullable"] is True
    assert all(columns[name]["nullable"] for name in _PROVENANCE_COLUMNS)
    assert _raw_row(engine, "task-legacy") == before
    upgraded = _raw_row(engine, "task-legacy", _LEGACY_COLUMNS + _PROVENANCE_COLUMNS)
    assert all(upgraded[name] is None for name in _PROVENANCE_COLUMNS)
    assert engine.connect().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    assert engine.connect().exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert inspect(engine).get_foreign_keys("task_child_a")[0]["referred_table"] == TEAM_TASKS.name
    assert inspect(engine).get_foreign_keys("task_child_b")[0]["referred_table"] == TEAM_TASKS.name
    assert "created_by" in {
        column
        for foreign_key in inspect(engine).get_foreign_keys(TEAM_TASKS.name)
        for column in foreign_key["constrained_columns"]
    }
    assert _SOURCE_COMMAND_INDEX in {
        item["name"] for item in inspect(engine).get_indexes(TEAM_TASKS.name)
    }
    assert _CREATOR_CHECK_NAME in {
        item["name"] for item in inspect(engine).get_check_constraints(TEAM_TASKS.name)
    }
    assert not any("DROP TABLE" in statement or "RENAME TO" in statement for statement in statements)
    engine.dispose()


def test_machine_and_human_task_provenance_are_storable_with_unaccepted_contract():
    engine = _engine()
    try:
        _migrate(engine, "upgrade")
        with engine.begin() as connection:
            connection.execute(
                TEAM_TASKS.insert().values(_base_task("task-human-new"))
            )
            connection.execute(
                TEAM_TASKS.insert().values(_machine_task())
            )

        with engine.connect() as connection:
            machine = connection.execute(
                select(
                    TEAM_TASKS.c.created_by,
                    TEAM_TASKS.c.status,
                    TEAM_TASKS.c.produced_by_principal_id,
                    TEAM_TASKS.c.source_planner_run_id,
                    TEAM_TASKS.c.source_planner_command_id,
                    TEAM_TASKS.c.accepted_contract_version,
                ).where(TEAM_TASKS.c.task_id == "task-machine")
            ).one()
        assert machine == (
            None,
            "proposed",
            "service:project-orchestrator",
            "planner-run-a",
            "planner-command-a",
            None,
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"created_by": "account-human"},
        {"produced_by_principal_id": "agent:planner"},
        {"source_planner_run_id": ""},
        {"source_planner_run_id": None},
        {"source_planner_command_id": ""},
        {"source_planner_command_id": None},
        {"process_id": ""},
        {"process_id": None},
        {"source_decision_id": ""},
        {"source_decision_id": None},
    ],
)
def test_creator_check_rejects_impersonation_or_incomplete_machine_provenance(
    upgraded_engine, overrides
):
    row = _machine_task("task-invalid")
    row.update(overrides)
    with pytest.raises(IntegrityError), upgraded_engine.begin() as connection:
        connection.execute(TEAM_TASKS.insert().values(row))


def test_existing_contract_check_remains_enforced_for_machine_tasks(upgraded_engine):
    row = _machine_task("task-invalid-contract", output_contract_json=None)
    with pytest.raises(IntegrityError), upgraded_engine.begin() as connection:
        connection.execute(TEAM_TASKS.insert().values(row))


def test_planner_command_is_unique_while_human_nulls_are_repeatable(upgraded_engine):
    with upgraded_engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.insert().values(_machine_task("task-machine-1"))
        )
    with pytest.raises(IntegrityError), upgraded_engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.insert().values(
                _machine_task(
                    "task-machine-2",
                    source_planner_run_id="planner-run-b",
                )
            )
        )

    with upgraded_engine.begin() as connection:
        connection.execute(TEAM_TASKS.insert().values(_base_task("task-human-1")))
        connection.execute(TEAM_TASKS.insert().values(_base_task("task-human-2")))


def test_downgrade_refuses_machine_provenance_before_schema_mutation():
    engine = _engine()
    _migrate(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(TEAM_TASKS.insert().values(_machine_task()))
    before_columns = [
        item | {"type": str(item["type"])}
        for item in inspect(engine).get_columns(TEAM_TASKS.name)
    ]
    before = _raw_row(engine, "task-machine", _LEGACY_COLUMNS + _PROVENANCE_COLUMNS)

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260831_61"):
        _migrate(engine, "downgrade")

    after_columns = [
        item | {"type": str(item["type"])}
        for item in inspect(engine).get_columns(TEAM_TASKS.name)
    ]
    assert after_columns == before_columns
    assert _raw_row(engine, "task-machine", _LEGACY_COLUMNS + _PROVENANCE_COLUMNS) == before
    assert engine.connect().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    engine.dispose()


def test_empty_machine_provenance_downgrade_preserves_old_rows_and_inbound_fks():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE task_child_a (id INTEGER PRIMARY KEY, task_id VARCHAR(128) "
            "NOT NULL REFERENCES product_team_tasks(task_id))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE task_child_b (id INTEGER PRIMARY KEY, task_id VARCHAR(128) "
            "NOT NULL REFERENCES product_team_tasks(task_id))"
        )
        connection.exec_driver_sql("INSERT INTO task_child_a VALUES (1, 'task-legacy')")
        connection.exec_driver_sql("INSERT INTO task_child_b VALUES (1, 'task-legacy')")
    before = _raw_row(engine, "task-legacy")

    _migrate(engine, "upgrade")
    _migrate(engine, "downgrade")

    columns = {item["name"]: item for item in inspect(engine).get_columns(TEAM_TASKS.name)}
    assert not set(_PROVENANCE_COLUMNS) & set(columns)
    assert columns["created_by"]["nullable"] is False
    assert _raw_row(engine, "task-legacy") == before
    assert inspect(engine).get_foreign_keys("task_child_a")[0]["referred_table"] == TEAM_TASKS.name
    assert inspect(engine).get_foreign_keys("task_child_b")[0]["referred_table"] == TEAM_TASKS.name
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("SELECT count(*) FROM task_child_a").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM task_child_b").scalar_one() == 1

    _migrate(engine, "upgrade")
    assert inspect(engine).get_columns(TEAM_TASKS.name)[4]["nullable"] is True
    engine.dispose()


def test_postgresql_team_task_ddl_has_creator_check_and_retains_account_fk():
    ddl = str(CreateTable(TEAM_TASKS).compile(dialect=postgresql.dialect()))
    assert "created_by VARCHAR(128)" in ddl
    assert "FOREIGN KEY(created_by) REFERENCES product_accounts (account_id)" in ddl
    assert "CONSTRAINT ck_product_team_tasks_creator CHECK" in ddl
    assert "produced_by_principal_id = 'service:project-orchestrator'" in ddl
    assert "UNIQUE (source_planner_command_id)" in ddl


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
    assert "ALTER TABLE product_team_tasks ALTER COLUMN created_by DROP NOT NULL" in sql
    assert "ALTER TABLE product_team_tasks ADD COLUMN produced_by_principal_id VARCHAR(256)" in sql
    assert "ALTER TABLE product_team_tasks ADD COLUMN source_planner_run_id VARCHAR(128)" in sql
    assert "ALTER TABLE product_team_tasks ADD COLUMN source_planner_command_id VARCHAR(128)" in sql
    assert f"CREATE UNIQUE INDEX {_SOURCE_COMMAND_INDEX}" in sql
    assert f"ADD CONSTRAINT {_CREATOR_CHECK_NAME} CHECK" in sql
    assert "DROP TABLE" not in sql
    assert migration.revision == "20260831_61"
    assert migration.down_revision == "20260831_60"
    assert "20260831_61" in ScriptDirectory(
        str(Path(__file__).parents[1] / "alembic")
    ).get_heads()
