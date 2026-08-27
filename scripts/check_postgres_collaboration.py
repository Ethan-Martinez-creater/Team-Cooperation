import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, insert, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.collaboration import (  # noqa: E402
    DurableCollaborationTransport,
    SignedEnvelopeCodec,
)
from coifesp_harness.collaboration.repository import (  # noqa: E402
    GOVERNANCE_EVENTS,
    GOVERNANCE_OUTBOX,
    GOVERNANCE_PROGRAMS,
)
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.errors import IntegrityError  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.security import Principal  # noqa: E402

EXPECTED_REVISION = "20260813_28"
TABLES = {"governance_outbox", "collaboration_inbox"}
EXPECTED_POLICIES = {
    ("governance_outbox", "governance_outbox_producer_select", "SELECT"),
    ("governance_outbox", "governance_outbox_producer_insert", "INSERT"),
    ("governance_outbox", "governance_outbox_producer_update", "UPDATE"),
    ("collaboration_inbox", "collaboration_inbox_tenant_select", "SELECT"),
    ("collaboration_inbox", "collaboration_inbox_tenant_insert", "INSERT"),
    ("collaboration_inbox", "collaboration_inbox_tenant_update", "UPDATE"),
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
    SignedEnvelopeCodec.from_settings(settings)
    AuditSigningKeyring.from_settings(settings)
    return settings


def set_tenant(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def check_schema(engine) -> tuple[bool, bool]:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        protected = {
            row.relname
            for row in connection.execute(
                text("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = ANY(:tables)
                    """),
                {"tables": sorted(TABLES)},
            )
            if row.relrowsecurity and row.relforcerowsecurity
        }
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
        role = connection.execute(text("""
                SELECT rolsuper, rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname = current_user
                """)).one()
    if revision != EXPECTED_REVISION:
        raise RuntimeError("database revision is not the expected head")
    if protected != TABLES or policies != EXPECTED_POLICIES:
        raise RuntimeError("collaboration transport RLS differs from the reviewed model")
    print(
        f"COLLABORATION_SCHEMA_OK revision={revision} tables={len(TABLES)} "
        f"policies={len(policies)} rls=forced"
    )
    print(
        "DATABASE_ROLE "
        f"superuser={'yes' if role.rolsuper else 'no'} "
        f"bypass_rls={'yes' if role.rolbypassrls else 'no'} "
        f"runtime_safe={'no' if role.rolsuper or role.rolbypassrls else 'yes'}"
    )
    return bool(role.rolsuper), bool(role.rolbypassrls)


def check_runtime(engine, settings: Settings) -> None:
    token = uuid.uuid4().hex
    tenant_a = f"transport-a-{token[:10]}"
    tenant_b = f"transport-b-{token[:10]}"
    outsider = f"transport-x-{token[:10]}"
    program_id = f"program-{token[:16]}"
    message_id = f"message-{token[:16]}"
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings),
    )
    transport = DurableCollaborationTransport(
        engine=engine,
        audit_log=audit,
        codec=SignedEnvelopeCodec.from_settings(settings),
    )
    relay = Principal(
        "relay-a",
        tenant_a,
        roles=frozenset({"collaboration_relay"}),
        is_service=True,
    )
    consumer = Principal(
        "consumer-b",
        tenant_b,
        roles=frozenset({"collaboration_consumer"}),
        is_service=True,
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        set_tenant(connection, tenant_a)
        now = datetime.now(UTC)
        connection.execute(
            insert(GOVERNANCE_PROGRAMS).values(
                program_id=program_id,
                owner_tenant_id=tenant_a,
                title="Transport smoke",
                objective="Verify cross-team reliable delivery",
                classification=2,
                compartments=["transport-smoke"],
                participant_tenant_ids=[tenant_a, tenant_b],
                aggregate_version=1,
                last_event_sequence=1,
                created_by="lead-a",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(GOVERNANCE_EVENTS).values(
                program_id=program_id,
                sequence=1,
                event_id=f"event-{token[:16]}",
                event_type="governance.assignment.proposed",
                actor_id="lead-a",
                actor_tenant_id=tenant_a,
                subject_id=f"assignment-{token[:12]}",
                payload={
                    "subject_id": f"assignment-{token[:12]}",
                    "credential": "API_KEY=must-not-cross-boundary",
                },
                visible_to_tenants=[tenant_a, tenant_b],
                occurred_at=now,
                audit_event_id=f"event-{token[:16]}",
            )
        )
        connection.execute(
            insert(GOVERNANCE_OUTBOX).values(
                message_id=message_id,
                program_id=program_id,
                event_sequence=1,
                producer_tenant_id=tenant_a,
                recipient_tenant_id=tenant_b,
                status="pending",
                attempt_count=0,
                max_attempts=3,
                available_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                envelope=None,
                envelope_digest=None,
                last_error_code=None,
                published_at=None,
                created_at=now,
            )
        )
        bound = transport.using_connection(connection)
        first_lease = bound.claim_outbound(relay=relay, lease_seconds=30)
        if first_lease is None:
            raise RuntimeError("outbox message was not leased")
        set_tenant(connection, tenant_a)
        connection.execute(
            text("""
                UPDATE governance_outbox
                SET lease_expires_at = :expired
                WHERE message_id = :message_id
                """),
            {
                "expired": datetime.now(UTC) - timedelta(seconds=1),
                "message_id": message_id,
            },
        )
        recovered = bound.claim_outbound(relay=relay, lease_seconds=30)
        if recovered is None or recovered.lease_token == first_lease.lease_token:
            raise RuntimeError("expired outbox lease was not fenced and recovered")
        envelope = bound.build_envelope(recovered)
        if "must-not-cross-boundary" in envelope.content:
            raise RuntimeError("secret redaction failed before cross-team delivery")
        try:
            bound.mark_published(
                relay=relay,
                message_id=message_id,
                lease_token=first_lease.lease_token,
                envelope=envelope,
            )
        except IntegrityError:
            pass
        else:
            raise RuntimeError("stale outbox fencing token was accepted")
        bound.mark_published(
            relay=relay,
            message_id=message_id,
            lease_token=recovered.lease_token,
            envelope=envelope,
        )
        receipt = bound.receive(consumer=consumer, envelope=envelope)
        duplicate = bound.receive(consumer=consumer, envelope=envelope)
        if receipt.duplicate or not duplicate.duplicate:
            raise RuntimeError("inbox replay suppression failed")
        inbox_lease = bound.claim_inbound(
            consumer=consumer,
            handler_key="governance-projector-v1",
        )
        if inbox_lease is None:
            raise RuntimeError("inbox message was not leased")
        bound.complete_inbound(
            consumer=consumer,
            message_id=message_id,
            lease_token=inbox_lease.lease_token,
            result_digest="a" * 64,
        )

        set_tenant(connection, tenant_a)
        outbox_status = connection.execute(
            text("SELECT status FROM governance_outbox " "WHERE message_id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        set_tenant(connection, tenant_b)
        inbox_status = connection.execute(
            text("SELECT status FROM collaboration_inbox " "WHERE message_id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        set_tenant(connection, outsider)
        hidden_outbox = connection.execute(
            text("SELECT count(*) FROM governance_outbox " "WHERE message_id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        hidden_inbox = connection.execute(
            text("SELECT count(*) FROM collaboration_inbox " "WHERE message_id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        if (
            outbox_status != "published"
            or inbox_status != "processed"
            or hidden_outbox != 0
            or hidden_inbox != 0
        ):
            raise RuntimeError("collaboration transport state or RLS invariant failed")
        if audit.verify_tenant_chain_in_transaction(connection, tenant_a) != 4:
            raise RuntimeError("sender transport audit chain is invalid")
        if audit.verify_tenant_chain_in_transaction(connection, tenant_b) != 2:
            raise RuntimeError("recipient transport audit chain is invalid")
        print(
            "COLLABORATION_RUNTIME_OK outbox_lease=yes fencing_token=yes "
            "lease_recovery=yes signed_envelope=yes freshness=yes redaction=yes "
            "inbox_dedup=yes processing_lease=yes tenant_isolation=yes atomic_audit=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
    if audit.verify_tenant_chain(tenant_a) or audit.verify_tenant_chain(tenant_b):
        raise RuntimeError("rolled-back collaboration audit records remain")
    print("COLLABORATION_ROLLBACK_OK test_records_retained=no")


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
            print(
                "POSTGRES_COLLABORATION_UNSAFE "
                "reason=runtime_role_can_bypass_rls secrets=redacted"
            )
            return 2
        print("POSTGRES_COLLABORATION_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_COLLABORATION_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
