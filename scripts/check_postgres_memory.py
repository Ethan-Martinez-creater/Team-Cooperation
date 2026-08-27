from __future__ import annotations

import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.audit import InMemoryAuditSink  # noqa: E402
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.memory import (  # noqa: E402
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryScope,
    MemoryService,
    MemorySource,
    MemoryWriteRequest,
    SQLAlchemyMemoryRepository,
    SourceType,
    TenantMemoryKeyring,
    TrustLevel,
)
from coifesp_harness.security import (  # noqa: E402
    Classification,
    PolicyEngine,
    Principal,
    ResourceLabel,
)

EXPECTED_REVISION = "20260813_28"
EXPECTED_POLICIES = {
    "memory_records": "memory_records_tenant_isolation",
    "memory_idempotency_claims": "memory_claims_tenant_isolation",
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
                {"table_names": list(EXPECTED_POLICIES)},
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
                {"table_names": list(EXPECTED_POLICIES)},
            )
            .mappings()
            .all()
        )
        role = connection.execute(text("""
                SELECT rolsuper, rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """)).one()

    if revision != EXPECTED_REVISION:
        raise RuntimeError("database revision is not the expected head")
    rls_by_table = {row["relname"]: row for row in rls_rows}
    policy_by_table = {row["tablename"]: row for row in policies}
    expected_setting = "current_setting('coifesp.tenant_id'::text, true)"
    for table_name, policy_name in EXPECTED_POLICIES.items():
        rls = rls_by_table.get(table_name)
        if rls is None or not rls["relrowsecurity"] or not rls["relforcerowsecurity"]:
            raise RuntimeError(f"{table_name} RLS is not enabled and forced")
        policy = policy_by_table.get(table_name)
        if policy is None or policy["policyname"] != policy_name:
            raise RuntimeError(f"{table_name} tenant isolation policy is missing")
        if (
            policy["cmd"] != "ALL"
            or expected_setting not in policy["qual"]
            or expected_setting not in policy["with_check"]
        ):
            raise RuntimeError(f"{table_name} tenant isolation policy is not fail-closed")

    role_is_privileged = bool(role.rolsuper or role.rolbypassrls)
    print(
        f"SCHEMA_OK revision={revision} rls=enabled forced=yes "
        f"policies={len(EXPECTED_POLICIES)}"
    )
    print(
        "DATABASE_ROLE "
        f"superuser={'yes' if role.rolsuper else 'no'} "
        f"bypass_rls={'yes' if role.rolbypassrls else 'no'} "
        f"runtime_safe={'no' if role_is_privileged else 'yes'}"
    )
    return bool(role.rolsuper), bool(role.rolbypassrls)


def check_crud(engine, settings: Settings) -> bool:
    token = uuid.uuid4().hex
    tenant_id = f"smoke-{token[:12]}"
    other_tenant_id = f"other-{token[:12]}"
    memory_id = f"memory-{token[:20]}"
    content = f"PostgreSQL encrypted Memory smoke test {token[:8]}."
    principal = Principal(
        principal_id="smoke-actor",
        tenant_id=tenant_id,
        roles=frozenset(),
        clearance=Classification.INTERNAL,
        compartments=frozenset(),
    )
    request = MemoryWriteRequest(
        memory_id=memory_id,
        idempotency_key=f"idem-{token[:20]}",
        correlation_id=f"corr-{token[:20]}",
        principal=principal,
        scope=MemoryScope.USER_PRIVATE,
        kind=MemoryKind.FACT,
        content=content,
        label=ResourceLabel(
            owner_tenant_id=tenant_id,
            classification=Classification.INTERNAL,
            compartments=frozenset(),
            resource_id=f"memory:{memory_id}",
        ),
        source=MemorySource(
            source_type=SourceType.SYSTEM,
            source_id=f"source-{token[:20]}",
            source_uri=None,
            trust_level=TrustLevel.AUTHORITATIVE,
        ),
        owner_principal_id=principal.principal_id,
    )
    repository = SQLAlchemyMemoryRepository(engine)
    service = MemoryService(
        repository=repository,
        keyring=TenantMemoryKeyring.from_settings(settings),
        policy=PolicyEngine(),
        admission=MemoryAdmissionPolicy(),
        audit=InMemoryAuditSink(),
    )

    cleanup_ok = False
    try:
        service.write(request)
        duplicate = service.write(request)
        if not duplicate.duplicate:
            raise RuntimeError("duplicate Memory write was not suppressed")
        view = service.read(principal=principal, memory_id=memory_id)
        if view.content != content:
            raise RuntimeError("decrypted Memory content did not round-trip")
        if repository.get(other_tenant_id, memory_id) is not None:
            raise RuntimeError("cross-tenant repository lookup exposed Memory")

        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
                {"tenant_id": tenant_id},
            )
            stored = connection.execute(
                text("""
                    SELECT
                        r.ciphertext,
                        (
                            SELECT count(*)
                            FROM memory_idempotency_claims AS c
                            WHERE c.tenant_id = :tenant_id
                              AND c.memory_id = :memory_id
                        ) AS claim_count
                    FROM memory_records AS r
                    WHERE r.tenant_id = :tenant_id
                      AND r.memory_id = :memory_id
                    """),
                {"tenant_id": tenant_id, "memory_id": memory_id},
            ).one()
        if content.encode("utf-8") in bytes(stored.ciphertext):
            raise RuntimeError("plaintext was found in persisted Memory ciphertext")
        if stored.claim_count != 1:
            raise RuntimeError("Memory record and idempotency claim are inconsistent")
        print(
            "MEMORY_CRUD_OK encrypted_at_rest=yes "
            "round_trip=yes app_tenant_filter=yes atomic_idempotency=yes"
        )
        return True
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
                {"tenant_id": tenant_id},
            )
            result = connection.execute(
                text("""
                    DELETE FROM memory_records
                    WHERE tenant_id = :tenant_id AND memory_id = :memory_id
                    """),
                {"tenant_id": tenant_id, "memory_id": memory_id},
            )
            remaining_claims = connection.execute(
                text("""
                    SELECT count(*)
                    FROM memory_idempotency_claims
                    WHERE tenant_id = :tenant_id AND memory_id = :memory_id
                    """),
                {"tenant_id": tenant_id, "memory_id": memory_id},
            ).scalar_one()
            cleanup_ok = result.rowcount in {0, 1} and remaining_claims == 0
        print(f"SMOKE_CLEANUP_OK exact_record={'yes' if cleanup_ok else 'no'}")


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
        crud_ok = check_crud(engine, settings)
        if is_superuser or bypasses_rls:
            print("POSTGRES_MEMORY_UNSAFE reason=runtime_role_can_bypass_rls " "secrets=redacted")
            return 2
        if not crud_ok:
            return 1
        print("POSTGRES_MEMORY_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_MEMORY_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
