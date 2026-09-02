from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    MetaData,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateIndex, CreateTable

from coifesp_harness.control_plane.bootstrap import SCHEMA_REVISION
from coifesp_harness.product.models import ProjectAgentRunKind
from coifesp_harness.product.repository import (
    PRODUCT_METADATA,
    PROJECT_AGENT_RUNS,
    SPECIALIST_DELEGATIONS,
)
from coifesp_harness.tool_jobs import TOOL_JOB_EVENTS, TOOL_JOBS, ToolJobStatus

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260901_63_specialist_delegations.py"
)
_RUN_UNIQUE = "uq_product_project_agent_run_task_attempt"
_RUN_LEGACY_CHECK = "ck_product_project_agent_runs_legacy_identity"
_RUN_SPECIALIST_CHECK = "ck_product_project_agent_runs_specialist"
_TOOL_STATUS_CHECK = "ck_tool_jobs_status"
_OLD_RUN_LEGACY_CHECK = (
    "run_kind = 'task_execution' OR (created_by IS NOT NULL AND mode IS NOT NULL)"
)
_OLD_TOOL_STATUS_CHECK = (
    "status IN ('queued','leased','running','retry_wait','succeeded','failed','cancelled')"
)


def _module():
    spec = spec_from_file_location("specialist_delegations_63", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _migrate(engine, direction: str):
    with engine.begin() as connection:
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, direction)()


def _legacy_metadata():
    metadata = MetaData()
    for table in PRODUCT_METADATA.tables.values():
        if table.name in {PROJECT_AGENT_RUNS.name, SPECIALIST_DELEGATIONS.name}:
            continue
        table.to_metadata(metadata)

    runs = PROJECT_AGENT_RUNS.to_metadata(metadata)
    for constraint in list(runs.constraints):
        if constraint.name in {_RUN_LEGACY_CHECK, _RUN_SPECIALIST_CHECK}:
            runs.constraints.remove(constraint)
    for index in list(runs.indexes):
        if index.name == _RUN_UNIQUE:
            runs.indexes.remove(index)
    runs.append_constraint(CheckConstraint(_OLD_RUN_LEGACY_CHECK, name=_RUN_LEGACY_CHECK))
    runs.append_constraint(
        UniqueConstraint(
            "process_id",
            "team_task_id",
            "execution_attempt",
            name=_RUN_UNIQUE,
        )
    )

    jobs = TOOL_JOBS.to_metadata(metadata)
    for constraint in list(jobs.constraints):
        if constraint.name in {"status", _TOOL_STATUS_CHECK}:
            jobs.constraints.remove(constraint)
    jobs.append_constraint(CheckConstraint(_OLD_TOOL_STATUS_CHECK, name=_TOOL_STATUS_CHECK))
    TOOL_JOB_EVENTS.to_metadata(metadata)
    return metadata


def _engine():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    _legacy_metadata().create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO product_teams "
                "(team_id, handle, handle_key, name, created_at) "
                "VALUES ('team-a','team-a','team-a','Team A', :now), "
                "('team-b','team-b','team-b','Team B', :now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_accounts "
                "(account_id, username, username_key, display_name, email, email_key, "
                "password_hash, team_id, team_role, registration_status, must_change_password, "
                "enabled, created_at) VALUES "
                "('account-a','account-a','account-a','Account A','a@example.invalid',"
                "'a@example.invalid','unused','team-a','admin','active',0,1,:now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_projects "
                "(project_id,name,description,owner_team_id,created_by,created_at) "
                "VALUES ('project-a','Project A','test','team-a','account-a',:now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_project_participations "
                "(project_id,team_id,name,kind,assigned_by,created_at) "
                "VALUES ('project-a','team-a','Owner','product','account-a',:now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_team_agent_profiles "
                "(profile_id,version,team_id,display_name,tool_policy_id,skill_policy_id,"
                "model_policy_id,memory_policy_id,autonomy_level,max_run_budget_profile,created_at) "
                "VALUES ('profile-a',1,'team-a','Agent A','default','default','default',"
                "'default','bounded','{}',:now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_team_project_agents "
                "(agent_id,project_id,team_id,status,memory_version,profile_id,profile_version,"
                "created_at,updated_at) VALUES "
                "('agent-a','project-a','team-a','active',1,'profile-a',1,:now,:now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_team_tasks "
                "(task_id,project_id,source_team_id,target_team_id,created_by,title,description,"
                "acceptance_criteria,status,assigned_account_id,artifact_resource_ids,review_note,"
                "priority,schedule_version,created_at,updated_at) VALUES "
                "('task-a','project-a','team-b','team-a','account-a','Task A','test',"
                "'tests pass','accepted',NULL,'[]','', 'normal',1,:now,:now)"
            ),
            {"now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO product_project_agent_runs "
                "(project_id,run_id,team_id,created_by,mode,conversation_id,turn_id,run_kind,"
                "created_at) VALUES "
                "('project-a','run-human','team-a','account-a','analysis','conv-a','turn-a',"
                "'conversation',:now)"
            ),
            {"now": _NOW},
        )
        _insert_tool_job(connection, "job-a")
    return engine


def _insert_tool_job(connection, job_id: str, *, status: str = "queued"):
    connection.execute(
        text(
            "INSERT INTO tool_jobs "
            "(tenant_id,job_id,run_id,call_id,tool_name,idempotency_key,request_digest,"
            "arguments_ciphertext,arguments_nonce,arguments_fingerprint,arguments_key_id,"
            "status,attempt_count,max_attempts,available_at,created_by,created_at,updated_at) "
            "VALUES ('team-a',:job_id,'run-human',:call_id,'specialist',:idem,:digest,"
            "X'78',X'6E',:digest,'key',:status,0,3,:now,'account-a',:now,:now)"
        ),
        {
            "job_id": job_id,
            "call_id": f"call-{job_id}",
            "idem": f"idem-{job_id}",
            "digest": "a" * 64,
            "status": status,
            "now": _NOW,
        },
    )


def _delegation_values(suffix: str = "a", **changes):
    values = {
        "delegation_id": f"delegation-{suffix}",
        "idempotency_key": f"delegation-idem-{suffix}",
        "project_id": "project-a",
        "process_id": "process-a",
        "work_node_id": f"node-{suffix}",
        "team_id": "team-a",
        "team_agent_id": "agent-a",
        "team_task_id": "task-a",
        "parent_run_id": "run-human",
        "child_run_id": f"child-{suffix}",
        "tool_job_tenant_id": "team-a",
        "tool_job_id": f"job-{suffix}",
        "specialist_kind": "security_review",
        "depth": 1,
        "purpose": "Review the bounded security surface.",
        "request_json": {"question": "review"},
        "context_scope_digest": "b" * 64,
        "profile_digest": "c" * 64,
        "output_schema_digest": "d" * 64,
        "project_budget_reservation_id": f"budget-{suffix}",
        "status": "PENDING",
        "result_json": None,
        "error_code": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "completed_at": None,
    }
    values.update(changes)
    return values


def _specialist_run(suffix: str = "a", **changes):
    values = {
        "project_id": "project-a",
        "run_id": f"specialist-run-{suffix}",
        "team_id": "team-a",
        "created_by": None,
        "mode": None,
        "conversation_id": None,
        "turn_id": None,
        "process_id": "process-a",
        "team_agent_id": "agent-a",
        "work_node_id": f"node-{suffix}",
        "team_task_id": "task-a",
        "parent_run_id": "run-human",
        "orchestration_decision_id": None,
        "run_kind": "specialist",
        "initiated_by_principal_id": "team-agent:team-a",
        "executed_as_principal_id": "specialist-agent:team-a:security_review",
        "delegation_scope_digest": "b" * 64,
        "execution_attempt": None,
        "capacity_reservation_id": None,
        "project_budget_reservation_id": f"budget-{suffix}",
        "created_at": _NOW,
    }
    values.update(changes)
    return values


def _task_run(suffix: str = "a", **changes):
    values = {
        "project_id": "project-a",
        "run_id": f"task-run-{suffix}",
        "team_id": "team-a",
        "created_by": None,
        "mode": None,
        "conversation_id": None,
        "turn_id": None,
        "process_id": "process-a",
        "team_agent_id": "agent-a",
        "work_node_id": f"task-node-{suffix}",
        "team_task_id": "task-a",
        "parent_run_id": None,
        "orchestration_decision_id": f"decision-{suffix}",
        "run_kind": "task_execution",
        "initiated_by_principal_id": "service:project-orchestrator",
        "executed_as_principal_id": "team-agent:team-a",
        "delegation_scope_digest": "e" * 64,
        "execution_attempt": int(suffix) if suffix.isdigit() else 1,
        "capacity_reservation_id": f"capacity-{suffix}",
        "project_budget_reservation_id": f"task-budget-{suffix}",
        "created_at": _NOW,
    }
    values.update(changes)
    return values


def _insert(engine, table, values):
    with engine.begin() as connection:
        connection.execute(table.insert().values(**values))


def test_repository_metadata_exposes_specialist_contract_and_wait_state():
    columns = {column.name: column for column in SPECIALIST_DELEGATIONS.columns}
    assert {
        "delegation_id",
        "idempotency_key",
        "project_id",
        "process_id",
        "work_node_id",
        "team_id",
        "team_agent_id",
        "team_task_id",
        "parent_run_id",
        "child_run_id",
        "tool_job_tenant_id",
        "tool_job_id",
        "specialist_kind",
        "depth",
        "purpose",
        "request_json",
        "context_scope_digest",
        "profile_digest",
        "output_schema_digest",
        "project_budget_reservation_id",
        "status",
        "result_json",
        "error_code",
        "created_at",
        "updated_at",
        "completed_at",
    } <= set(columns)
    assert columns["depth"].nullable is False
    assert columns["context_scope_digest"].type.length == 64
    assert columns["profile_digest"].type.length == 64
    assert columns["output_schema_digest"].type.length == 64
    assert {
        foreign_key.target_fullname
        for foreign_key in SPECIALIST_DELEGATIONS.foreign_keys
    } >= {
        "product_projects.project_id",
        "product_teams.team_id",
        "product_team_project_agents.agent_id",
        "product_team_tasks.task_id",
        "tool_jobs.tenant_id",
        "tool_jobs.job_id",
    }
    assert ToolJobStatus.AWAITING_SPECIALIST.value == "awaiting_specialist"
    partial = next(index for index in PROJECT_AGENT_RUNS.indexes if index.name == _RUN_UNIQUE)
    ddl = str(CreateIndex(partial).compile(dialect=postgresql.dialect()))
    assert "WHERE run_kind = 'task_execution'" in ddl
    assert ProjectAgentRunKind.SPECIALIST.value == "specialist"


def test_upgrade_preserves_legacy_rows_and_all_inbound_fks():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE run_child (id INTEGER PRIMARY KEY, run_id VARCHAR(128) "
            "NOT NULL REFERENCES product_project_agent_runs(run_id))"
        )
        connection.exec_driver_sql("INSERT INTO run_child VALUES (1, 'run-human')")
        connection.exec_driver_sql(
            "INSERT INTO tool_job_events "
            "(tenant_id,job_id,sequence,event_id,event_type,actor_id,to_status,occurred_at) "
            "VALUES ('team-a','job-a',1,'event-a','queued','account-a','queued',:now)",
            {"now": _NOW},
        )
    before = _run_row(engine, "run-human")
    _migrate(engine, "upgrade")
    inspector = inspect(engine)
    assert "product_specialist_delegations" in inspector.get_table_names()
    assert _run_row(engine, "run-human") == before
    assert _RUN_SPECIALIST_CHECK in {
        item["name"] for item in inspector.get_check_constraints(PROJECT_AGENT_RUNS.name)
    }
    assert _RUN_UNIQUE in {item["name"] for item in inspector.get_indexes(PROJECT_AGENT_RUNS.name)}
    assert _TOOL_STATUS_CHECK in {
        item["name"] for item in inspector.get_check_constraints("tool_jobs")
    }
    assert inspector.get_foreign_keys("run_child")[0]["referred_table"] == PROJECT_AGENT_RUNS.name
    assert inspector.get_foreign_keys("tool_job_events")[0]["referred_table"] == "tool_jobs"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.execute(
            select(TOOL_JOBS.c.status).where(TOOL_JOBS.c.job_id == "job-a")
        ).scalar_one() == "queued"
        connection.execute(
            text(
                "UPDATE tool_jobs SET status='awaiting_specialist' "
                "WHERE tenant_id='team-a' AND job_id='job-a'"
            )
        )
        assert connection.execute(
            text("SELECT status FROM tool_jobs WHERE job_id='job-a'")
        ).scalar_one() == "awaiting_specialist"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO run_child (id,run_id) VALUES (2,'run-human')"
            )
        )
    engine.dispose()


def _run_row(engine, run_id):
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT project_id,run_id,team_id,created_by,mode,conversation_id,turn_id,"
                    "run_kind,initiated_by_principal_id,executed_as_principal_id "
                    "FROM product_project_agent_runs WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            ).mappings().one()
        )


def test_specialist_delegation_binding_and_lifecycle_constraints():
    engine = _engine()
    _migrate(engine, "upgrade")
    _insert(engine, PROJECT_AGENT_RUNS, _specialist_run())
    _insert(engine, SPECIALIST_DELEGATIONS, _delegation_values())
    with engine.connect() as connection:
        row = connection.execute(
            select(SPECIALIST_DELEGATIONS).where(
                SPECIALIST_DELEGATIONS.c.delegation_id == "delegation-a"
            )
        ).mappings().one()
    assert row["status"] == "PENDING"
    assert row["tool_job_id"] == "job-a"

    for suffix, changes in (
        ("idem", {"idempotency_key": "delegation-idem-a"}),
        ("child", {"child_run_id": "child-a"}),
        ("job", {"tool_job_id": "job-a"}),
    ):
        with engine.begin() as connection:
            _insert_tool_job(connection, f"job-{suffix}")
        with pytest.raises(IntegrityError):
            _insert(engine, SPECIALIST_DELEGATIONS, _delegation_values(suffix, **changes))

    invalid = (
        {"depth": 0},
        {"context_scope_digest": "b" * 63},
        {"profile_digest": "c" * 65},
        {"output_schema_digest": "x"},
        {"tool_job_tenant_id": "team-b"},
        {"status": "UNKNOWN"},
        {"status": "PENDING", "completed_at": _NOW},
        {"status": "COMPLETED", "completed_at": _NOW},
        {"status": "COMPLETED", "completed_at": _NOW, "result_json": {}, "error_code": "err"},
        {"status": "FAILED", "completed_at": _NOW},
        {"status": "FAILED", "completed_at": _NOW, "error_code": "failed", "result_json": {}},
        {"status": "RUNNING", "error_code": "still-running"},
    )
    for index, changes in enumerate(invalid, start=1):
        suffix = f"invalid-{index}"
        with engine.begin() as connection:
            _insert_tool_job(connection, f"job-{suffix}")
        with pytest.raises(IntegrityError):
            _insert(engine, SPECIALIST_DELEGATIONS, _delegation_values(suffix, **changes))

    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "DELETE FROM tool_jobs WHERE tenant_id='team-a' AND job_id='job-a'"
            )
        )
    engine.dispose()


def test_specialist_run_identity_and_task_attempt_uniqueness_are_separate():
    engine = _engine()
    _migrate(engine, "upgrade")
    _insert(engine, PROJECT_AGENT_RUNS, _task_run("1"))
    _insert(engine, PROJECT_AGENT_RUNS, _specialist_run("same"))
    _insert(engine, PROJECT_AGENT_RUNS, _task_run("2"))
    with pytest.raises(IntegrityError):
        _insert(engine, PROJECT_AGENT_RUNS, _task_run("duplicate", run_id="task-run-duplicate", execution_attempt=1))

    invalid = (
        {"parent_run_id": None},
        {"project_id": None},
        {"process_id": None},
        {"team_agent_id": None},
        {"work_node_id": None},
        {"team_task_id": None},
        {"initiated_by_principal_id": "service:project-orchestrator"},
        {"executed_as_principal_id": "team-agent:team-a"},
        {"delegation_scope_digest": "x"},
        {"project_budget_reservation_id": None},
        {"created_by": "account-a"},
        {"mode": "analysis"},
    )
    for index, changes in enumerate(invalid, start=1):
        with pytest.raises(IntegrityError):
            _insert(
                engine,
                PROJECT_AGENT_RUNS,
                _specialist_run(f"invalid-{index}", **changes),
            )
    engine.dispose()


def test_downgrade_refuses_specialist_rows_before_schema_mutation():
    engine = _engine()
    _migrate(engine, "upgrade")
    _insert(engine, PROJECT_AGENT_RUNS, _specialist_run())
    columns_before = {item["name"] for item in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)}
    with pytest.raises(RuntimeError, match="specialist Agent runs"):
        _migrate(engine, "downgrade")
    assert columns_before == {
        item["name"] for item in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)
    }
    assert "product_specialist_delegations" in inspect(engine).get_table_names()
    engine.dispose()

    delegation_engine = _engine()
    _migrate(delegation_engine, "upgrade")
    _insert(delegation_engine, SPECIALIST_DELEGATIONS, _delegation_values())
    with pytest.raises(RuntimeError, match="specialist delegation data"):
        _migrate(delegation_engine, "downgrade")
    assert "product_specialist_delegations" in inspect(delegation_engine).get_table_names()
    delegation_engine.dispose()


def test_empty_downgrade_restores_legacy_constraints_and_fk_children():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE run_child (id INTEGER PRIMARY KEY, run_id VARCHAR(128) "
            "NOT NULL REFERENCES product_project_agent_runs(run_id))"
        )
        connection.exec_driver_sql("INSERT INTO run_child VALUES (1, 'run-human')")
    _migrate(engine, "upgrade")
    _migrate(engine, "downgrade")
    inspector = inspect(engine)
    assert "product_specialist_delegations" not in inspector.get_table_names()
    assert _RUN_SPECIALIST_CHECK not in {
        item["name"] for item in inspector.get_check_constraints(PROJECT_AGENT_RUNS.name)
    }
    assert _RUN_UNIQUE in {
        item["name"] for item in inspector.get_unique_constraints(PROJECT_AGENT_RUNS.name)
    }
    assert any(
        item["name"] in {"status", _TOOL_STATUS_CHECK}
        and _OLD_TOOL_STATUS_CHECK.replace(" ", "") in item["sqltext"].replace(" ", "")
        for item in inspector.get_check_constraints("tool_jobs")
    )
    assert inspector.get_foreign_keys("run_child")[0]["referred_table"] == PROJECT_AGENT_RUNS.name
    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE tool_jobs SET status='awaiting_specialist' "
                    "WHERE tenant_id='team-a' AND job_id='job-a'"
                )
            )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_postgresql_ddl_and_revision_head_are_aligned():
    run_ddl = str(CreateTable(PROJECT_AGENT_RUNS).compile(dialect=postgresql.dialect()))
    delegation_ddl = str(
        CreateTable(SPECIALIST_DELEGATIONS).compile(dialect=postgresql.dialect())
    )
    assert "ck_product_project_agent_runs_specialist" in run_ddl
    assert "specialist-agent:" in run_ddl
    assert "UNIQUE (process_id, team_task_id, execution_attempt)" not in run_ddl
    assert "FOREIGN KEY(tool_job_tenant_id, tool_job_id)" in delegation_ddl
    assert "ON DELETE RESTRICT" in delegation_ddl
    assert "tool_job_tenant_id = team_id" in delegation_ddl
    assert "status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')" in delegation_ddl
    migration = _module()
    output = StringIO()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name="postgresql",
            opts={"as_sql": True, "output_buffer": output},
        )
    )
    migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE product_specialist_delegations" in sql
    assert "awaiting_specialist" in sql
    assert "WHERE run_kind = 'task_execution'" in sql
    assert migration.revision == "20260901_63"
    assert migration.down_revision == "20260901_62"
    assert ScriptDirectory(str(Path(__file__).parents[1] / "alembic")).get_heads() == [
        SCHEMA_REVISION
    ]
