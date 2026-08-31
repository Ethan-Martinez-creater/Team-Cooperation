from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .agent_runs import (
    AgentCheckpointKeyring,
    AgentControlKeyring,
    AgentRunService,
    DurableAgentWorker,
    DurableAgentWorkerRunner,
    SQLAlchemyAgentRunRepository,
)
from .approvals import ApprovalService, SQLAlchemyApprovalRepository
from .auth import (
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    KeycloakDirectoryConfig,
    KeycloakPrincipalResolver,
    OIDCVerifier,
    OIDCWorkerIdentityProvider,
)
from .config import ConfigurationError, Environment, Settings
from .context import ContextAssembler
from .control_plane.bootstrap import DatabaseReadinessProbe, load_environment_settings
from .idempotency import InMemoryIdempotencyStore
from .observability import ObservabilityRuntime, configure_structured_logging
from .postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from .runtime import AgentLoop
from .runtime.providers import build_model_gateway
from .security import PolicyEngine
from .skills import SkillCatalog, SkillTrustStore
from .team_agents import TeamAgentPrincipalResolver
from .tool_catalog import (
    build_agent_worker_registry,
    build_builtin_manifests,
    validate_registry_manifests,
)
from .tool_jobs import SQLAlchemyToolJobRepository, ToolBatchCoordinator, ToolJobKeyring
from .tools import ToolExecutor
from .verification.worker_runtime import configure_worker_reviews

logger = logging.getLogger("coifesp.worker")


def build_skill_catalog_from_settings(settings: Settings) -> SkillCatalog | None:
    """Load the signed skill catalog, or ``None`` when skills are unset.

    Only administrator-preset, Ed25519-signed packages are accepted; the trust
    roots come from deployment configuration and are never inferred from the
    packages themselves.
    """
    if not settings.skills_root or not settings.skills_trusted_keys_json:
        return None
    import base64
    import json
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    trust = SkillTrustStore()
    try:
        entries = json.loads(settings.skills_trusted_keys_json)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("COIFESP_SKILLS_TRUSTED_KEYS_JSON is invalid JSON") from exc
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("COIFESP_SKILLS_TRUSTED_KEYS_JSON must be a non-empty list")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "tenant_id",
            "key_id",
            "public_key",
        }:
            raise ConfigurationError("skill trust entries are invalid")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(entry["public_key"], validate=True)
            )
        except Exception as exc:
            raise ConfigurationError("skill trust public key is invalid") from exc
        trust.register(tenant_id=entry["tenant_id"], key_id=entry["key_id"], public_key=public_key)
    catalog = SkillCatalog(
        root=Path(settings.skills_root),
        trust_store=trust,
        policy=PolicyEngine(),
    )
    catalog.scan()
    return catalog


_build_skill_catalog = build_skill_catalog_from_settings


@dataclass(slots=True)
class WorkerRuntime:
    runner: DurableAgentWorkerRunner
    worker_tokens: ClientCredentialsTokenProvider
    directory_tokens: ClientCredentialsTokenProvider
    directory: KeycloakPrincipalResolver
    verifier: OIDCVerifier
    engine: Engine
    observability: ObservabilityRuntime

    async def aclose(self) -> None:
        await self.directory.aclose()
        await self.directory_tokens.aclose()
        await self.worker_tokens.aclose()
        await self.verifier.aclose()
        self.observability.shutdown()
        self.engine.dispose()


async def build_worker_runtime(settings: Settings) -> WorkerRuntime:
    settings.validate(require_auth=True, require_memory=True, require_llm=True, require_worker=True)
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required by the durable worker")
    assert settings.worker_token_endpoint is not None
    assert settings.worker_client_id is not None
    assert settings.worker_client_secret is not None
    assert settings.worker_tenant_id is not None
    assert settings.directory_api_base_url is not None
    assert settings.directory_realm is not None
    assert settings.directory_token_endpoint is not None
    assert settings.directory_client_id is not None
    assert settings.directory_client_secret is not None

    configure_structured_logging(settings)
    observability = ObservabilityRuntime(settings=settings)
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        pool_recycle=1800,
        hide_parameters=True,
    )
    verifier = OIDCVerifier(settings=settings)
    worker_tokens: ClientCredentialsTokenProvider | None = None
    directory_tokens: ClientCredentialsTokenProvider | None = None
    directory: KeycloakPrincipalResolver | None = None
    allow_http = settings.environment is not Environment.PRODUCTION
    try:
        DatabaseReadinessProbe(engine)()
        audit = SQLAlchemyAuditLog(
            engine=engine,
            keyring=AuditSigningKeyring.from_settings(settings),
        )
        approvals = ApprovalService(SQLAlchemyApprovalRepository(engine=engine, audit_log=audit))
        run_repository = SQLAlchemyAgentRunRepository(
            engine=engine,
            keyring=AgentCheckpointKeyring.from_settings(settings),
            control_keyring=AgentControlKeyring.from_settings(settings),
            audit_log=audit,
        )
        service = AgentRunService(run_repository, approval_service=approvals)
        tool_repository = SQLAlchemyToolJobRepository(
            engine=engine,
            keyring=ToolJobKeyring.from_settings(settings),
            audit_log=audit,
        )
        tool_dispatcher = ToolBatchCoordinator(
            engine=engine,
            agent_runs=run_repository,
            tool_jobs=tool_repository,
        )
        review_reconciler = configure_worker_reviews(
            settings=settings, engine=engine, service=service,
            jobs=tool_repository, audit=audit,
        )
        # The Agent Worker exposes the same manifests the Tool Worker executes.
        # Declarations carry no handlers: durable tools dispatch to Tool Jobs,
        # and run-scoped skill handlers are attached per run by the loop.
        sandbox_profile_ids = ()
        sandbox_timeout_seconds = 90.0
        if settings.sandbox_profiles_json:
            from .sandbox import load_code_profiles

            sandbox_profiles = load_code_profiles(settings.sandbox_profiles_json)
            sandbox_profile_ids = tuple(profile.profile_id for profile in sandbox_profiles)
            sandbox_timeout_seconds = max(profile.limits.timeout_seconds for profile in sandbox_profiles) + 10
        manifests = build_builtin_manifests(
            sandbox_profile_ids=sandbox_profile_ids,
            sandbox_timeout_seconds=sandbox_timeout_seconds,
            office_connector_configured=bool(settings.connectors_json),
            task_artifact_publication_configured=bool(settings.artifact_store_root),
        )
        registry = build_agent_worker_registry(manifests)
        validate_registry_manifests(manifests, registry)
        skill_catalog = _build_skill_catalog(settings)
        gateway = build_model_gateway(settings, observer=observability)
        agent_loop = AgentLoop(
            provider=gateway,
            registry=registry,
            executor=ToolExecutor(
                registry=registry,
                policy=PolicyEngine(),
                audit=audit,
                # Durable tools dispatch to Tool Jobs; the Tool Worker owns
                # side-effecting handlers and the sandbox boundary.
                idempotency=InMemoryIdempotencyStore(),
                approval_service=approvals,
            ),
            audit=audit,
            context_assembler=ContextAssembler(policy=PolicyEngine(), audit=audit),
            durable_tools=True,
            skill_catalog=skill_catalog,
        )
        worker_tokens = ClientCredentialsTokenProvider(
            ClientCredentialsConfig(
                token_endpoint=settings.worker_token_endpoint,
                client_id=settings.worker_client_id,
                client_secret=settings.worker_client_secret,
                allow_insecure_http=allow_http,
            )
        )
        directory_tokens = ClientCredentialsTokenProvider(
            ClientCredentialsConfig(
                token_endpoint=settings.directory_token_endpoint,
                client_id=settings.directory_client_id,
                client_secret=settings.directory_client_secret,
                allow_insecure_http=allow_http,
            )
        )
        directory = KeycloakPrincipalResolver(
            config=KeycloakDirectoryConfig(
                admin_api_base_url=settings.directory_api_base_url,
                realm=settings.directory_realm,
                allow_insecure_http=allow_http,
            ),
            tokens=directory_tokens,
        )
        worker = DurableAgentWorker(
            service=service,
            loop=agent_loop,
            principal_resolver=TeamAgentPrincipalResolver(
                engine=engine,
                human_resolver=directory,
            ),
            lease_seconds=settings.worker_lease_seconds,
            heartbeat_interval_seconds=settings.worker_heartbeat_seconds,
            observer=observability,
            tool_dispatcher=tool_dispatcher,
        )
        identity = OIDCWorkerIdentityProvider(
            tokens=worker_tokens,
            verifier=verifier,
            expected_tenant_id=settings.worker_tenant_id,
        )
        return WorkerRuntime(
            runner=DurableAgentWorkerRunner(
                worker=worker,
                identity_provider=identity,
                idle_poll_seconds=settings.worker_idle_poll_seconds,
                terminal_reconciler=review_reconciler,
            ),
            worker_tokens=worker_tokens,
            directory_tokens=directory_tokens,
            directory=directory,
            verifier=verifier,
            engine=engine,
            observability=observability,
        )
    except Exception:
        if directory is not None:
            await directory.aclose()
        if directory_tokens is not None:
            await directory_tokens.aclose()
        if worker_tokens is not None:
            await worker_tokens.aclose()
        await verifier.aclose()
        observability.shutdown()
        engine.dispose()
        raise


async def run_worker(settings: Settings) -> None:
    runtime = await build_worker_runtime(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is None:
            continue
        try:
            loop.add_signal_handler(value, stop.set)
            installed.append(value)
        except (NotImplementedError, RuntimeError):
            continue
    logger.info("durable agent worker started")
    try:
        await runtime.runner.run(stop=stop)
    finally:
        for value in installed:
            loop.remove_signal_handler(value)
        await runtime.aclose()
        logger.info("durable agent worker stopped")


def main() -> int:
    try:
        settings = load_environment_settings(Path.cwd() / ".env")
        asyncio.run(run_worker(settings))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - process entry point
        logging.getLogger("coifesp.worker").error(
            "durable agent worker failed error_type=%s", type(exc).__name__
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
