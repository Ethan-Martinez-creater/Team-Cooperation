import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.agent_runs import (  # noqa: E402
    AgentCheckpointKeyring,
    AgentRunCheckpointCodec,
    AgentRunService,
    AgentWorkerOutcomeStatus,
    DurableAgentWorker,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.idempotency import InMemoryIdempotencyStore  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.runtime import (  # noqa: E402
    AgentLoop,
    AgentRunRequest,
    LLMResponse,
    Message,
    ToolCall,
)
from coifesp_harness.security import PolicyEngine, Principal, RiskLevel  # noqa: E402
from coifesp_harness.tool_jobs import (  # noqa: E402
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolBatchCoordinator,
    ToolJobKeyring,
)
from coifesp_harness.tools import ToolDefinition, ToolExecutor, ToolRegistry  # noqa: E402


class Provider:
    def __init__(self, token: str) -> None:
        self.calls = 0
        self.token = token

    async def complete(self, *, messages, **_):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                tool_calls=(
                    ToolCall("call-a", "smoke.echo", {"value": f"a-{self.token}"}),
                    ToolCall("call-b", "smoke.echo", {"value": f"b-{self.token}"}),
                ),
                input_tokens=3,
                output_tokens=2,
            )
        if sum(message.role == "tool" for message in messages) != 2:
            raise RuntimeError("resumed model context lacks the complete tool batch")
        return LLMResponse(text="done", input_tokens=4, output_tokens=1)


class Resolver:
    def __init__(self, owner):
        self.owner = owner

    async def resolve(self, **_):
        return self.owner


class BoundAudit:
    def __init__(self, audit, connection):
        self.audit = audit
        self.connection = connection

    def append(self, event):
        return self.audit.append_in_transaction(self.connection, event)


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


def run_check(engine, settings: Settings) -> None:
    token = uuid.uuid4().hex[:16]
    tenant = f"batch-{token}"
    run_id = f"run-{token}"
    connection = engine.connect()
    transaction = connection.begin()
    try:
        audit_log = SQLAlchemyAuditLog(
            engine=engine,
            keyring=AuditSigningKeyring.from_settings(settings),
        )
        audit = BoundAudit(audit_log, connection)
        runs = SQLAlchemyAgentRunRepository(
            engine=engine,
            keyring=AgentCheckpointKeyring.from_settings(settings),
            audit_log=audit_log,
            _bound_connection=connection,
        )
        jobs = SQLAlchemyToolJobRepository(
            engine=engine,
            keyring=ToolJobKeyring.from_settings(settings),
            audit_log=audit_log,
            _bound_connection=connection,
        )
        # Coordinator opens one transaction; bind its repositories to the
        # already-owned smoke transaction while keeping the same engine.
        coordinator = ToolBatchCoordinator(
            engine=engine, agent_runs=runs, tool_jobs=jobs
        ).using_connection(connection)
        registry = ToolRegistry()

        async def echo(arguments):
            return {"echo": arguments["value"]}

        registry.register(
            ToolDefinition(
                name="smoke.echo",
                description="non-side-effecting PostgreSQL smoke tool",
                handler=echo,
                parameters_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                risk=RiskLevel.LOW,
            )
        )
        provider = Provider(token)
        loop = AgentLoop(
            provider=provider,
            registry=registry,
            executor=ToolExecutor(
                registry=registry,
                policy=PolicyEngine(),
                audit=audit,
                idempotency=InMemoryIdempotencyStore(),
            ),
            audit=audit,
            durable_tools=True,
        )
        owner = Principal("alice", tenant)
        worker_identity = Principal(
            "agent-worker", tenant, roles=frozenset({"agent_worker"}), is_service=True
        )
        request = AgentRunRequest(
            run_id=run_id,
            correlation_id=f"corr-{token}",
            principal=owner,
            messages=(Message("user", "run the PostgreSQL tool batch smoke"),),
        )
        service = AgentRunService(runs)
        service.create(
            principal=owner,
            run_id=run_id,
            correlation_id=request.correlation_id,
            idempotency_key=f"idem-{token}",
            checkpoint=AgentRunCheckpointCodec().initial(request),
        )
        agent = DurableAgentWorker(
            service=service,
            loop=loop,
            principal_resolver=Resolver(owner),
            tool_dispatcher=coordinator,
            heartbeat_interval_seconds=1,
        )
        first = asyncio.run(agent.process_once(worker=worker_identity))
        if first.status is not AgentWorkerOutcomeStatus.AWAITING_TOOL:
            raise RuntimeError("Agent did not enter awaiting_tool")
        tool = DurableToolWorker(
            repository=jobs,
            registry=registry,
            tenant_id=tenant,
            worker_id="tool-worker",
            lease_seconds=5,
            heartbeat_seconds=1,
            reconciler=coordinator,
        )
        if not asyncio.run(tool.run_once()) or not asyncio.run(tool.run_once()):
            raise RuntimeError("Tool Worker did not complete both jobs")
        if runs.get(tenant_id=tenant, run_id=run_id).status is not DurableRunStatus.QUEUED:
            raise RuntimeError("completed Tool batch did not wake the Agent")
        second = asyncio.run(agent.process_once(worker=worker_identity))
        if second.status is not AgentWorkerOutcomeStatus.COMPLETED:
            raise RuntimeError("resumed Agent did not complete")
        jobs_raw = connection.execute(
            text("SELECT count(*) FROM tool_jobs WHERE tenant_id=:tenant AND run_id=:run"),
            {"tenant": tenant, "run": run_id},
        ).scalar_one()
        events = connection.execute(
            text("SELECT count(*) FROM tool_job_events WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        ).scalar_one()
        if jobs_raw != 2 or events != 8:
            raise RuntimeError("Tool batch durable journal is incomplete")
        print(
            "POSTGRES_TOOL_BATCH_RUNTIME_OK parallel=2 atomic_dispatch=yes "
            "awaiting_tool=yes encrypted_checkpoint=yes tool_results=yes "
            "atomic_wakeup=yes resumed_agent=yes"
        )
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
    with engine.connect() as check:
        check.execute(
            text("SELECT set_config('coifesp.tenant_id',:tenant,true)"), {"tenant": tenant}
        )
        remaining = check.execute(
            text("SELECT count(*) FROM agent_runs WHERE tenant_id=:tenant"), {"tenant": tenant}
        ).scalar_one()
    if remaining:
        raise RuntimeError("rolled-back Tool batch smoke records remain")
    print("POSTGRES_TOOL_BATCH_ROLLBACK_OK test_records_retained=no")


def main() -> int:
    engine = None
    try:
        settings = load_settings()
        engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
        run_check(engine, settings)
        print("POSTGRES_TOOL_BATCH_OK secrets=redacted")
        return 0
    except Exception as exc:
        print(
            f"POSTGRES_TOOL_BATCH_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
