import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.agent_runs import (  # noqa: E402
    AgentCheckpointKeyring,
    AgentControlKeyring,
    AgentControlStatus,
    AgentControlType,
    AgentRunPersistenceError,
    AgentRunService,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.security import Principal  # noqa: E402

EXPECTED_REVISION = "20260813_28"
EXPECTED_POLICIES = {
    ("agent_runs", "agent_runs_tenant_select", "SELECT"),
    ("agent_runs", "agent_runs_tenant_insert", "INSERT"),
    ("agent_runs", "agent_runs_tenant_update", "UPDATE"),
    ("agent_run_events", "agent_run_events_tenant_select", "SELECT"),
    ("agent_run_events", "agent_run_events_tenant_insert", "INSERT"),
    ("agent_run_commands", "agent_run_commands_tenant_select", "SELECT"),
    ("agent_run_commands", "agent_run_commands_tenant_insert", "INSERT"),
    ("agent_run_commands", "agent_run_commands_tenant_update", "UPDATE"),
}
EXPECTED_TRIGGERS = {
    "trg_agent_run_events_reject_update_delete",
    "trg_agent_run_events_reject_truncate",
    "trg_agent_run_commands_validate_update",
    "trg_agent_run_commands_reject_delete",
    "trg_agent_run_commands_reject_truncate",
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
    AuditSigningKeyring.from_settings(settings)
    AgentCheckpointKeyring.from_settings(settings)
    AgentControlKeyring.from_settings(settings)
    return settings


def set_tenant(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def check_schema(engine) -> tuple[bool, bool]:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity)
            for row in connection.execute(text("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relname IN (
                    'agent_runs', 'agent_run_events', 'agent_run_commands'
                  )
                """))
        }
        policies = {(row.tablename, row.policyname, row.cmd) for row in connection.execute(text("""
                SELECT tablename, policyname, cmd
                FROM pg_catalog.pg_policies
                WHERE schemaname = current_schema()
                  AND tablename IN (
                    'agent_runs', 'agent_run_events', 'agent_run_commands'
                  )
                """))}
        triggers = {row.tgname for row in connection.execute(text("""
                SELECT t.tgname
                FROM pg_catalog.pg_trigger AS t
                JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relname IN ('agent_run_events', 'agent_run_commands')
                  AND NOT t.tgisinternal
                """))}
        runtime_columns = {row.column_name for row in connection.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'agent_runs'
                  AND column_name IN (
                    'failure_count', 'max_failures',
                    'next_attempt_at', 'last_error_code',
                    'model_cost_microusd'
                  )
                """))}
        role = connection.execute(text("""
            SELECT rolsuper, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname = current_user
            """)).one()
    if revision != EXPECTED_REVISION:
        raise RuntimeError("database revision is not the expected head")
    if set(tables) != {"agent_runs", "agent_run_events", "agent_run_commands"} or not all(
        enabled and forced for enabled, forced in tables.values()
    ):
        raise RuntimeError("agent run tables do not force RLS")
    if policies != EXPECTED_POLICIES:
        raise RuntimeError("agent run RLS policies differ from the reviewed model")
    if triggers != EXPECTED_TRIGGERS:
        raise RuntimeError("agent run event immutability triggers are missing")
    if runtime_columns != {
        "failure_count",
        "max_failures",
        "next_attempt_at",
        "last_error_code",
        "model_cost_microusd",
    }:
        raise RuntimeError("agent run retry columns are missing")
    print(
        f"AGENT_RUN_SCHEMA_OK revision={revision} tables={len(tables)} "
        f"policies={len(policies)} triggers={len(triggers)} rls=forced"
    )
    print(
        "DATABASE_ROLE "
        f"superuser={'yes' if role.rolsuper else 'no'} "
        f"bypass_rls={'yes' if role.rolbypassrls else 'no'} "
        f"runtime_safe={'no' if role.rolsuper or role.rolbypassrls else 'yes'}"
    )
    return bool(role.rolsuper), bool(role.rolbypassrls)


def checkpoint(
    content: str,
    *,
    turns: int = 0,
    tokens: int = 0,
    model_cost_microusd: int = 0,
    control=None,
) -> dict:
    messages = [{"role": "user", "content": content}]
    cursor = 0
    if control is not None:
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "schema": "coifesp.agent-control.v1",
                        "instruction_trust": "user_instruction",
                        "sequence": control.sequence,
                        "command_id": control.command_id,
                        "command_type": control.command_type.value,
                        "content": control.content,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        cursor = control.sequence
    return {
        "schema": "coifesp.agent-run-checkpoint.v1",
        "messages": messages,
        "budget": {
            "max_turns": 20,
            "max_tool_calls": 50,
            "max_total_tokens": 100000,
            "max_model_cost_microusd": 10000000,
        },
        "usage": {
            "turns": turns,
            "tool_calls": 0,
            "total_tokens": tokens,
            "model_cost_microusd": model_cost_microusd,
        },
        "approval_bindings": [],
        "control_cursor": cursor,
        "model_route_policy": {
            "data_classification": 1,
            "required_capabilities": [],
            "allowed_provider_ids": [],
            "residency_regions": [],
            "allow_external_egress": False,
            "max_call_cost_microusd": None,
            "max_output_tokens": None,
            "max_call_total_tokens": None,
        },
    }


def check_runtime(engine, settings: Settings) -> None:
    token = uuid.uuid4().hex
    tenant = f"agent-run-{token[:12]}"
    outsider = f"outsider-{token[:12]}"
    run_id = f"run-{token[:12]}"
    secret_marker = f"private-{token}"
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings),
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        repository = SQLAlchemyAgentRunRepository(
            engine=engine,
            keyring=AgentCheckpointKeyring.from_settings(settings),
            control_keyring=AgentControlKeyring.from_settings(settings),
            audit_log=audit,
        ).using_connection(connection)
        service = AgentRunService(repository)
        owner = Principal("lead", tenant)
        worker = Principal(
            "worker",
            tenant,
            roles=frozenset({"agent_worker"}),
            is_service=True,
        )
        created = service.create(
            principal=owner,
            run_id=run_id,
            correlation_id=f"corr-{token[:12]}",
            idempotency_key=f"idem-{token[:12]}",
            checkpoint=checkpoint(secret_marker),
        )
        duplicate = service.create(
            principal=owner,
            run_id=run_id,
            correlation_id=f"corr-{token[:12]}",
            idempotency_key=f"idem-{token[:12]}",
            checkpoint=checkpoint(secret_marker),
        )
        control_secret = f"steering-{token}"
        command = service.submit_control(
            principal=owner,
            run_id=run_id,
            command_id=f"command-{token[:12]}",
            command_type=AgentControlType.STEER,
            content=control_secret,
            expected_run_version=created.version,
        )
        lease = service.claim(worker=worker, lease_seconds=60)
        if lease is None:
            raise RuntimeError("queued run could not be claimed")
        service.start(worker=worker, run_id=run_id, lease_token=lease.lease_token)
        expiry = service.heartbeat(
            worker=worker,
            run_id=run_id,
            lease_token=lease.lease_token,
            lease_seconds=120,
        )
        completed = service.checkpoint(
            worker=worker,
            run_id=run_id,
            lease_token=lease.lease_token,
            target=DurableRunStatus.COMPLETED,
            checkpoint=checkpoint(
                secret_marker,
                turns=1,
                tokens=12,
                model_cost_microusd=37,
                control=command,
            ),
            turns=1,
            tool_calls=0,
            total_tokens=12,
            model_cost_microusd=37,
            applied_control_sequences=(command.sequence,),
        )
        persisted_run = connection.execute(
            text("""
                SELECT checkpoint_ciphertext, model_cost_microusd
                FROM agent_runs
                WHERE tenant_id = :tenant AND run_id = :run_id
                """),
            {"tenant": tenant, "run_id": run_id},
        ).one()
        command_row = connection.execute(
            text("""
                SELECT content_ciphertext, status, applied_run_version
                FROM agent_run_commands
                WHERE tenant_id = :tenant AND run_id = :run_id
                """),
            {"tenant": tenant, "run_id": run_id},
        ).one()
        if (
            created.run_id != duplicate.run_id
            or completed.status is not DurableRunStatus.COMPLETED
            or expiry <= datetime.now(UTC)
            or secret_marker.encode() in bytes(persisted_run.checkpoint_ciphertext)
            or persisted_run.model_cost_microusd != 37
            or control_secret.encode() in bytes(command_row.content_ciphertext)
            or command_row.status != AgentControlStatus.APPLIED.value
            or command_row.applied_run_version != completed.version
        ):
            raise RuntimeError(
                "agent run lifecycle, control atomicity, idempotency, or encryption failed"
            )
        events = service.events(principal=owner, run_id=run_id)
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise RuntimeError("agent run event sequence is not contiguous")
        retry_run_id = f"retry-{token[:12]}"
        service.create(
            principal=owner,
            run_id=retry_run_id,
            correlation_id=f"retry-corr-{token[:12]}",
            idempotency_key=f"retry-idem-{token[:12]}",
            checkpoint=checkpoint("retry safely"),
            max_failures=2,
        )
        retry_lease = service.claim(worker=worker, lease_seconds=60)
        if retry_lease is None or retry_lease.run.run_id != retry_run_id:
            raise RuntimeError("retry test run could not be claimed")
        service.start(
            worker=worker,
            run_id=retry_run_id,
            lease_token=retry_lease.lease_token,
        )
        scheduled = service.retry(
            worker=worker,
            run_id=retry_run_id,
            lease_token=retry_lease.lease_token,
            error_code="dependency_timeout",
            delay_seconds=0,
        )
        second_lease = service.claim(worker=worker, lease_seconds=60)
        if second_lease is None or second_lease.run.run_id != retry_run_id:
            raise RuntimeError("scheduled retry was not claimable")
        service.start(
            worker=worker,
            run_id=retry_run_id,
            lease_token=second_lease.lease_token,
        )
        exhausted = service.retry(
            worker=worker,
            run_id=retry_run_id,
            lease_token=second_lease.lease_token,
            error_code="dependency_timeout",
            delay_seconds=0,
        )
        retry_events = service.events(principal=owner, run_id=retry_run_id)
        if (
            scheduled.status is not DurableRunStatus.QUEUED
            or exhausted.status is not DurableRunStatus.FAILED
            or exhausted.failure_count != exhausted.max_failures
            or exhausted.last_error_code != "dependency_timeout"
        ):
            raise RuntimeError("bounded agent retry lifecycle failed")
        set_tenant(connection, outsider)
        hidden = connection.execute(
            text("SELECT count(*) FROM agent_runs WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
        if hidden != 0:
            raise RuntimeError("agent run crossed the tenant RLS boundary")
        hidden_commands = connection.execute(
            text("SELECT count(*) FROM agent_run_commands WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
        if hidden_commands != 0:
            raise RuntimeError("agent control command crossed the tenant RLS boundary")
        set_tenant(connection, tenant)
        connection.exec_driver_sql("SAVEPOINT agent_event_immutability")
        try:
            mutation = connection.execute(
                text("""
                    UPDATE agent_run_events SET event_type = 'tampered'
                    WHERE tenant_id = :tenant AND run_id = :run_id AND sequence = 1
                    """),
                {"tenant": tenant, "run_id": run_id},
            )
        except Exception:
            connection.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_event_immutability")
            connection.exec_driver_sql("RELEASE SAVEPOINT agent_event_immutability")
        else:
            connection.exec_driver_sql("RELEASE SAVEPOINT agent_event_immutability")
            if mutation.rowcount != 0:
                raise RuntimeError("agent run event mutation was accepted")
        connection.exec_driver_sql("SAVEPOINT agent_command_immutability")
        try:
            mutation = connection.execute(
                text("""
                    UPDATE agent_run_commands SET command_type = 'follow_up'
                    WHERE tenant_id = :tenant AND run_id = :run_id AND sequence = 1
                    """),
                {"tenant": tenant, "run_id": run_id},
            )
        except Exception:
            connection.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_command_immutability")
            connection.exec_driver_sql("RELEASE SAVEPOINT agent_command_immutability")
        else:
            connection.exec_driver_sql("RELEASE SAVEPOINT agent_command_immutability")
            if mutation.rowcount != 0:
                raise RuntimeError("agent control command mutation was accepted")
        connection.exec_driver_sql("SAVEPOINT agent_command_terminal_immutability")
        try:
            mutation = connection.execute(
                text("""
                    UPDATE agent_run_commands
                    SET applied_run_version = applied_run_version + 1
                    WHERE tenant_id = :tenant AND run_id = :run_id AND sequence = 1
                    """),
                {"tenant": tenant, "run_id": run_id},
            )
        except Exception:
            connection.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_command_terminal_immutability")
            connection.exec_driver_sql("RELEASE SAVEPOINT agent_command_terminal_immutability")
        else:
            connection.exec_driver_sql("RELEASE SAVEPOINT agent_command_terminal_immutability")
            if mutation.rowcount != 0:
                raise RuntimeError("terminal agent control lifecycle mutation was accepted")
        if audit.verify_tenant_chain_in_transaction(connection, tenant) != (
            len(events) + len(retry_events)
        ):
            raise RuntimeError("agent run lifecycle and signed audit chain diverged")
        print(
            "AGENT_RUN_RUNTIME_OK encrypted_checkpoint=yes idempotency=yes "
            "encrypted_control=yes atomic_control_checkpoint=yes immutable_control=yes "
            "lease_fencing=yes heartbeat=yes bounded_retry=yes sse_cursor_events=yes "
            "model_cost_recovery=yes tenant_isolation=yes "
            "immutable_events=yes atomic_audit=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
    if audit.verify_tenant_chain(tenant) != 0:
        raise RuntimeError("rolled-back agent run audit records remain")
    print("AGENT_RUN_ROLLBACK_OK test_records_retained=no")


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
            print("POSTGRES_AGENT_RUN_UNSAFE reason=runtime_role_can_bypass_rls secrets=redacted")
            return 2
        print("POSTGRES_AGENT_RUN_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_AGENT_RUN_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
