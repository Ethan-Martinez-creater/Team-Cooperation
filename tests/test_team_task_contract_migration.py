from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
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

from coifesp_harness.control_plane.bootstrap import SCHEMA_REVISION
from coifesp_harness.product.repository import PRODUCT_METADATA, TEAM_TASKS

_NOW = datetime(2026, 8, 30, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "20260830_53_team_task_contracts.py"
)
_CONTRACT_COLUMNS = (
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
)
_REQUIRED_CONTRACT_FIELDS = (
    "process_id",
    "work_node_id",
    "requested_capability",
    "input_manifest_json",
    "output_contract_json",
    "verification_policy_json",
    "autonomy_requirement",
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
    "created_at",
    "updated_at",
)


def _migration():
    spec = spec_from_file_location("team_task_contracts_53", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "20260830_53"
    assert migration.down_revision == "20260830_52"
    return migration


def _migrate(engine, direction):
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
    return metadata


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
            metadata.tables["product_accounts"].insert(), {"account_id": "account-a"}
        )
        connection.execute(
            metadata.tables["product_projects"].insert(), {"project_id": "project-a"}
        )
        connection.execute(
            metadata.tables["product_team_tasks"].insert(), _base_task("task-legacy")
        )
    return engine


def _base_task(task_id):
    return {
        "task_id": task_id,
        "project_id": "project-a",
        "source_team_id": "team-b",
        "target_team_id": "team-a",
        "created_by": "account-a",
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
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _contract_values():
    return {
        "process_id": "process-a",
        "work_node_id": "node-a",
        "requested_capability": {"name": "backend-api"},
        "input_manifest_json": {"items": []},
        "output_contract_json": {"media_type": "application/json"},
        "verification_policy_json": {"checks": ["unit"]},
        "source_decision_id": None,
        "source_contract_version": 1,
        "autonomy_requirement": "bounded",
        "accepted_contract_version": None,
    }


def _raw_row(engine, task_id, selected_columns=_LEGACY_COLUMNS):
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


@pytest.fixture
def upgraded_engine():
    engine = _engine()
    _migrate(engine, "upgrade")
    yield engine
    engine.dispose()


def test_revision_and_upgrade_preserve_legacy_task_and_add_nullable_contract_fields():
    engine = _engine()
    before = _raw_row(engine, "task-legacy")

    _migrate(engine, "upgrade")

    columns = {item["name"]: item for item in inspect(engine).get_columns("product_team_tasks")}
    assert ScriptDirectory(str(Path(__file__).parents[1] / "alembic")).get_heads() == [SCHEMA_REVISION]
    assert set(_CONTRACT_COLUMNS).issubset(columns)
    assert all(columns[name]["nullable"] for name in _CONTRACT_COLUMNS)
    assert _raw_row(engine, "task-legacy") == before
    assert set(_CONTRACT_COLUMNS).isdisjoint(
        {
            fk_column
            for foreign_key in inspect(engine).get_foreign_keys("product_team_tasks")
            for fk_column in foreign_key["constrained_columns"]
        }
    )
    assert "ck_product_team_tasks_contract" in {
        item["name"] for item in inspect(engine).get_check_constraints("product_team_tasks")
    }
    engine.dispose()


def test_valid_contract_is_storable_with_optional_acceptance_version(upgraded_engine):
    values = _base_task("task-contract") | _contract_values()
    with upgraded_engine.begin() as connection:
        connection.execute(TEAM_TASKS.insert().values(values))
        connection.execute(
            TEAM_TASKS.insert().values(
                _base_task("task-accepted-contract")
                | (_contract_values() | {"accepted_contract_version": 1})
            )
        )

    with upgraded_engine.connect() as connection:
        rows = connection.execute(
            select(
                TEAM_TASKS.c.source_contract_version,
                TEAM_TASKS.c.accepted_contract_version,
            ).order_by(TEAM_TASKS.c.task_id)
        ).all()
    assert (1, None) in rows
    assert (1, 1) in rows


def test_contract_check_rejects_partial_fields_and_invalid_versions(upgraded_engine):
    valid = _contract_values()
    cases = [{"source_contract_version": 1}]
    cases.extend(
        {field: valid[field]}
        for field in _CONTRACT_COLUMNS
        if field != "source_contract_version" and valid[field] is not None
    )
    cases.extend(
        valid | {field: None}
        for field in _REQUIRED_CONTRACT_FIELDS
    )
    cases.extend(
        [
            valid | {"source_contract_version": 0},
            valid | {"source_contract_version": -1},
            valid | {"accepted_contract_version": 0},
            valid | {"accepted_contract_version": 2},
        ]
    )

    for index, contract in enumerate(cases):
        with pytest.raises(IntegrityError), upgraded_engine.begin() as connection:
            connection.execute(
                TEAM_TASKS.insert().values(_base_task(f"task-invalid-{index}") | contract)
            )


def test_downgrade_refuses_structured_contract_data_before_schema_mutation(upgraded_engine):
    with upgraded_engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.insert().values(_base_task("task-contract") | _contract_values())
        )
    before = _raw_row(upgraded_engine, "task-contract")
    columns_before = [
        item | {"type": str(item["type"])}
        for item in inspect(upgraded_engine).get_columns("product_team_tasks")
    ]

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260830_53.*structured TeamTask"):
        _migrate(upgraded_engine, "downgrade")

    columns_after = [
        item | {"type": str(item["type"])}
        for item in inspect(upgraded_engine).get_columns("product_team_tasks")
    ]
    assert columns_after == columns_before
    assert _raw_row(upgraded_engine, "task-contract") == before


def test_legacy_rows_roundtrip_upgrade_downgrade_and_reupgrade_without_contract_data():
    engine = _engine()
    before = _raw_row(engine, "task-legacy")

    _migrate(engine, "upgrade")
    upgraded = _raw_row(engine, "task-legacy", _LEGACY_COLUMNS + _CONTRACT_COLUMNS)
    assert all(upgraded[name] is None for name in _CONTRACT_COLUMNS)

    _migrate(engine, "downgrade")
    assert set(_CONTRACT_COLUMNS).isdisjoint(
        {item["name"] for item in inspect(engine).get_columns("product_team_tasks")}
    )
    assert _raw_row(engine, "task-legacy") == before

    _migrate(engine, "upgrade")
    assert all(
        _raw_row(engine, "task-legacy", _LEGACY_COLUMNS + _CONTRACT_COLUMNS)[name] is None
        for name in _CONTRACT_COLUMNS
    )
    engine.dispose()


def test_sqlite_native_migration_preserves_inbound_graph_indexes_and_triggers():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE task_child (id INTEGER PRIMARY KEY, task_id VARCHAR(128) NOT NULL "
            "REFERENCES product_team_tasks(task_id))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE task_grandchild (id INTEGER PRIMARY KEY, child_id INTEGER "
            "NOT NULL REFERENCES task_child(id))"
        )
        connection.exec_driver_sql("CREATE INDEX ix_task_child_task ON task_child(task_id)")
        connection.exec_driver_sql("CREATE TABLE task_touch_audit (task_id VARCHAR(128))")
        connection.exec_driver_sql(
            "CREATE TRIGGER task_title_touched AFTER UPDATE OF title ON product_team_tasks "
            "BEGIN INSERT INTO task_touch_audit VALUES (NEW.task_id); END"
        )
        connection.exec_driver_sql("INSERT INTO task_child VALUES (1, 'task-legacy')")
        connection.exec_driver_sql("INSERT INTO task_grandchild VALUES (1, 1)")

    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    for direction in ("upgrade", "downgrade", "upgrade"):
        _migrate(engine, direction)
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("SELECT count(*) FROM task_grandchild").scalar_one() == 1
            connection.exec_driver_sql("UPDATE product_team_tasks SET title='touched' WHERE task_id='task-legacy'")
        assert inspect(engine).get_foreign_keys("task_child")[0]["referred_table"] == "product_team_tasks"
        assert "ix_task_child_task" in {item["name"] for item in inspect(engine).get_indexes("task_child")}
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO task_child VALUES (2, 'missing-task')")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM task_touch_audit").scalar_one() == 3
    assert not any("DROP TABLE" in item or "RENAME TO" in item for item in statements)
    assert not any("FOREIGN_KEYS=OFF" in item.replace(" ", "") for item in statements)
    engine.dispose()


def test_fresh_metadata_contract_check_allows_native_downgrade():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    PRODUCT_METADATA.create_all(engine)
    _migrate(engine, "downgrade")
    assert not set(_CONTRACT_COLUMNS) & {
        column["name"] for column in inspect(engine).get_columns(TEAM_TASKS.name)
    }
    _migrate(engine, "upgrade")
    assert set(_CONTRACT_COLUMNS) <= {
        column["name"] for column in inspect(engine).get_columns(TEAM_TASKS.name)
    }
    engine.dispose()


def test_postgresql_team_task_ddl_has_contract_check_without_cross_metadata_foreign_keys():
    ddl = str(CreateTable(TEAM_TASKS).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT ck_product_team_tasks_contract CHECK" in ddl
    assert "source_contract_version IS NULL" in ddl
    assert "source_contract_version >= 1" in ddl
    assert "FOREIGN KEY (process_id)" not in ddl
    assert "FOREIGN KEY (work_node_id)" not in ddl
    assert {foreign_key.column.table.metadata for foreign_key in TEAM_TASKS.foreign_keys} == {
        PRODUCT_METADATA
    }


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
    ddl = output.getvalue()
    assert "ALTER TABLE product_team_tasks ADD COLUMN process_id VARCHAR(128)" in ddl
    assert "ADD CONSTRAINT ck_product_team_tasks_contract CHECK" in ddl
