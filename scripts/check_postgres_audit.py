from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.audit import AuditEvent  # noqa: E402
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)

EXPECTED_REVISION = "20260813_28"
EXPECTED_POLICIES = {
    ("audit_heads", "audit_heads_tenant_isolation", "ALL"),
    ("audit_events", "audit_events_tenant_select", "SELECT"),
    ("audit_events", "audit_events_tenant_insert", "INSERT"),
}
EXPECTED_TRIGGERS = {
    "trg_audit_events_reject_update_delete",
    "trg_audit_events_reject_truncate",
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


def check_schema(engine) -> tuple[bool, bool]:
    with engine.connect() as connection:
        if connection.dialect.name != "postgresql":
            raise ConfigurationError("PostgreSQL is required for this check")
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        rls_rows = (
            connection.execute(
                text("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n
                      ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = ANY(:table_names)
                    """),
                {"table_names": ["audit_heads", "audit_events"]},
            )
            .mappings()
            .all()
        )
        policies = (
            connection.execute(
                text("""
                    SELECT tablename, policyname, cmd, qual, with_check
                    FROM pg_catalog.pg_policies
                    WHERE schemaname = current_schema()
                      AND tablename = ANY(:table_names)
                    """),
                {"table_names": ["audit_heads", "audit_events"]},
            )
            .mappings()
            .all()
        )
        trigger_names = set(connection.execute(text("""
                    SELECT t.tgname
                    FROM pg_catalog.pg_trigger AS t
                    JOIN pg_catalog.pg_class AS c
                      ON c.oid = t.tgrelid
                    JOIN pg_catalog.pg_namespace AS n
                      ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = 'audit_events'
                      AND NOT t.tgisinternal
                      AND t.tgenabled = 'O'
                    """)).scalars().all())
        role = connection.execute(text("""
                SELECT rolsuper, rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """)).one()

    if revision != EXPECTED_REVISION:
        raise RuntimeError("database revision is not the expected head")
    rls_by_table = {row["relname"]: row for row in rls_rows}
    for table_name in ("audit_heads", "audit_events"):
        rls = rls_by_table.get(table_name)
        if rls is None or not rls["relrowsecurity"] or not rls["relforcerowsecurity"]:
            raise RuntimeError(f"{table_name} RLS is not enabled and forced")

    policy_by_identity = {
        (row["tablename"], row["policyname"], row["cmd"]): row for row in policies
    }
    expected_setting = "current_setting('coifesp.tenant_id'::text, true)"
    for identity in EXPECTED_POLICIES:
        policy = policy_by_identity.get(identity)
        if policy is None:
            raise RuntimeError(f"required audit policy is missing: {identity[1]}")
        expressions = tuple(
            expression
            for expression in (policy["qual"], policy["with_check"])
            if expression is not None
        )
        if not expressions or any(expected_setting not in value for value in expressions):
            raise RuntimeError(f"audit policy is not fail-closed: {identity[1]}")

    if trigger_names != EXPECTED_TRIGGERS:
        raise RuntimeError("immutable audit triggers are missing or unexpectedly changed")

    role_is_privileged = bool(role.rolsuper or role.rolbypassrls)
    print(
        f"AUDIT_SCHEMA_OK revision={revision} rls=enabled forced=yes "
        f"policies={len(EXPECTED_POLICIES)} triggers={len(EXPECTED_TRIGGERS)}"
    )
    print(
        "DATABASE_ROLE "
        f"superuser={'yes' if role.rolsuper else 'no'} "
        f"bypass_rls={'yes' if role.rolbypassrls else 'no'} "
        f"runtime_safe={'no' if role_is_privileged else 'yes'}"
    )
    return bool(role.rolsuper), bool(role.rolbypassrls)


def _expect_immutable_rejection(
    connection,
    statement,
    parameters,
    *,
    allow_rls_zero_rows: bool = False,
) -> str:
    savepoint = connection.begin_nested()
    try:
        result = connection.execute(statement, parameters)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate not in {"42501", "55000"}:
            raise RuntimeError("audit mutation failed for an unexpected reason") from exc
        return "database-rejected"
    else:
        if allow_rls_zero_rows and result.rowcount == 0:
            return "rls-hidden"
        raise RuntimeError("audit mutation unexpectedly succeeded")
    finally:
        if savepoint.is_active:
            savepoint.rollback()


def _set_tenant_context(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
        {"tenant_id": tenant_id},
    )


def check_audit_runtime(engine, settings: Settings) -> bool:
    token = uuid.uuid4().hex
    tenant_a = f"audit-smoke-a-{token[:12]}"
    tenant_b = f"audit-smoke-b-{token[:12]}"
    event_a_id = f"audit-event-a-{token[:20]}"
    event_b_id = f"audit-event-b-{token[:20]}"
    occurred_at = datetime.now(UTC).isoformat()
    event_a = AuditEvent(
        tenant_id=tenant_a,
        event_type="system.audit_smoke",
        actor_id="audit-smoke-check",
        outcome="allowed",
        details={"check": "immutable-chain", "contains_secret": False},
        correlation_id=f"audit-correlation-{token[:20]}",
        event_id=event_a_id,
        occurred_at=occurred_at,
    )
    event_b = AuditEvent(
        tenant_id=tenant_b,
        event_type="system.audit_smoke",
        actor_id="audit-smoke-check",
        outcome="allowed",
        details={"check": "tenant-isolation", "contains_secret": False},
        correlation_id=f"audit-correlation-{token[8:28]}",
        event_id=event_b_id,
        occurred_at=occurred_at,
    )
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings),
    )

    connection = engine.connect()
    transaction = connection.begin()
    try:
        if audit.append_in_transaction(connection, event_a) != event_a_id:
            raise RuntimeError("audit append returned an unexpected event id")
        if audit.append_in_transaction(connection, event_a) != event_a_id:
            raise RuntimeError("idempotent audit append returned an unexpected event id")
        audit.append_in_transaction(connection, event_b)
        if audit.verify_tenant_chain_in_transaction(connection, tenant_a) != 1:
            raise RuntimeError("tenant A audit chain is invalid")
        if audit.verify_tenant_chain_in_transaction(connection, tenant_b) != 1:
            raise RuntimeError("tenant B audit chain is invalid")

        _set_tenant_context(connection, tenant_b)
        cross_tenant_count = connection.execute(
            text("""
                SELECT count(*)
                FROM audit_events
                WHERE tenant_id = :tenant_id
                  AND event_id = :event_id
                """),
            {"tenant_id": tenant_a, "event_id": event_a_id},
        ).scalar_one()
        if cross_tenant_count != 0:
            raise RuntimeError("cross-tenant audit event was visible")

        _set_tenant_context(connection, tenant_a)
        _expect_immutable_rejection(
            connection,
            text("""
                UPDATE audit_events
                SET payload = payload
                WHERE tenant_id = :tenant_id
                  AND event_id = :event_id
            """),
            {"tenant_id": tenant_a, "event_id": event_a_id},
            allow_rls_zero_rows=True,
        )
        _expect_immutable_rejection(
            connection,
            text("""
                DELETE FROM audit_events
                WHERE tenant_id = :tenant_id
                  AND event_id = :event_id
            """),
            {"tenant_id": tenant_a, "event_id": event_a_id},
            allow_rls_zero_rows=True,
        )
        _expect_immutable_rejection(
            connection,
            text("TRUNCATE TABLE audit_events"),
            {},
        )
        if audit.verify_tenant_chain_in_transaction(connection, tenant_a) != 1:
            raise RuntimeError("audit chain changed after rejected mutation")
        print(
            "AUDIT_RUNTIME_OK chain=yes signature=yes idempotency=yes "
            "tenant_isolation=yes immutable_update=yes immutable_delete=yes "
            "immutable_truncate=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()

    with engine.connect() as check_connection:
        for tenant_id, event_id in (
            (tenant_a, event_a_id),
            (tenant_b, event_b_id),
        ):
            _set_tenant_context(check_connection, tenant_id)
            remaining = check_connection.execute(
                text("""
                    SELECT
                        (SELECT count(*) FROM audit_events
                         WHERE tenant_id = :tenant_id AND event_id = :event_id)
                      + (SELECT count(*) FROM audit_heads
                         WHERE tenant_id = :tenant_id)
                    """),
                {"tenant_id": tenant_id, "event_id": event_id},
            ).scalar_one()
            if remaining != 0:
                raise RuntimeError("rolled-back audit smoke data remains")
    print("AUDIT_ROLLBACK_OK test_events_retained=no")
    return True


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
        is_superuser, bypasses_rls = check_schema(engine)
        runtime_ok = check_audit_runtime(engine, settings)
        if is_superuser or bypasses_rls:
            print("POSTGRES_AUDIT_UNSAFE reason=runtime_role_can_bypass_rls secrets=redacted")
            return 2
        if not runtime_ok:
            return 1
        print("POSTGRES_AUDIT_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_AUDIT_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
