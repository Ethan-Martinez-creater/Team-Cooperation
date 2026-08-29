from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.pool import StaticPool

from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    ProjectWorkspaceService,
    TeamAccountRole,
)
from coifesp_harness.product.repository import TEAM_AGENT_PROFILES


def _engine():
    return create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _product_stack():
    engine = _engine()
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    accounts.register_team(team_id="team-a", team_handle="team-a", team_name="Team A")
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="profile test",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    return engine


def test_default_profile_is_idempotent_runtime_policy_only_and_agent_is_pinned():
    engine = _product_stack()
    workspace = ProjectWorkspaceService(engine)

    first = workspace.ensure_team_agent_profile(team_id="team-a")
    second = workspace.ensure_team_agent_profile(team_id="team-a")
    agent = workspace.ensure_team_project_agent(
        project_id="project-a", team_id="team-a"
    )

    assert first == second
    assert first.profile_id == "team-a"
    assert first.version == 1
    assert first.autonomy_level == "bounded"
    assert first.max_run_budget_profile["max_turns"] == 20
    assert not {"tags", "capabilities", "capacity"} & set(first.max_run_budget_profile)
    assert agent.profile_id == first.profile_id
    assert agent.profile_version == first.version
    with engine.connect() as connection:
        assert connection.execute(select(TEAM_AGENT_PROFILES)).mappings().all()


def test_migration_51_backfills_existing_team_agents_and_downgrades():
    engine = _engine()
    metadata = MetaData()
    teams = Table(
        "product_teams",
        metadata,
        Column("team_id", String(128), primary_key=True),
        Column("name", String(128), nullable=False),
    )
    agents = Table(
        "product_team_project_agents",
        metadata,
        Column("agent_id", String(128), primary_key=True),
        Column("project_id", String(128), nullable=False),
        Column("team_id", String(128), nullable=False),
        Column("status", String(32), nullable=False),
        Column("memory_version", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    from datetime import UTC, datetime

    now = datetime(2026, 8, 30, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(teams.insert().values(team_id="team-a", name="Team A"))
        connection.execute(
            agents.insert().values(
                agent_id="agent-a",
                project_id="project-a",
                team_id="team-a",
                status="active",
                memory_version=1,
                created_at=now,
                updated_at=now,
            )
        )
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260830_51_team_agent_profiles.py"
    )
    spec = spec_from_file_location("team_agent_profiles_51", path)
    migration = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    inspector = inspect(engine)
    columns = {
        item["name"]
        for item in inspector.get_columns("product_team_project_agents")
    }
    assert {"profile_id", "profile_version"} <= columns
    with engine.connect() as connection:
        profile = connection.execute(
            select(Table("product_team_agent_profiles", MetaData(), autoload_with=engine))
        ).mappings().one()
        migrated_agent = connection.execute(
            select(
                Table("product_team_project_agents", MetaData(), autoload_with=engine)
            )
        ).mappings().one()
    assert profile["profile_id"] == "team-a"
    assert profile["display_name"] == "Team A Agent"
    assert migrated_agent["profile_id"] == "team-a"
    assert migrated_agent["profile_version"] == 1

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
    assert "product_team_agent_profiles" not in inspect(engine).get_table_names()
