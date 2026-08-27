from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.audit import InMemoryAuditSink  # noqa: E402
from coifesp_harness.collaboration.governance import GovernanceBoard  # noqa: E402
from coifesp_harness.collaboration.governance_models import (  # noqa: E402
    BoardMember,
    CollaborationRole,
)
from coifesp_harness.collaboration.repository import (  # noqa: E402
    SQLAlchemyGovernanceRepository,
)
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.idempotency import ClaimStatus  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.security import Classification  # noqa: E402

EXPECTED_REVISION = "20260813_28"
VISIBLE_TABLES = (
    "governance_members",
    "governance_plans",
    "governance_plan_deliverables",
    "governance_plan_approvers",
    "governance_discussion_items",
    "governance_assignments",
    "governance_assignment_dependencies",
    "governance_assignment_artifacts",
)
ALL_TABLES = {
    "collaboration_inbox",
    "governance_programs",
    "governance_commands",
    *VISIBLE_TABLES,
    "governance_events",
    "governance_outbox",
}
EXPECTED_TRIGGERS = {
    "trg_governance_events_reject_update_delete",
    "trg_governance_events_reject_truncate",
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
    result = {
        (
            "governance_programs",
            f"governance_programs_tenant_{command.lower()}",
            command,
        )
        for command in ("SELECT", "INSERT", "UPDATE")
    }
    for table_name in VISIBLE_TABLES:
        result.update(
            {
                (
                    table_name,
                    f"{table_name}_tenant_{command.lower()}",
                    command,
                )
                for command in ("SELECT", "INSERT", "UPDATE")
            }
        )
    result.update(
        {
            (
                "governance_commands",
                "governance_commands_tenant_select",
                "SELECT",
            ),
            (
                "governance_commands",
                "governance_commands_tenant_insert",
                "INSERT",
            ),
            (
                "governance_commands",
                "governance_commands_tenant_update",
                "UPDATE",
            ),
            (
                "governance_events",
                "governance_events_tenant_select",
                "SELECT",
            ),
            (
                "governance_events",
                "governance_events_tenant_insert",
                "INSERT",
            ),
            (
                "governance_outbox",
                "governance_outbox_producer_select",
                "SELECT",
            ),
            (
                "governance_outbox",
                "governance_outbox_producer_insert",
                "INSERT",
            ),
            (
                "governance_outbox",
                "governance_outbox_producer_update",
                "UPDATE",
            ),
        }
    )
    for command in ("SELECT", "INSERT", "UPDATE"):
        result.add(
            (
                "collaboration_inbox",
                f"collaboration_inbox_tenant_{command.lower()}",
                command,
            )
        )
    return result


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
                {"table_names": sorted(ALL_TABLES)},
            )
            .mappings()
            .all()
        )
        policies = (
            connection.execute(
                text("""
                    SELECT tablename, policyname, cmd
                    FROM pg_catalog.pg_policies
                    WHERE schemaname = current_schema()
                      AND tablename = ANY(:table_names)
                    """),
                {"table_names": sorted(ALL_TABLES)},
            )
            .mappings()
            .all()
        )
        triggers = set(connection.execute(text("""
                    SELECT t.tgname
                    FROM pg_catalog.pg_trigger AS t
                    JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = 'governance_events'
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
    protected = {
        row["relname"] for row in rls_rows if row["relrowsecurity"] and row["relforcerowsecurity"]
    }
    if protected != ALL_TABLES:
        raise RuntimeError("governance tables are not all protected by forced RLS")
    actual_policies = {(row["tablename"], row["policyname"], row["cmd"]) for row in policies}
    if actual_policies != expected_policies():
        raise RuntimeError("governance RLS policy set differs from the reviewed model")
    if triggers != EXPECTED_TRIGGERS:
        raise RuntimeError("governance immutability triggers are missing or changed")

    role_is_privileged = bool(role.rolsuper or role.rolbypassrls)
    print(
        f"GOVERNANCE_SCHEMA_OK revision={revision} tables={len(ALL_TABLES)} "
        f"policies={len(actual_policies)} triggers={len(triggers)} rls=forced"
    )
    print(
        "DATABASE_ROLE "
        f"superuser={'yes' if role.rolsuper else 'no'} "
        f"bypass_rls={'yes' if role.rolbypassrls else 'no'} "
        f"runtime_safe={'no' if role_is_privileged else 'yes'}"
    )
    return bool(role.rolsuper), bool(role.rolbypassrls)


def set_tenant(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config(" "'coifesp.tenant_id', :tenant_id, true" ")"),
        {"tenant_id": tenant_id},
    )


def check_immutable_event_log(connection, program_id: str) -> None:
    savepoint = connection.begin_nested()
    try:
        result = connection.execute(
            text("""
                UPDATE governance_events
                SET payload = payload
                WHERE program_id = :program_id
                """),
            {"program_id": program_id},
        )
        if result.rowcount != 0:
            raise RuntimeError("governance event update unexpectedly changed rows")
    finally:
        if savepoint.is_active:
            savepoint.rollback()

    savepoint = connection.begin_nested()
    try:
        connection.execute(text("TRUNCATE TABLE governance_events"))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"0A000", "42501", "55000"}:
            raise RuntimeError("governance truncate failed unexpectedly") from exc
    else:
        raise RuntimeError("governance event truncate unexpectedly succeeded")
    finally:
        if savepoint.is_active:
            savepoint.rollback()


def check_runtime(engine, settings: Settings) -> bool:
    token = uuid.uuid4().hex
    program_id = f"governance-smoke-{token[:16]}"
    tenant_a = f"gov-a-{token[:12]}"
    tenant_b = f"gov-b-{token[:12]}"
    tenant_c = f"gov-c-{token[:12]}"
    audit_log = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings),
    )
    repository = SQLAlchemyGovernanceRepository(
        engine=engine,
        audit_log=audit_log,
    )
    sink = InMemoryAuditSink()
    board = GovernanceBoard(
        program_id=program_id,
        owner_tenant_id=tenant_a,
        title="Governance PostgreSQL smoke",
        objective="Verify atomic cross-team governance persistence",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"smoke"}),
        audit=sink,
    )
    board.add_member(BoardMember("lead-a", tenant_a, CollaborationRole.LEAD))
    board.add_member(
        BoardMember("contributor-b", tenant_b, CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    board.add_member(
        BoardMember("observer-c", tenant_c, CollaborationRole.OBSERVER),
        actor_id="lead-a",
    )

    connection = engine.connect()
    transaction = connection.begin()
    try:
        create_digest = hashlib.sha256(
            f"program.create\n{program_id}\n{tenant_a}".encode("utf-8")
        ).hexdigest()
        create_claim = repository.claim_command_in_transaction(
            connection,
            tenant_id=tenant_a,
            idempotency_key=f"create-{token[:20]}",
            program_id=program_id,
            command_type="program.create",
            request_digest=create_digest,
        )
        if create_claim.status is not ClaimStatus.CLAIMED:
            raise RuntimeError("new governance create command was not claimed")
        repository.create_in_transaction(
            connection,
            board=board,
            actor_id="lead-a",
            events=tuple(sink.events),
        )
        repository.complete_command_in_transaction(
            connection,
            tenant_id=tenant_a,
            idempotency_key=f"create-{token[:20]}",
            result_version=1,
        )
        duplicate_claim = repository.claim_command_in_transaction(
            connection,
            tenant_id=tenant_a,
            idempotency_key=f"create-{token[:20]}",
            program_id=program_id,
            command_type="program.create",
            request_digest=create_digest,
        )
        if (
            duplicate_claim.status is not ClaimStatus.DUPLICATE
            or duplicate_claim.result_version != 1
        ):
            raise RuntimeError("governance command retry was not suppressed")
        sink.events.clear()
        plan = board.create_plan(
            actor_id="lead-a",
            plan_id=f"plan-{token[:16]}",
            version=1,
            title="Need-to-know contract",
            objective="Share the contract with the implementing team only",
            deliverables=("versioned contract", "verification report"),
            required_approvers=frozenset({"lead-a", "contributor-b"}),
            visible_to_tenants=frozenset({tenant_a, tenant_b}),
        )
        board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
        plan_digest = hashlib.sha256(
            f"plan.create\n{program_id}\n{plan.plan_id}".encode("utf-8")
        ).hexdigest()
        plan_claim = repository.claim_command_in_transaction(
            connection,
            tenant_id=tenant_a,
            idempotency_key=f"plan-{token[:20]}",
            program_id=program_id,
            command_type="plan.create_and_open",
            request_digest=plan_digest,
        )
        if plan_claim.status is not ClaimStatus.CLAIMED:
            raise RuntimeError("new governance plan command was not claimed")
        new_version = repository.save_in_transaction(
            connection,
            board=board,
            expected_version=1,
            actor_id="lead-a",
            events=tuple(sink.events),
        )
        repository.complete_command_in_transaction(
            connection,
            tenant_id=tenant_a,
            idempotency_key=f"plan-{token[:20]}",
            result_version=new_version,
        )

        set_tenant(connection, tenant_b)
        team_b_plans = connection.execute(
            text("SELECT count(*) FROM governance_plans WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        set_tenant(connection, tenant_c)
        team_c_programs = connection.execute(
            text("SELECT count(*) FROM governance_programs WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        team_c_plans = connection.execute(
            text("SELECT count(*) FROM governance_plans WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        team_c_events = connection.execute(
            text("SELECT count(*) FROM governance_events WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        team_c_outbox = connection.execute(
            text("SELECT count(*) FROM governance_outbox WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        set_tenant(connection, f"outsider-{token[:12]}")
        outsider_programs = connection.execute(
            text("SELECT count(*) FROM governance_programs WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        if (
            team_b_plans != 1
            or team_c_programs != 1
            or team_c_plans != 0
            or team_c_events != 3
            or team_c_outbox != 0
            or outsider_programs != 0
        ):
            raise RuntimeError("governance tenant visibility invariant failed")

        set_tenant(connection, tenant_a)
        event_count = connection.execute(
            text("SELECT count(*) FROM governance_events WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        outbox_count = connection.execute(
            text("SELECT count(*) FROM governance_outbox WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
        if event_count != 5 or outbox_count != 13:
            raise RuntimeError("owner-visible governance event/outbox count is invalid")
        if audit_log.verify_tenant_chain_in_transaction(connection, tenant_a) != 5:
            raise RuntimeError("atomic governance audit chain is invalid")
        set_tenant(connection, tenant_a)
        check_immutable_event_log(connection, program_id)
        print(
            "GOVERNANCE_RUNTIME_OK normalized_state=yes optimistic_version=yes "
            "command_idempotency=yes explicit_visibility=yes cross_tenant_hidden=yes event_log=yes "
            "outbox=yes atomic_audit=yes immutable_events=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()

    with engine.connect() as check_connection:
        set_tenant(check_connection, tenant_a)
        remaining = check_connection.execute(
            text("SELECT count(*) FROM governance_programs WHERE program_id = :program_id"),
            {"program_id": program_id},
        ).scalar_one()
    if remaining != 0 or audit_log.verify_tenant_chain(tenant_a) != 0:
        raise RuntimeError("rolled-back governance smoke data remains")
    print("GOVERNANCE_ROLLBACK_OK test_records_retained=no")
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
        runtime_ok = check_runtime(engine, settings)
        if is_superuser or bypasses_rls:
            print("POSTGRES_GOVERNANCE_UNSAFE reason=runtime_role_can_bypass_rls secrets=redacted")
            return 2
        if not runtime_ok:
            return 1
        print("POSTGRES_GOVERNANCE_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_GOVERNANCE_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
