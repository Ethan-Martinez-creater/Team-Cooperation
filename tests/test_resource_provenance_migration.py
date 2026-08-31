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

from coifesp_harness.product.repository import PROJECT_RESOURCES
from coifesp_harness.product.service import ProjectResourceService

_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260831_59_resource_provenance.py"
)
_LEGACY_COLUMNS = (
    "resource_id",
    "project_id",
    "owner_team_id",
    "created_by",
    "title",
    "artifact_owner_team_id",
    "artifact_id",
    "artifact_sha256",
    "media_type",
    "propagation",
    "created_at",
)
_PROVENANCE_COLUMNS = (
    "produced_by_principal_id",
    "source_run_id",
    "source_integration_id",
    "process_id",
)
_COLUMNS = _LEGACY_COLUMNS + _PROVENANCE_COLUMNS
_CHECK_NAMES = {
    "propagation",
    "artifact_sha256",
    "ck_product_project_resources_provenance",
}
_INDEX_NAMES = {"ix_product_resources_project_created"}


class _SchemaVersionFailureBind:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def exec_driver_sql(self, statement, *args, **kwargs):
        if statement == "PRAGMA schema_version":
            raise RuntimeError("schema version probe failed")
        return self.connection.exec_driver_sql(statement, *args, **kwargs)


def _migration():
    spec = importlib.util.spec_from_file_location(
        "resource_provenance_revision_59", _MIGRATION_PATH
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


def _legacy_metadata():
    metadata = MetaData()
    Table("product_accounts", metadata, Column("account_id", String(128), primary_key=True))
    Table("product_teams", metadata, Column("team_id", String(128), primary_key=True))
    resources = Table(
        "product_project_resources",
        metadata,
        Column("resource_id", String(128), primary_key=True),
        Column("project_id", String(128), nullable=False),
        Column(
            "owner_team_id",
            String(128),
            ForeignKey("product_teams.team_id"),
            nullable=False,
        ),
        Column(
            "created_by",
            String(128),
            ForeignKey("product_accounts.account_id"),
            nullable=False,
        ),
        Column("title", String(256), nullable=False),
        Column("artifact_owner_team_id", String(128), nullable=False),
        Column("artifact_id", String(128), nullable=False),
        Column("artifact_sha256", String(64), nullable=False),
        Column("media_type", String(256), nullable=False),
        Column("propagation", String(32), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        CheckConstraint(
            "propagation IN ('team_private','project_readonly','portable')",
            name="propagation",
        ),
        CheckConstraint("length(artifact_sha256) = 64", name="artifact_sha256"),
        UniqueConstraint(
            "artifact_owner_team_id",
            "artifact_id",
            name="uq_product_resource_artifact",
        ),
    )
    Index(
        "ix_product_resources_project_created",
        resources.c.project_id,
        resources.c.created_at,
    )
    return metadata


def _engine(*, with_child: bool = False, url: str | None = None, seed: bool = True):
    engine_kwargs = {} if url is not None else {"poolclass": StaticPool}
    engine = create_engine(url or "sqlite+pysqlite://", **engine_kwargs)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    metadata = _legacy_metadata()
    metadata.create_all(engine)
    if seed:
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["product_accounts"].insert().values(account_id="account-human")
            )
            connection.execute(
                metadata.tables["product_teams"].insert().values(team_id="team-owner")
            )
    if with_child:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE resource_child ("
                "child_id INTEGER PRIMARY KEY, "
                "resource_id VARCHAR(128) NOT NULL "
                "REFERENCES product_project_resources(resource_id))"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_resource_child_resource ON resource_child(resource_id)"
            )
    return engine


def _base_resource(resource_id: str = "resource-human", **overrides):
    row = {
        "resource_id": resource_id,
        "project_id": "project-1",
        "owner_team_id": "team-owner",
        "created_by": "account-human",
        "title": "Legacy resource",
        "artifact_owner_team_id": "team-owner",
        "artifact_id": f"artifact-{resource_id}",
        "artifact_sha256": "a" * 64,
        "media_type": "text/plain",
        "propagation": "project_readonly",
        "created_at": _NOW,
    }
    row.update(overrides)
    return row


def _insert(engine, values):
    resources = Table(PROJECT_RESOURCES.name, MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(resources.insert().values(**values))


def _insert_child(engine, resource_id="resource-human"):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO resource_child VALUES (?, ?)", (1, resource_id)
        )


def _row(engine, resource_id="resource-human"):
    resources = Table(PROJECT_RESOURCES.name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(resources).where(resources.c.resource_id == resource_id)
            )
            .mappings()
            .one()
        )


def test_repository_metadata_matches_resource_provenance_contract():
    assert PROJECT_RESOURCES.metadata.tables[PROJECT_RESOURCES.name] is PROJECT_RESOURCES
    assert tuple(PROJECT_RESOURCES.columns.keys()) == _COLUMNS
    assert PROJECT_RESOURCES.c.created_by.nullable is True
    assert PROJECT_RESOURCES.c.created_by.type.length == 128
    assert PROJECT_RESOURCES.c.produced_by_principal_id.nullable is True
    assert PROJECT_RESOURCES.c.produced_by_principal_id.type.length == 256
    for name in ("source_run_id", "source_integration_id", "process_id"):
        assert PROJECT_RESOURCES.c[name].nullable is True
        assert PROJECT_RESOURCES.c[name].type.length == 128
    assert {
        constraint.name
        for constraint in PROJECT_RESOURCES.constraints
        if isinstance(constraint, CheckConstraint)
    } == _CHECK_NAMES
    provenance = next(
        constraint
        for constraint in PROJECT_RESOURCES.constraints
        if constraint.name == "ck_product_project_resources_provenance"
    )
    assert str(provenance.sqltext) == _migration()._PROVENANCE_CHECK
    assert {index.name for index in PROJECT_RESOURCES.indexes} == _INDEX_NAMES
    assert {
        (foreign_key.parent.name, foreign_key.target_fullname)
        for foreign_key in PROJECT_RESOURCES.foreign_keys
    } == {
        ("owner_team_id", "product_teams.team_id"),
        ("created_by", "product_accounts.account_id"),
    }


def test_revision_59_upgrade_matches_runtime_metadata_and_preserves_legacy_row():
    engine = _engine(with_child=True)
    legacy = _legacy_metadata().tables["product_project_resources"]
    with engine.begin() as connection:
        connection.execute(legacy.insert().values(_base_resource()))
    _insert_child(engine)
    before = _row(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").all() == [("ok",)]

    _migrate(engine, "upgrade")

    inspector = inspect(engine)
    migrated_columns = inspector.get_columns(PROJECT_RESOURCES.name)
    assert [column["name"] for column in migrated_columns] == list(_COLUMNS)
    assert [column["name"] for column in migrated_columns][-4:] == list(_PROVENANCE_COLUMNS)
    columns = {
        column["name"]: column for column in migrated_columns
    }
    assert columns["created_by"]["nullable"] is True
    assert {
        column["name"]: (column["nullable"], str(column["type"]))
        for column in migrated_columns
    } == {
        column.name: (column.nullable, str(column.type))
        for column in PROJECT_RESOURCES.columns
    }
    assert {
        item["name"]
        for item in inspector.get_check_constraints(PROJECT_RESOURCES.name)
    } == _CHECK_NAMES
    assert {
        item["name"] for item in inspector.get_indexes(PROJECT_RESOURCES.name)
    } == _INDEX_NAMES
    assert {
        (item["constrained_columns"][0], item["referred_table"], item["referred_columns"][0])
        for item in inspector.get_foreign_keys(PROJECT_RESOURCES.name)
    } == {
        ("owner_team_id", "product_teams", "team_id"),
        ("created_by", "product_accounts", "account_id"),
    }
    upgraded = _row(engine)
    assert upgraded == before | {name: None for name in _PROVENANCE_COLUMNS}
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA writable_schema").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").all() == [("ok",)]
        assert connection.exec_driver_sql(
            "SELECT resource_id FROM resource_child"
        ).scalar_one() == "resource-human"
    engine.dispose()


def test_valid_machine_resources_are_projected_without_a_human_owner():
    engine = _engine()
    _migrate(engine, "upgrade")
    _insert(
        engine,
        _base_resource(
            "resource-run",
            created_by=None,
            produced_by_principal_id="team-agent:team-owner",
            source_run_id="run-1",
            process_id="process-1",
        ),
    )
    _insert(
        engine,
        _base_resource(
            "resource-integration",
            created_by=None,
            produced_by_principal_id="team-agent:team-owner",
            source_integration_id="integration-1",
            process_id="process-1",
        ),
    )

    run_resource = ProjectResourceService._resource(_row(engine, "resource-run"))
    integration_resource = ProjectResourceService._resource(
        _row(engine, "resource-integration")
    )
    assert run_resource.created_by is None
    assert run_resource.produced_by_principal_id == "team-agent:team-owner"
    assert run_resource.source_run_id == "run-1"
    assert run_resource.source_integration_id is None
    assert run_resource.process_id == "process-1"
    assert integration_resource.source_run_id is None
    assert integration_resource.source_integration_id == "integration-1"
    engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"created_by": None},
        {"created_by": None, "process_id": "process-1"},
        {
            "created_by": None,
            "produced_by_principal_id": "",
            "source_run_id": "run-1",
            "process_id": "process-1",
        },
        {
            "created_by": None,
            "produced_by_principal_id": "machine-1",
            "source_run_id": "run-1",
            "source_integration_id": "integration-1",
            "process_id": "process-1",
        },
        {
            "created_by": None,
            "produced_by_principal_id": "machine-1",
            "source_run_id": "",
            "process_id": "process-1",
        },
        {
            "created_by": None,
            "produced_by_principal_id": "machine-1",
            "source_integration_id": "",
            "process_id": "process-1",
        },
        {
            "created_by": None,
            "produced_by_principal_id": "machine-1",
            "source_run_id": "run-1",
            "process_id": "",
        },
        {
            "created_by": "account-human",
            "produced_by_principal_id": "machine-1",
            "source_run_id": "run-1",
            "process_id": "process-1",
        },
    ],
)
def test_sqlite_provenance_check_rejects_missing_owner_mixed_or_empty_fields(changes):
    engine = _engine()
    _migrate(engine, "upgrade")
    with pytest.raises(IntegrityError):
        _insert(engine, _base_resource("resource-invalid", **changes))
    engine.dispose()


def test_downgrade_refuses_machine_provenance_before_schema_mutation():
    engine = _engine(with_child=True)
    legacy = _legacy_metadata().tables["product_project_resources"]
    with engine.begin() as connection:
        connection.execute(legacy.insert().values(_base_resource()))
    _insert_child(engine)
    _migrate(engine, "upgrade")
    _insert(
        engine,
        _base_resource(
            "resource-machine",
            created_by=None,
            produced_by_principal_id="machine-1",
            source_run_id="run-1",
            process_id="process-1",
        ),
    )
    columns_before = [
        item | {"type": str(item["type"])}
        for item in inspect(engine).get_columns(PROJECT_RESOURCES.name)
    ]

    with pytest.raises(RuntimeError, match="Cannot downgrade 20260831_59"):
        _migrate(engine, "downgrade")

    assert [
        item | {"type": str(item["type"])}
        for item in inspect(engine).get_columns(PROJECT_RESOURCES.name)
    ] == columns_before
    assert _row(engine, "resource-machine")["process_id"] == "process-1"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM resource_child").scalar_one() == 1
    engine.dispose()


def test_legacy_rows_and_child_fk_roundtrip_through_downgrade():
    engine = _engine(with_child=True)
    legacy = _legacy_metadata().tables["product_project_resources"]
    with engine.begin() as connection:
        connection.execute(legacy.insert().values(_base_resource()))
    _insert_child(engine)
    _migrate(engine, "upgrade")
    _migrate(engine, "downgrade")

    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns(PROJECT_RESOURCES.name)}
    assert [
        column["name"] for column in inspector.get_columns(PROJECT_RESOURCES.name)
    ] == list(_LEGACY_COLUMNS)
    assert set(columns) == set(_LEGACY_COLUMNS)
    assert columns["created_by"]["nullable"] is False
    assert _row(engine)["created_by"] == "account-human"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("SELECT count(*) FROM resource_child").scalar_one() == 1
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO resource_child VALUES (2, 'missing-resource')"
            )
    engine.dispose()


def test_empty_resource_table_roundtrips_upgrade_and_downgrade():
    engine = _engine()
    _migrate(engine, "upgrade")
    _migrate(engine, "downgrade")
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns(PROJECT_RESOURCES.name)
    }
    assert set(columns) == set(_LEGACY_COLUMNS)
    assert columns["created_by"]["nullable"] is False
    engine.dispose()


def test_sqlite_reopen_reflects_nullable_and_integrity_after_upgrade(tmp_path):
    database = tmp_path / "resource-provenance.db"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    engine = _engine(url=url)
    legacy = _legacy_metadata().tables["product_project_resources"]
    with engine.begin() as connection:
        connection.execute(legacy.insert().values(_base_resource()))
    _migrate(engine, "upgrade")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").all() == [("ok",)]
    engine.dispose()

    reopened = _engine(url=url, seed=False)
    columns = {
        column["name"]: column
        for column in inspect(reopened).get_columns(PROJECT_RESOURCES.name)
    }
    assert columns["created_by"]["nullable"] is True
    with reopened.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA writable_schema").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").all() == [("ok",)]
    reopened.dispose()


def test_sqlite_schema_rewrite_turns_writable_schema_off_after_error():
    engine = _engine()
    migration = _migration()
    with engine.begin() as connection:
        failing_bind = _SchemaVersionFailureBind(connection)
        with pytest.raises(RuntimeError, match="schema version probe failed"):
            migration._rewrite_sqlite_created_by(
                failing_bind,
                migration._CREATED_BY_NOT_NULL,
                r"\g<prefix>",
                "nullable",
            )
        assert connection.exec_driver_sql("PRAGMA writable_schema").scalar_one() == 0
    engine.dispose()


def test_postgresql_resource_ddl_has_provenance_check_and_only_legacy_fk():
    ddl = str(CreateTable(PROJECT_RESOURCES).compile(dialect=postgresql.dialect()))
    assert "created_by VARCHAR(128)" in ddl
    assert "produced_by_principal_id VARCHAR(256)" in ddl
    assert "source_run_id VARCHAR(128)" in ddl
    assert "source_integration_id VARCHAR(128)" in ddl
    assert "process_id VARCHAR(128)" in ddl
    assert "CONSTRAINT ck_product_project_resources_provenance CHECK" in ddl
    assert "FOREIGN KEY(created_by) REFERENCES product_accounts (account_id)" in ddl
    assert not PROJECT_RESOURCES.c.source_run_id.foreign_keys


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
    assert (
        "ALTER TABLE product_project_resources ADD COLUMN produced_by_principal_id VARCHAR(256)"
        in sql
    )
    assert (
        "ALTER TABLE product_project_resources ALTER COLUMN created_by DROP NOT NULL"
        in sql
    )
    assert "ADD CONSTRAINT ck_product_project_resources_provenance CHECK" in sql
