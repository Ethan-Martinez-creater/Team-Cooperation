import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.tool_jobs import (  # noqa: E402
    SQLAlchemyToolJobRepository,
    ToolJobError,
    ToolJobKeyring,
    ToolJobStatus,
)

EXPECTED_REVISION = "20260813_28"
TABLES = {"tool_jobs", "tool_job_events"}
TRIGGERS = {
    "trg_tool_job_events_reject_truncate",
    "trg_tool_job_events_reject_update_delete",
}


def load_settings() -> Settings:
    from dotenv import load_dotenv

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        raise ConfigurationError("configuration file is missing: .env")
    load_dotenv(env_path, override=True)
    settings = Settings.from_environment()
    settings.validate(require_memory=True)
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required")
    return settings


def set_tenant(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant": tenant_id}
    )


def check_schema(engine) -> tuple[bool, bool]:
    expected_policies = {
        ("tool_jobs", "tool_jobs_tenant_select", "SELECT"),
        ("tool_jobs", "tool_jobs_tenant_insert", "INSERT"),
        ("tool_jobs", "tool_jobs_tenant_update", "UPDATE"),
        ("tool_job_events", "tool_job_events_tenant_select", "SELECT"),
        ("tool_job_events", "tool_job_events_tenant_insert", "INSERT"),
    }
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        protected = set(
            connection.execute(
                text("""
                SELECT c.relname FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema() AND c.relname=ANY(:tables)
                  AND c.relrowsecurity AND c.relforcerowsecurity
            """),
                {"tables": sorted(TABLES)},
            ).scalars()
        )
        policies = {
            (r.tablename, r.policyname, r.cmd)
            for r in connection.execute(
                text("""
                SELECT tablename,policyname,cmd FROM pg_catalog.pg_policies
                WHERE schemaname=current_schema() AND tablename=ANY(:tables)
            """),
                {"tables": sorted(TABLES)},
            )
        }
        triggers = set(connection.execute(text("""
            SELECT t.tgname FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname='tool_job_events'
              AND NOT t.tgisinternal AND t.tgenabled='O'
        """)).scalars())
        role = connection.execute(text("""
            SELECT rolsuper,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user
        """)).one()
    if revision != EXPECTED_REVISION or protected != TABLES:
        raise RuntimeError("tool job schema revision or forced RLS is invalid")
    if policies != expected_policies or triggers != TRIGGERS:
        raise RuntimeError("tool job policies or immutability triggers are invalid")
    print(f"TOOL_JOB_SCHEMA_OK revision={revision} tables=2 policies=5 " f"triggers=2 rls=forced")
    return bool(role.rolsuper), bool(role.rolbypassrls)


def check_runtime(engine, settings: Settings) -> None:
    token = uuid.uuid4().hex
    tenant = f"tool-{token[:16]}"
    outsider = f"outside-{token[:16]}"
    job_id = f"job-{token[:16]}"
    secret = f"confidential-{token}"
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring.from_settings(settings))
    base = SQLAlchemyToolJobRepository(
        engine=engine, keyring=ToolJobKeyring.from_settings(settings), audit_log=audit
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        repository = base.using_connection(connection)
        created = repository.enqueue(
            tenant_id=tenant,
            actor_id="lead",
            job_id=job_id,
            run_id=f"run-{token[:16]}",
            call_id="call-1",
            tool_name="office.send_message",
            idempotency_key=f"idem-{token[:16]}",
            arguments={"message": secret},
            max_attempts=2,
        )
        duplicate = repository.enqueue(
            tenant_id=tenant,
            actor_id="lead",
            job_id=job_id,
            run_id=f"run-{token[:16]}",
            call_id="call-1",
            tool_name="office.send_message",
            idempotency_key=f"idem-{token[:16]}",
            arguments={"message": secret},
            max_attempts=2,
        )
        if duplicate.request_digest != created.request_digest:
            raise RuntimeError("idempotent enqueue returned a different request")
        raw = connection.execute(
            text("""
            SELECT arguments_ciphertext FROM tool_jobs
            WHERE tenant_id=:tenant AND job_id=:job
        """),
            {"tenant": tenant, "job": job_id},
        ).scalar_one()
        if secret.encode() in bytes(raw):
            raise RuntimeError("tool arguments were stored in plaintext")
        old = repository.claim_next(tenant_id=tenant, worker_id="worker-1", lease_seconds=30)
        if old is None:
            raise RuntimeError("queued tool job was not claimable")
        repository.start(
            tenant_id=tenant, job_id=job_id, worker_id="worker-1", lease_token=old.lease_token
        )
        connection.execute(
            text("""
            UPDATE tool_jobs SET lease_expires_at=:expired
            WHERE tenant_id=:tenant AND job_id=:job
        """),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "tenant": tenant, "job": job_id},
        )
        if (
            repository.recover_expired(
                tenant_id=tenant, actor_id="tool-reaper", retry_delay_seconds=1
            )
            != 1
        ):
            raise RuntimeError("expired tool lease was not recovered")
        connection.execute(
            text("""
            UPDATE tool_jobs SET available_at=:ready
            WHERE tenant_id=:tenant AND job_id=:job
        """),
            {"ready": datetime.now(UTC) - timedelta(seconds=1), "tenant": tenant, "job": job_id},
        )
        lease = repository.claim_next(tenant_id=tenant, worker_id="worker-2", lease_seconds=30)
        if lease is None or lease.lease_token == old.lease_token:
            raise RuntimeError("recovered tool job lacks a fresh fencing token")
        try:
            repository.start(
                tenant_id=tenant, job_id=job_id, worker_id="worker-1", lease_token=old.lease_token
            )
        except ToolJobError:
            pass
        else:
            raise RuntimeError("stale tool worker was not fenced")
        repository.start(
            tenant_id=tenant, job_id=job_id, worker_id="worker-2", lease_token=lease.lease_token
        )
        repository.succeed(
            tenant_id=tenant,
            job_id=job_id,
            worker_id="worker-2",
            lease_token=lease.lease_token,
            result={"provider_id": secret},
        )
        complete = repository.get(tenant_id=tenant, job_id=job_id, include_payloads=True)
        if complete.status is not ToolJobStatus.SUCCEEDED or complete.result != {
            "provider_id": secret
        }:
            raise RuntimeError("encrypted tool result did not round-trip")
        set_tenant(connection, outsider)
        if (
            connection.execute(
                text("SELECT count(*) FROM tool_jobs WHERE job_id=:job"), {"job": job_id}
            ).scalar_one()
            != 0
        ):
            raise RuntimeError("tool job crossed the tenant RLS boundary")
        set_tenant(connection, tenant)
        event_count = connection.execute(
            text("""
            SELECT count(*) FROM tool_job_events WHERE tenant_id=:tenant AND job_id=:job
        """),
            {"tenant": tenant, "job": job_id},
        ).scalar_one()
        if event_count != 7 or audit.verify_tenant_chain_in_transaction(connection, tenant) != 7:
            raise RuntimeError("tool event journal and signed audit chain diverged")
        savepoint = connection.begin_nested()
        try:
            mutation = connection.execute(
                text("""
                UPDATE tool_job_events SET actor_id=actor_id
                WHERE tenant_id=:tenant AND job_id=:job
            """),
                {"tenant": tenant, "job": job_id},
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55000":
                raise RuntimeError("event immutability failed unexpectedly") from exc
        else:
            # The runtime role has no UPDATE RLS policy, so PostgreSQL normally
            # hides all target rows before reaching the defense-in-depth
            # trigger.  Either zero affected rows or SQLSTATE 55000 is a valid
            # fail-closed outcome.
            if mutation.rowcount != 0:
                raise RuntimeError("tool event journal was mutable")
        finally:
            if savepoint.is_active:
                savepoint.rollback()
        print(
            "TOOL_JOB_RUNTIME_OK encryption=yes idempotency=yes lease_recovery=yes "
            "fencing=yes tenant_isolation=yes atomic_audit=yes immutable_events=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
    if audit.verify_tenant_chain(tenant) != 0:
        raise RuntimeError("rolled-back tool job smoke audit remains")
    print("TOOL_JOB_ROLLBACK_OK test_records_retained=no")


def main() -> int:
    engine = None
    try:
        settings = load_settings()
        engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
        superuser, bypass = check_schema(engine)
        check_runtime(engine, settings)
        if superuser or bypass:
            print("POSTGRES_TOOL_JOBS_UNSAFE reason=runtime_role_can_bypass_rls secrets=redacted")
            return 2
        print("POSTGRES_TOOL_JOBS_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            f"POSTGRES_TOOL_JOBS_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
