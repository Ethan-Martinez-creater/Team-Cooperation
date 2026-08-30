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

from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product import ProductAccountService, TeamCollaborationService
from coifesp_harness.product.models import ProjectAgentRunKind
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
_BINDING_FIELDS = {
    "process_id",
    "team_agent_id",
    "work_node_id",
    "team_task_id",
    "parent_run_id",
    "orchestration_decision_id",
    "run_kind",
    "initiated_by_principal_id",
    "executed_as_principal_id",
    "delegation_scope_digest",
    "execution_attempt",
    "capacity_reservation_id",
    "project_budget_reservation_id",
}


def _migration():
    path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "20260830_52_team_task_run_bindings.py"
    )
    spec = spec_from_file_location("team_task_run_bindings_52", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "20260830_52"
    assert migration.down_revision == "20260830_51"
    return migration


def _migrate(engine, direction):
    with engine.begin() as connection:
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, direction)()


def _legacy_metadata(mode_constraint_name):
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
        Column(
            "created_by", String(128), ForeignKey("product_accounts.account_id"), nullable=False
        ),
        Column("mode", String(32), nullable=False),
        Column("conversation_id", String(128), nullable=True),
        Column("turn_id", String(128), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        CheckConstraint(
            "mode IN ('analysis','collaboration_actions','delivery_review')",
            name=mode_constraint_name,
        ),
        UniqueConstraint("run_id", name="uq_product_project_agent_run"),
    )
    Index("ix_product_project_agent_runs_conversation", runs.c.conversation_id, runs.c.turn_id)
    return metadata


def _engine(*, legacy=False, mode_constraint_name="ck_product_project_agent_runs_mode"):
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    if legacy:
        _legacy_metadata(mode_constraint_name).create_all(engine)
    else:
        # Full product schema must not require process/work-graph metadata.
        ProductAccountService(engine).create_schema()
    with engine.begin() as connection:
        connection.execute(
            TEAMS.insert(),
            [
                dict(team_id=team, handle=team, handle_key=team, name=team, created_at=_NOW)
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
        connection.execute(
            PROJECT_AGENT_RUNS.insert().values(
                project_id="project-a",
                run_id="run-human",
                team_id="team-a",
                created_by="account-a",
                mode="analysis",
                conversation_id="conversation-a",
                turn_id="turn-a",
                created_at=_NOW,
            )
        )
    return engine


@pytest.fixture(params=["metadata", "migration"])
def binding_engine(request):
    engine = _engine(legacy=request.param == "migration")
    if request.param == "migration":
        _migrate(engine, "upgrade")
    yield engine
    engine.dispose()


def _automatic_run(**changes):
    values = dict(
        project_id="project-a",
        run_id="run-automatic",
        team_id="team-a",
        created_by=None,
        mode=None,
        process_id="process-a",
        team_agent_id="agent-a",
        work_node_id="node-a",
        team_task_id="task-a",
        parent_run_id=None,
        orchestration_decision_id="decision-a",
        run_kind="task_execution",
        initiated_by_principal_id="service:project-orchestrator",
        executed_as_principal_id="team-agent:team-a",
        delegation_scope_digest="d" * 64,
        execution_attempt=1,
        capacity_reservation_id="capacity-a",
        project_budget_reservation_id="budget-a",
        created_at=_NOW,
    )
    values.update(changes)
    return values


def _insert(engine, **values):
    with engine.begin() as connection:
        connection.execute(PROJECT_AGENT_RUNS.insert().values(**values))


def _row(engine, run_id="run-human"):
    table = Table(PROJECT_AGENT_RUNS.name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return dict(
            connection.execute(select(table).where(table.c.run_id == run_id)).mappings().one()
        )


@pytest.mark.parametrize("mode_constraint_name", ["mode", "ck_product_project_agent_runs_mode"])
def test_migration_52_preserves_legacy_rows_constraints_and_indices_on_round_trip(
    mode_constraint_name,
):
    engine = _engine(legacy=True, mode_constraint_name=mode_constraint_name)
    before = _row(engine)
    _migrate(engine, "upgrade")
    after = _row(engine)
    assert {key: after[key] for key in before} == before
    assert after["run_kind"] == "conversation"
    assert after["initiated_by_principal_id"] == after["executed_as_principal_id"] == "account-a"
    assert all(
        after[name] is None
        for name in _BINDING_FIELDS
        - {
            "run_kind",
            "initiated_by_principal_id",
            "executed_as_principal_id",
        }
    )
    columns = {
        column["name"]: column for column in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)
    }
    assert columns["created_by"]["nullable"] is True
    assert columns["mode"]["nullable"] is True
    assert columns["run_kind"]["nullable"] is False
    assert columns["run_kind"]["default"] == "'conversation'"
    foreign_keys = inspect(engine).get_foreign_keys(PROJECT_AGENT_RUNS.name)
    assert {(tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in foreign_keys} >= {
        (("created_by",), "product_accounts"),
        (("team_agent_id",), "product_team_project_agents"),
        (("team_task_id",), "product_team_tasks"),
    }
    # Old writers still omit all newly introduced columns after the upgrade.
    _insert(
        engine,
        project_id="project-a",
        run_id="run-later-human",
        team_id="team-a",
        created_by="account-a",
        mode="delivery_review",
        created_at=_NOW,
    )
    later_before = _row(engine, "run-later-human")
    assert later_before["run_kind"] == "conversation"
    assert later_before["initiated_by_principal_id"] is None
    _migrate(engine, "downgrade")
    assert _row(engine) == before
    assert _row(engine, "run-later-human") == {key: later_before[key] for key in before}
    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns(PROJECT_AGENT_RUNS.name)}
    assert not _BINDING_FIELDS & columns.keys()
    assert columns["created_by"]["nullable"] is False
    assert columns["mode"]["nullable"] is False
    assert mode_constraint_name in {
        item["name"] for item in inspector.get_check_constraints(PROJECT_AGENT_RUNS.name)
    }
    assert "ix_product_project_agent_runs_conversation" in {
        item["name"] for item in inspector.get_indexes(PROJECT_AGENT_RUNS.name)
    }
    assert "uq_product_project_agent_run" in {
        item["name"] for item in inspector.get_unique_constraints(PROJECT_AGENT_RUNS.name)
    }
    assert any(
        fk["constrained_columns"] == ["created_by"] and fk["referred_table"] == "product_accounts"
        for fk in inspector.get_foreign_keys(PROJECT_AGENT_RUNS.name)
    )


def test_valid_automatic_run_uses_service_identity_and_next_attempt_is_allowed(binding_engine):
    _insert(binding_engine, **_automatic_run())
    _insert(
        binding_engine,
        **_automatic_run(run_id="run-retry", execution_attempt=2, parent_run_id="run-automatic"),
    )
    row = _row(binding_engine, "run-automatic")
    assert row["created_by"] is None
    assert row["mode"] is None
    assert row["parent_run_id"] is None
    assert row["executed_as_principal_id"] == "team-agent:team-a"
    assert _row(binding_engine, "run-retry")["execution_attempt"] == 2


@pytest.mark.parametrize("field", sorted(_BINDING_FIELDS - {"parent_run_id", "run_kind"}))
def test_automatic_run_requires_every_identity_and_execution_binding(binding_engine, field):
    with pytest.raises(IntegrityError):
        _insert(binding_engine, **_automatic_run(**{field: None}))


@pytest.mark.parametrize(
    "changes",
    [
        {"initiated_by_principal_id": "account-a"},
        {"executed_as_principal_id": "account-a"},
        {"executed_as_principal_id": "team-agent:team-b"},
        {"team_id": "team-b"},
        {"created_by": "account-a"},
        {"mode": "analysis"},
        {"execution_attempt": 0},
        {"execution_attempt": -1},
        {"delegation_scope_digest": "d" * 63},
        {"delegation_scope_digest": "d" * 65},
        {"team_agent_id": "missing-agent"},
        {"team_task_id": "missing-task"},
        {"run_kind": None},
        {"run_kind": "unknown"},
    ],
)
def test_automatic_run_rejects_invalid_identity_attempt_digest_and_foreign_keys(
    binding_engine, changes
):
    with pytest.raises(IntegrityError):
        _insert(binding_engine, **_automatic_run(**changes))


def test_same_process_task_attempt_cannot_be_dispatched_twice(binding_engine):
    _insert(binding_engine, **_automatic_run())
    with pytest.raises(IntegrityError):
        _insert(binding_engine, **_automatic_run(run_id="run-duplicate"))


@pytest.mark.parametrize(
    "run_kind",
    [item.value for item in ProjectAgentRunKind if item is not ProjectAgentRunKind.TASK_EXECUTION],
)
def test_non_task_run_still_requires_real_account_and_legacy_mode(binding_engine, run_kind):
    values = dict(
        project_id="project-a",
        run_id="run-legacy",
        team_id="team-a",
        created_by="account-a",
        mode="analysis",
        run_kind=run_kind,
        created_at=_NOW,
    )
    for changes in (
        {"created_by": None},
        {"mode": None},
        {"created_by": "missing-account"},
        {"mode": "task_execution"},
    ):
        with pytest.raises(IntegrityError):
            _insert(binding_engine, **(values | changes))
    _insert(binding_engine, **values)


def test_legacy_binding_list_excludes_automatic_run_and_cannot_claim_it(binding_engine):
    service = TeamCollaborationService(binding_engine)
    _insert(binding_engine, **_automatic_run())
    service.bind_project_agent_run(
        project_id="project-a", run_id="run-another-human", actor_id="account-a"
    )
    listed = service.list_project_agent_runs(project_id="project-a", actor_id="account-a")
    assert {item.run_id for item in listed} == {"run-human", "run-another-human"}
    assert all(item.created_by == "account-a" and item.mode.value == "analysis" for item in listed)
    with pytest.raises(PolicyDenied):
        service.assert_project_agent_run(
            project_id="project-a", run_id="run-automatic", actor_id="account-a"
        )
    with pytest.raises(GovernanceConflictError):
        service.bind_project_agent_run(
            project_id="project-a", run_id="run-automatic", actor_id="account-a"
        )


def test_downgrade_refuses_automatic_rows_before_mutating_schema_or_data():
    engine = _engine(legacy=True)
    _migrate(engine, "upgrade")
    _insert(engine, **_automatic_run())
    rows_before = [_row(engine, run_id) for run_id in ("run-human", "run-automatic")]

    def columns():
        return [
            column | {"type": str(column["type"])}
            for column in inspect(engine).get_columns(PROJECT_AGENT_RUNS.name)
        ]

    columns_before = columns()
    with pytest.raises(RuntimeError, match="Cannot downgrade 20260830_52.*NULL created_by or mode"):
        _migrate(engine, "downgrade")
    assert columns() == columns_before
    assert [_row(engine, run_id) for run_id in ("run-human", "run-automatic")] == rows_before


def test_postgresql_run_table_compiles_without_cross_metadata_dependencies():
    ddl = str(CreateTable(PROJECT_AGENT_RUNS).compile(dialect=postgresql.dialect()))
    assert "executed_as_principal_id = 'team-agent:' || team_id" in ddl
    assert "length(delegation_scope_digest) = 64" in ddl
    assert "UNIQUE (process_id, team_task_id, execution_attempt)" in ddl
    assert {fk.column.table.metadata for fk in PROJECT_AGENT_RUNS.foreign_keys} == {
        PRODUCT_METADATA
    }


def test_postgresql_upgrade_emits_portable_constraints_and_legacy_backfill():
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
    assert "ALTER COLUMN created_by DROP NOT NULL" in ddl
    assert "ALTER COLUMN mode DROP NOT NULL" in ddl
    assert "executed_as_principal_id = 'team-agent:' || team_id" in ddl
    assert "UNIQUE (process_id, team_task_id, execution_attempt)" in ddl
    assert "initiated_by_principal_id = created_by, executed_as_principal_id = created_by" in ddl


def test_fresh_metadata_schema_can_downgrade_and_reupgrade_legacy_rows():
    engine = _engine()
    before = _row(engine)
    _migrate(engine, "downgrade")
    assert _row(engine) == {
        key: value for key, value in before.items() if key not in _BINDING_FIELDS
    }
    _migrate(engine, "upgrade")
    assert _row(engine)["initiated_by_principal_id"] == "account-a"
