import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.execution import SQLAlchemyTaskRepository, TaskStatus  # noqa: E402
from coifesp_harness.execution.repository import TaskExecutionError  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)

EXPECTED_REVISION = "20260813_28"
TABLES = {
    "execution_tasks",
    "execution_task_dependencies",
    "execution_task_events",
}
EXPECTED_TRIGGERS = {
    "trg_execution_task_events_reject_truncate",
    "trg_execution_task_events_reject_update_delete",
}


def load_settings() -> Settings:
    from dotenv import load_dotenv

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        raise ConfigurationError("configuration file is missing: .env")
    load_dotenv(env_path, override=True)
    settings = Settings.from_environment()
    settings.validate()
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required")
    AuditSigningKeyring.from_settings(settings)
    return settings


def expected_policies() -> set[tuple[str, str, str]]:
    result = set()
    for table_name in ("execution_tasks", "execution_task_dependencies"):
        for command in ("SELECT", "INSERT", "UPDATE"):
            result.add(
                (
                    table_name,
                    f"{table_name}_tenant_{command.lower()}",
                    command,
                )
            )
    for command in ("SELECT", "INSERT"):
        result.add(
            (
                "execution_task_events",
                f"execution_task_events_tenant_{command.lower()}",
                command,
            )
        )
    return result


def check_schema(engine) -> tuple[bool, bool]:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        rls_rows = (
            connection.execute(
                text("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = ANY(:tables)
                    """),
                {"tables": sorted(TABLES)},
            )
            .mappings()
            .all()
        )
        policies = {
            (row.tablename, row.policyname, row.cmd)
            for row in connection.execute(
                text("""
                    SELECT tablename, policyname, cmd
                    FROM pg_catalog.pg_policies
                    WHERE schemaname = current_schema()
                      AND tablename = ANY(:tables)
                    """),
                {"tables": sorted(TABLES)},
            )
        }
        triggers = set(connection.execute(text("""
                    SELECT t.tgname
                    FROM pg_catalog.pg_trigger AS t
                    JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = 'execution_task_events'
                      AND NOT t.tgisinternal
                      AND t.tgenabled = 'O'
                    """)).scalars())
        role = connection.execute(text("""
                SELECT rolsuper, rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """)).one()
    protected = {row.relname for row in rls_rows if row.relrowsecurity and row.relforcerowsecurity}
    if revision != EXPECTED_REVISION:
        raise RuntimeError("database revision is not the expected head")
    if protected != TABLES or policies != expected_policies():
        raise RuntimeError("execution RLS schema differs from the reviewed model")
    if triggers != EXPECTED_TRIGGERS:
        raise RuntimeError("execution event immutability triggers are missing")
    print(
        f"EXECUTION_SCHEMA_OK revision={revision} tables={len(TABLES)} "
        f"policies={len(policies)} triggers={len(triggers)} rls=forced"
    )
    print(
        "DATABASE_ROLE "
        f"superuser={'yes' if role.rolsuper else 'no'} "
        f"bypass_rls={'yes' if role.rolbypassrls else 'no'} "
        f"runtime_safe={'no' if role.rolsuper or role.rolbypassrls else 'yes'}"
    )
    return bool(role.rolsuper), bool(role.rolbypassrls)


def set_tenant(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def check_immutable_events(connection, tenant_id: str, task_id: str) -> None:
    savepoint = connection.begin_nested()
    try:
        result = connection.execute(
            text("""
                UPDATE execution_task_events
                SET details = details
                WHERE tenant_id = :tenant_id AND task_id = :task_id
                """),
            {"tenant_id": tenant_id, "task_id": task_id},
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55000":
            raise RuntimeError("execution event mutation failed unexpectedly") from exc
    else:
        if result.rowcount != 0:
            raise RuntimeError("execution event mutation unexpectedly changed rows")
    finally:
        if savepoint.is_active:
            savepoint.rollback()


def check_runtime(engine, settings: Settings) -> None:
    token = uuid.uuid4().hex
    tenant_id = f"exec-{token[:16]}"
    outsider = f"outsider-{token[:16]}"
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings),
    )
    base_repository = SQLAlchemyTaskRepository(engine=engine, audit_log=audit)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        repository = base_repository.using_connection(connection)
        first = repository.enqueue(
            tenant_id=tenant_id,
            actor_id="contributor",
            idempotency_key=f"idem-build-{token[:12]}",
            task_id=f"build-{token[:12]}",
            queue="coding",
            payload={"operation": "build"},
            max_attempts=2,
        )
        duplicate = repository.enqueue(
            tenant_id=tenant_id,
            actor_id="contributor",
            idempotency_key=f"idem-build-{token[:12]}",
            task_id=first.task_id,
            queue="coding",
            payload={"operation": "build"},
            max_attempts=2,
        )
        dependent = repository.enqueue(
            tenant_id=tenant_id,
            actor_id="contributor",
            idempotency_key=f"idem-test-{token[:12]}",
            task_id=f"test-{token[:12]}",
            queue="coding",
            payload={"operation": "test"},
            dependencies=(first.task_id,),
        )
        lease = repository.claim_next(
            tenant_id=tenant_id,
            queue="coding",
            worker_id="worker-1",
            lease_seconds=30,
        )
        if lease is None or lease.task.task_id != first.task_id:
            raise RuntimeError("DAG scheduler did not lease the ready predecessor")
        if (
            repository.claim_next(
                tenant_id=tenant_id,
                queue="coding",
                worker_id="worker-2",
            )
            is not None
        ):
            raise RuntimeError("scheduler leased a task with incomplete dependencies")
        try:
            repository.start(
                tenant_id=tenant_id,
                task_id=first.task_id,
                worker_id="worker-1",
                lease_token="stale-token",
            )
        except TaskExecutionError:
            pass
        else:
            raise RuntimeError("stale fencing token was accepted")
        repository.start(
            tenant_id=tenant_id,
            task_id=first.task_id,
            worker_id="worker-1",
            lease_token=lease.lease_token,
        )
        repository.succeed(
            tenant_id=tenant_id,
            task_id=first.task_id,
            worker_id="worker-1",
            lease_token=lease.lease_token,
            result={"artifact_ref": "artifact://smoke/build"},
        )
        dependent_lease = repository.claim_next(
            tenant_id=tenant_id,
            queue="coding",
            worker_id="worker-2",
            lease_seconds=30,
        )
        if dependent_lease is None or dependent_lease.task.task_id != dependent.task_id:
            raise RuntimeError("completed dependency did not release its successor")
        connection.execute(
            text("""
                UPDATE execution_tasks
                SET lease_expires_at = :expired
                WHERE tenant_id = :tenant_id AND task_id = :task_id
                """),
            {
                "expired": datetime.now(UTC) - timedelta(seconds=1),
                "tenant_id": tenant_id,
                "task_id": dependent.task_id,
            },
        )
        if repository.recover_expired(tenant_id=tenant_id) != 1:
            raise RuntimeError("expired lease was not recovered")
        recovered = repository.claim_next(
            tenant_id=tenant_id,
            queue="coding",
            worker_id="worker-3",
        )
        if recovered is None or recovered.lease_token == dependent_lease.lease_token:
            raise RuntimeError("recovered task did not receive a new fencing token")
        repository.request_cancel(
            tenant_id=tenant_id,
            task_id=dependent.task_id,
            actor_id="contributor",
        )
        repository.acknowledge_cancel(
            tenant_id=tenant_id,
            task_id=dependent.task_id,
            worker_id="worker-3",
            lease_token=recovered.lease_token,
        )

        set_tenant(connection, outsider)
        hidden = connection.execute(
            text("SELECT count(*) FROM execution_tasks WHERE task_id = :task_id"),
            {"task_id": first.task_id},
        ).scalar_one()
        if hidden != 0:
            raise RuntimeError("execution task crossed the tenant RLS boundary")
        set_tenant(connection, tenant_id)
        event_count = connection.execute(
            text("SELECT count(*) FROM execution_task_events " "WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()
        if duplicate.request_digest != first.request_digest or event_count != 10:
            raise RuntimeError("execution idempotency or event journal invariant failed")
        if audit.verify_tenant_chain_in_transaction(connection, tenant_id) != event_count:
            raise RuntimeError("execution events and signed audit chain diverged")
        set_tenant(connection, tenant_id)
        check_immutable_events(connection, tenant_id, first.task_id)
        print(
            "EXECUTION_RUNTIME_OK dag=yes skip_locked_model=yes fencing_token=yes "
            "lease_recovery=yes cancellation=yes idempotency=yes tenant_isolation=yes "
            "atomic_audit=yes immutable_events=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()

    with engine.connect() as check_connection:
        set_tenant(check_connection, tenant_id)
        remaining = check_connection.execute(
            text("SELECT count(*) FROM execution_tasks WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()
    if remaining != 0 or audit.verify_tenant_chain(tenant_id) != 0:
        raise RuntimeError("rolled-back execution smoke data remains")
    print("EXECUTION_ROLLBACK_OK test_records_retained=no")


def main() -> int:
    engine = None
    try:
        settings = load_settings()
        assert settings.database_url is not None
        engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        superuser, bypass_rls = check_schema(engine)
        check_runtime(engine, settings)
        if superuser or bypass_rls:
            print("POSTGRES_EXECUTION_UNSAFE reason=runtime_role_can_bypass_rls secrets=redacted")
            return 2
        print("POSTGRES_EXECUTION_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_EXECUTION_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
