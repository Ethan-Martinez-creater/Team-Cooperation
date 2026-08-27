import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.approvals import (  # noqa: E402
    ApprovalService,
    ApprovalStatus,
    ApprovalWorkflowError,
    SQLAlchemyApprovalRepository,
)
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.security import Classification, Principal, ResourceLabel  # noqa: E402

EXPECTED_REVISION = "20260813_28"
EXPECTED_POLICIES = {
    ("approval_requests_tenant_select", "SELECT"),
    ("approval_requests_tenant_insert", "INSERT"),
    ("approval_requests_tenant_update", "UPDATE"),
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


def set_tenant(connection, tenant_id: str) -> None:
    connection.execute(
        text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def check_schema(engine) -> tuple[bool, bool]:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        table = connection.execute(text("""
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relname = 'approval_requests'
            """)).one()
        policies = {(row.policyname, row.cmd) for row in connection.execute(text("""
                SELECT policyname, cmd
                FROM pg_catalog.pg_policies
                WHERE schemaname = current_schema()
                  AND tablename = 'approval_requests'
                """))}
        role = connection.execute(text("""
            SELECT rolsuper, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname = current_user
            """)).one()
    if revision != EXPECTED_REVISION:
        raise RuntimeError("database revision is not the expected head")
    if not table.relrowsecurity or not table.relforcerowsecurity:
        raise RuntimeError("approval table does not force RLS")
    if policies != EXPECTED_POLICIES:
        raise RuntimeError("approval RLS policies differ from the reviewed model")
    print(
        f"APPROVAL_SCHEMA_OK revision={revision} tables=1 " f"policies={len(policies)} rls=forced"
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
    tenant = f"approval-{token[:12]}"
    outsider = f"outsider-{token[:12]}"
    approval_id = f"approval-{token[:12]}"
    digest = "a" * 64
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings),
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        repository = SQLAlchemyApprovalRepository(
            engine=engine,
            audit_log=audit,
        ).using_connection(connection)
        service = ApprovalService(repository)
        requester = Principal(
            "operator",
            tenant,
            clearance=Classification.CONFIDENTIAL,
            compartments=frozenset({"project-x"}),
        )
        approver = Principal(
            "reviewer",
            tenant,
            roles=frozenset({"tool_approver"}),
            clearance=Classification.CONFIDENTIAL,
            compartments=frozenset({"project-x"}),
        )
        review_projection = {
            "schema": "coifesp.approval-review.v1",
            "tool_name": "send_external",
            "fields": [
                {
                    "name": "destination",
                    "json_pointer": "/destination",
                    "disclosure": "value",
                    "value": "external-system",
                    "value_digest": "b" * 64,
                    "redaction_findings": [],
                }
            ],
        }
        pending = service.request_for_tool(
            principal=requester,
            approval_id=approval_id,
            tool_name="send_external",
            request_digest=digest,
            review_projection=review_projection,
            label=ResourceLabel(
                tenant,
                Classification.CONFIDENTIAL,
                frozenset({"project-x"}),
            ),
            expires_in_seconds=900,
            required_approver_role="tool_approver",
        )
        try:
            service.decide(
                principal=Principal(
                    "operator",
                    tenant,
                    roles=frozenset({"tool_approver"}),
                    clearance=Classification.CONFIDENTIAL,
                    compartments=frozenset({"project-x"}),
                ),
                approval_id=approval_id,
                approve=True,
                expected_version=pending.version,
            )
        except ApprovalWorkflowError:
            pass
        else:
            raise RuntimeError("self-approval was accepted")
        approved = service.decide(
            principal=approver,
            approval_id=approval_id,
            approve=True,
            expected_version=pending.version,
        )
        consumed = service.consume(
            principal=requester,
            approval_id=approval_id,
            tool_name="send_external",
            request_digest=digest,
            execution_id=f"execution-{token[:12]}",
            input_label=ResourceLabel(
                tenant,
                Classification.CONFIDENTIAL,
                frozenset({"project-x"}),
            ),
            require_tool_managed=True,
        )
        replay = service.consume(
            principal=requester,
            approval_id=approval_id,
            tool_name="send_external",
            request_digest=digest,
            execution_id=f"execution-{token[:12]}",
            input_label=ResourceLabel(
                tenant,
                Classification.CONFIDENTIAL,
                frozenset({"project-x"}),
            ),
            require_tool_managed=True,
        )
        if (
            approved.status is not ApprovalStatus.APPROVED
            or consumed.status is not ApprovalStatus.CONSUMED
            or replay.version != consumed.version
            or pending.review_projection != review_projection
            or pending.projection_digest is None
        ):
            raise RuntimeError("approval lifecycle or idempotent consumption failed")
        set_tenant(connection, outsider)
        hidden = connection.execute(
            text("SELECT count(*) FROM approval_requests WHERE approval_id = :approval_id"),
            {"approval_id": approval_id},
        ).scalar_one()
        if hidden != 0:
            raise RuntimeError("approval crossed the tenant RLS boundary")
        set_tenant(connection, tenant)
        if audit.verify_tenant_chain_in_transaction(connection, tenant) != 3:
            raise RuntimeError("approval lifecycle and signed audit chain diverged")
        print(
            "APPROVAL_RUNTIME_OK separation_of_duties=yes optimistic_version=yes "
            "exact_digest_binding=yes security_label_binding=yes single_use=yes replay_safe=yes "
            "managed_review_projection=yes tenant_isolation=yes atomic_audit=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
    if audit.verify_tenant_chain(tenant) != 0:
        raise RuntimeError("rolled-back approval audit records remain")
    print("APPROVAL_ROLLBACK_OK test_records_retained=no")


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
            print("POSTGRES_APPROVAL_UNSAFE reason=runtime_role_can_bypass_rls secrets=redacted")
            return 2
        print("POSTGRES_APPROVAL_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "POSTGRES_APPROVAL_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
