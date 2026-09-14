from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from coifesp_harness.product.repository import (
    ACCOUNTS,
    PRODUCT_METADATA,
    PROJECT_AGENT_RUNS,
    PROJECT_TEAMS,
    PROJECTS,
    TEAM_AGENT_PROFILES,
    TEAM_PROJECT_AGENTS,
    TEAM_TASKS,
    TEAMS,
)

_NOW = datetime(2026, 8, 30, tzinfo=UTC)
_RESULT_COLUMNS = (
    "task_contract_version",
    "task_result_status",
    "task_result_json",
    "task_result_at",
)


def _migration():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260830_54_team_task_result_receipts.py"
    )
    spec = spec_from_file_location("team_task_result_receipts_54", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "20260830_54"
    assert migration.down_revision == "20260830_53"
    return migration


def _migrate(engine, direction):
    with engine.begin() as connection:
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, direction)()


def _legacy_metadata():
    """Revision-53 product metadata, with result columns intentionally absent."""
    metadata = MetaData()
    for table in PRODUCT_METADATA.tables.values():
        if table is not PROJECT_AGENT_RUNS:
            table.to_metadata(metadata)
    runs = Table(
        PROJECT_AGENT_RUNS.name,
        metadata,
        Column(
            "project_id", String(128), ForeignKey("product_projects.project_id"), primary_key=True
        ),
        Column("run_id", String(128), primary_key=True),
        Column("team_id", String(128), ForeignKey("product_teams.team_id"), nullable=False),
        Column("created_by", String(128), ForeignKey("product_accounts.account_id")),
        Column("mode", String(32)),
        Column("conversation_id", String(128)),
        Column("turn_id", String(128)),
        Column("process_id", String(128)),
        Column(
            "team_agent_id",
            String(128),
            ForeignKey("product_team_project_agents.agent_id", name="fk_project_run_team_agent"),
        ),
        Column("work_node_id", String(128)),
        Column(
            "team_task_id",
            String(128),
            ForeignKey("product_team_tasks.task_id", name="fk_project_run_team_task"),
        ),
        Column("parent_run_id", String(128)),
        Column("orchestration_decision_id", String(128)),
        Column("run_kind", String(32), nullable=False, server_default="conversation"),
        Column("initiated_by_principal_id", String(256)),
        Column("executed_as_principal_id", String(256)),
        Column("delegation_scope_digest", String(64)),
        Column("execution_attempt", Integer),
        Column("capacity_reservation_id", String(128)),
        Column("project_budget_reservation_id", String(128)),
        Column("created_at", DateTime(timezone=True), nullable=False),
        CheckConstraint(
            "mode IN ('analysis','collaboration_actions','delivery_review')", name="mode"
        ),
        CheckConstraint(
            "run_kind IN ('conversation','planning','task_execution','verification',"
            "'replanning','exchange_draft','specialist')",
            name="ck_product_project_agent_runs_kind",
        ),
        CheckConstraint(
            "run_kind = 'task_execution' OR (created_by IS NOT NULL AND mode IS NOT NULL)",
            name="ck_product_project_agent_runs_legacy_identity",
        ),
        CheckConstraint(
            "run_kind <> 'task_execution' OR ("
            "process_id IS NOT NULL AND team_agent_id IS NOT NULL AND "
            "work_node_id IS NOT NULL AND team_task_id IS NOT NULL AND "
            "orchestration_decision_id IS NOT NULL AND "
            "initiated_by_principal_id IS NOT NULL AND "
            "executed_as_principal_id IS NOT NULL AND "
            "delegation_scope_digest IS NOT NULL AND "
            "execution_attempt IS NOT NULL AND "
            "capacity_reservation_id IS NOT NULL AND "
            "project_budget_reservation_id IS NOT NULL AND "
            "created_by IS NULL AND mode IS NULL AND "
            "execution_attempt >= 1 AND length(delegation_scope_digest) = 64 AND "
            "initiated_by_principal_id = 'service:project-orchestrator' AND "
            "executed_as_principal_id = 'team-agent:' || team_id)",
            name="ck_product_project_agent_runs_task_execution",
        ),
        UniqueConstraint("run_id", name="uq_product_project_agent_run"),
        UniqueConstraint(
            "process_id",
            "team_task_id",
            "execution_attempt",
            name="uq_product_project_agent_run_task_attempt",
        ),
    )
    Index("ix_product_project_agent_runs_conversation", runs.c.conversation_id, runs.c.turn_id)
    return metadata


def _engine():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    _legacy_metadata().create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            TEAMS.insert(),
            [
                {"team_id": team, "handle": team, "handle_key": team, "name": team, "created_at": _NOW}
                for team in ("team-a", "team-b")
            ],
        )
        connection.execute(
            ACCOUNTS.insert().values(
                account_id="account-a",
                username="account-a",
                username_key="account-a",
                display_name="Account A",
                email="a@example.invalid",
                email_key="a@example.invalid",
                password_hash="unused",
                team_id="team-a",
                team_role="admin",
                registration_status="active",
                must_change_password=False,
                enabled=True,
                created_at=_NOW,
            )
        )
        connection.execute(
            PROJECTS.insert().values(
                project_id="project-a",
                name="Project A",
                description="test",
                owner_team_id="team-a",
                created_by="account-a",
                created_at=_NOW,
            )
        )
        connection.execute(
            PROJECT_TEAMS.insert().values(
                project_id="project-a",
                team_id="team-a",
                name="Owner",
                kind="product",
                assigned_by="account-a",
                created_at=_NOW,
            )
        )
        connection.execute(
            TEAM_AGENT_PROFILES.insert().values(
                profile_id="profile-a",
                version=1,
                team_id="team-a",
                display_name="Team A Agent",
                tool_policy_id="default",
                skill_policy_id="default",
                model_policy_id="default",
                memory_policy_id="default",
                autonomy_level="bounded",
                max_run_budget_profile={},
                created_at=_NOW,
            )
        )
        connection.execute(
            TEAM_PROJECT_AGENTS.insert().values(
                agent_id="agent-a",
                project_id="project-a",
                team_id="team-a",
                status="active",
                memory_version=1,
                profile_id="profile-a",
                profile_version=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        connection.execute(
            TEAM_TASKS.insert().values(
                task_id="task-a",
                project_id="project-a",
                source_team_id="team-b",
                target_team_id="team-a",
                created_by="account-a",
                title="Task A",
                description="test",
                acceptance_criteria="tests pass",
                status="accepted",
                artifact_resource_ids="[]",
                review_note="",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        connection.execute(PROJECT_AGENT_RUNS.insert().values(**_human_run()))
    return engine


def _human_run(run_id="run-human", **changes):
    values = {
        "project_id": "project-a",
        "run_id": run_id,
        "team_id": "team-a",
        "created_by": "account-a",
        "mode": "analysis",
        "conversation_id": "conversation-a",
        "turn_id": "turn-a",
        "run_kind": "conversation",
        "created_at": _NOW,
    }
    values.update(changes)
    return values


def _automatic_run(run_id="run-automatic", **changes):
    values = {
        "project_id": "project-a",
        "run_id": run_id,
        "team_id": "team-a",
        "created_by": None,
        "mode": None,
        "process_id": "process-a",
        "team_agent_id": "agent-a",
        "work_node_id": "node-a",
        "team_task_id": "task-a",
        "parent_run_id": None,
        "orchestration_decision_id": "decision-a",
        "run_kind": "task_execution",
        "initiated_by_principal_id": "service:project-orchestrator",
        "executed_as_principal_id": "team-agent:team-a",
        "delegation_scope_digest": "d" * 64,
        "execution_attempt": 1,
        "capacity_reservation_id": "capacity-a",
        "project_budget_reservation_id": "budget-a",
        "created_at": _NOW,
    }
    values.update(changes)
    return values


def _insert(engine, values):
    with engine.begin() as connection:
        connection.execute(PROJECT_AGENT_RUNS.insert().values(**values))


def _row(engine, run_id="run-human"):
    table = Table(PROJECT_AGENT_RUNS.name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return dict(
            connection.execute(select(table).where(table.c.run_id == run_id)).mappings().one()
        )


def test_revision_upgrade_preserves_legacy_human_and_adds_nullable_result_columns():
    engine = _engine()
    before = _row(engine)

    _migrate(engine, "upgrade")

    columns = inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)
    assert [column["name"] for column in columns][-4:] == list(_RESULT_COLUMNS)
    assert all(column["nullable"] for column in columns[-4:])
    assert _row(engine) | {name: None for name in _RESULT_COLUMNS} == before | {
        name: None for name in _RESULT_COLUMNS
    }
    assert "ck_product_project_agent_runs_task_result" in {
        item["name"] for item in inspect(engine).get_check_constraints(PROJECT_AGENT_RUNS.name)
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_valid_complete_partial_and_missing_version_receipts_are_storable():
    engine = _engine()
    _migrate(engine, "upgrade")

    _insert(engine, _automatic_run(run_id="run-no-receipt", execution_attempt=1))
    _insert(
        engine,
        _automatic_run(run_id="run-version-only", execution_attempt=2, task_contract_version=1),
    )
    _insert(
        engine,
        _automatic_run(
            run_id="run-invalid-output",
            execution_attempt=3,
            task_result_status="invalid_output",
            task_result_json={"error": "schema"},
            task_result_at=_NOW,
        ),
    )
    _insert(
        engine,
        _automatic_run(
            run_id="run-submitted",
            execution_attempt=4,
            task_contract_version=1,
            task_result_status="submitted",
            task_result_json={"artifact_id": "artifact-a"},
            task_result_at=_NOW,
        ),
    )
    _insert(
        engine,
        _automatic_run(
            run_id="run-failed",
            execution_attempt=5,
            task_contract_version=1,
            task_result_status="failed",
            task_result_json={"error": "worker"},
            task_result_at=_NOW,
        ),
    )
    _insert(
        engine,
        _automatic_run(
            run_id="run-cancelled",
            execution_attempt=6,
            task_contract_version=1,
            task_result_status="cancelled",
            task_result_json={"reason": "timeout"},
            task_result_at=_NOW,
        ),
    )

    assert _row(engine, "run-invalid-output")["task_contract_version"] is None
    assert _row(engine, "run-submitted")["task_result_json"] == {"artifact_id": "artifact-a"}
    engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"task_contract_version": 0},
        {"task_contract_version": -1},
        {"task_result_status": "unknown", "task_result_json": {}, "task_result_at": _NOW},
        {"task_result_status": "submitted", "task_result_json": None, "task_result_at": _NOW},
        {"task_result_status": "submitted", "task_result_json": {}, "task_result_at": None},
        {"task_result_status": None, "task_result_json": {}, "task_result_at": None},
        {"task_result_status": None, "task_result_json": None, "task_result_at": _NOW},
        {
            "task_contract_version": 1,
            "task_result_status": "submitted",
            "task_result_json": {},
            "task_result_at": _NOW,
            "run_kind": "conversation",
        },
    ],
)
def test_result_check_rejects_invalid_version_status_pairings(changes):
    engine = _engine()
    _migrate(engine, "upgrade")
    base = _automatic_run(run_id="run-invalid")
    if changes.get("run_kind") == "conversation":
        base = _human_run(run_id="run-invalid")
    with pytest.raises(IntegrityError):
        _insert(engine, base | changes)
    engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"task_contract_version": 1},
        {"task_result_status": "submitted", "task_result_json": {}, "task_result_at": _NOW},
    ],
)
def test_downgrade_rejects_any_non_null_result_or_version_data(changes):
    engine = _engine()
    _migrate(engine, "upgrade")
    _insert(engine, _automatic_run(run_id="run-receipt", execution_attempt=2, **changes))
    columns_before = [item | {"type": str(item["type"])} for item in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)]

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260830_54"):
        _migrate(engine, "downgrade")

    columns_after = [item | {"type": str(item["type"])} for item in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)]
    assert columns_after == columns_before
    assert _row(engine, "run-receipt")[_RESULT_COLUMNS[0]] == changes.get(_RESULT_COLUMNS[0])
    engine.dispose()


def test_legacy_rows_roundtrip_upgrade_downgrade_and_reupgrade():
    engine = _engine()
    before = _row(engine)
    _migrate(engine, "upgrade")
    upgraded = _row(engine)
    assert all(upgraded[name] is None for name in _RESULT_COLUMNS)

    _migrate(engine, "downgrade")
    assert not set(_RESULT_COLUMNS) & {
        item["name"] for item in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)
    }
    assert _row(engine) == before

    _migrate(engine, "upgrade")
    assert all(_row(engine)[name] is None for name in _RESULT_COLUMNS)
    engine.dispose()


def test_sqlite_native_migration_preserves_inbound_graph_indexes_and_triggers():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE task_child (id INTEGER PRIMARY KEY, run_id VARCHAR(128) NOT NULL "
            "REFERENCES product_project_agent_runs(run_id))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE task_grandchild (id INTEGER PRIMARY KEY, child_id INTEGER "
            "NOT NULL REFERENCES task_child(id))"
        )
        connection.exec_driver_sql("CREATE INDEX ix_task_child_run ON task_child(run_id)")
        connection.exec_driver_sql("CREATE TABLE run_touch_audit (run_id VARCHAR(128))")
        connection.exec_driver_sql(
            "CREATE TRIGGER run_title_touched AFTER UPDATE OF conversation_id "
            "ON product_project_agent_runs BEGIN INSERT INTO run_touch_audit VALUES (NEW.run_id); END"
        )
        connection.exec_driver_sql("INSERT INTO task_child VALUES (1, 'run-human')")
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
            connection.exec_driver_sql(
                "UPDATE product_project_agent_runs SET conversation_id='touched' "
                "WHERE run_id='run-human'"
            )
        assert inspect(engine).get_foreign_keys("task_child")[0]["referred_table"] == PROJECT_AGENT_RUNS.name
        assert "ix_task_child_run" in {item["name"] for item in inspect(engine).get_indexes("task_child")}
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO task_child VALUES (2, 'missing-run')")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM run_touch_audit").scalar_one() == 3
    assert not any("FOREIGN_KEYS=OFF" in item.replace(" ", "") for item in statements)
    engine.dispose()


def test_postgresql_run_table_ddl_contains_receipt_check_and_no_new_foreign_keys():
    ddl = str(CreateTable(PROJECT_AGENT_RUNS).compile(dialect=postgresql.dialect()))
    assert "task_contract_version INTEGER" in ddl
    assert "task_result_status VARCHAR(32)" in ddl
    assert "task_result_json JSON" in ddl
    assert "task_result_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "CONSTRAINT ck_product_project_agent_runs_task_result CHECK" in ddl
    assert "task_result_status IN ('submitted','invalid_output','failed','cancelled')" in ddl
    assert "FOREIGN KEY (task_contract_version)" not in ddl
    assert "FOREIGN KEY (task_result_json)" not in ddl


def test_postgresql_upgrade_compiles_four_columns_and_named_check():
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
    assert "ALTER TABLE product_project_agent_runs ADD COLUMN task_contract_version INTEGER" in ddl
    assert "ALTER TABLE product_project_agent_runs ADD COLUMN task_result_status VARCHAR(32)" in ddl
    assert "ALTER TABLE product_project_agent_runs ADD COLUMN task_result_json JSON" in ddl
    assert "ALTER TABLE product_project_agent_runs ADD COLUMN task_result_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "ADD CONSTRAINT ck_product_project_agent_runs_task_result CHECK" in ddl
