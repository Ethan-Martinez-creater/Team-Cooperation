from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .agent_runs import AgentCheckpointKeyring, SQLAlchemyAgentRunRepository
from .auth import (
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    OIDCVerifier,
    OIDCWorkerIdentityProvider,
)
from .config import ConfigurationError, Environment, Settings
from .connectors import (
    ConnectorCatalog,
    OfficeMessageTools,
    SecureConnectorClient,
    load_connector_endpoints,
)
from .control_plane.bootstrap import DatabaseReadinessProbe, load_environment_settings
from .postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from .sandbox import (
    OCISandbox,
    SandboxedCodeTools,
    SandboxWorkspaceManager,
    load_code_profiles,
)
from .security import Classification
from .tool_catalog import build_builtin_manifests, validate_registry_manifests
from .tool_jobs import (
    DurableToolWorkerRunner,
    SQLAlchemyToolJobRepository,
    ToolBatchCoordinator,
    ToolJobKeyring,
)
from .tools import ToolRegistry

logger = logging.getLogger("coifesp.tool_worker")


@dataclass(slots=True)
class ToolWorkerRuntime:
    runner: DurableToolWorkerRunner
    tokens: ClientCredentialsTokenProvider
    verifier: OIDCVerifier
    engine: Engine

    async def aclose(self) -> None:
        await self.tokens.aclose()
        await self.verifier.aclose()
        self.engine.dispose()


async def build_tool_worker_runtime(
    settings: Settings, *, registry: ToolRegistry | None = None
) -> ToolWorkerRuntime:
    # File publication and office tools do not execute code. Validate a full
    # sandbox configuration only when any sandbox option was explicitly set.
    require_sandbox = registry is None and any((settings.sandbox_runtime,
        settings.sandbox_workspace_root, settings.sandbox_profiles_json))
    settings.validate(
        require_auth=True,
        require_memory=True,
        require_tool_worker=True,
        require_sandbox=require_sandbox,
    )
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required by the Tool Worker")
    assert settings.tool_worker_token_endpoint is not None
    assert settings.tool_worker_client_id is not None
    assert settings.tool_worker_client_secret is not None
    assert settings.tool_worker_tenant_id is not None
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
    tokens: ClientCredentialsTokenProvider | None = None
    try:
        DatabaseReadinessProbe(engine)()
        audit = SQLAlchemyAuditLog(
            engine=engine, keyring=AuditSigningKeyring.from_settings(settings)
        )
        runs = SQLAlchemyAgentRunRepository(
            engine=engine,
            keyring=AgentCheckpointKeyring.from_settings(settings),
            audit_log=audit,
        )
        jobs = SQLAlchemyToolJobRepository(
            engine=engine,
            keyring=ToolJobKeyring.from_settings(settings),
            audit_log=audit,
        )
        coordinator = ToolBatchCoordinator(engine=engine, agent_runs=runs, tool_jobs=jobs)
        allow_http = settings.environment is not Environment.PRODUCTION
        tokens = ClientCredentialsTokenProvider(
            ClientCredentialsConfig(
                token_endpoint=settings.tool_worker_token_endpoint,
                client_id=settings.tool_worker_client_id,
                client_secret=settings.tool_worker_client_secret,
                allow_insecure_http=allow_http,
            )
        )
        identity = OIDCWorkerIdentityProvider(
            tokens=tokens,
            verifier=verifier,
            required_role="tool_worker",
            expected_tenant_id=settings.tool_worker_tenant_id,
        )
        effective_registry = registry
        workspace_manager = None
        reconciler = coordinator
        if effective_registry is None:
            effective_registry = ToolRegistry()
            profiles = ()
            sandbox = None
            workspace_root = None
            if require_sandbox:
                assert settings.sandbox_runtime is not None
                assert settings.sandbox_workspace_root is not None
                assert settings.sandbox_profiles_json is not None
                workspace_root = Path(settings.sandbox_workspace_root)
                profiles = load_code_profiles(settings.sandbox_profiles_json)
                sandbox = OCISandbox(
                    runtime=settings.sandbox_runtime, workspace_root=workspace_root,
                    allowed_images=frozenset(item.image for item in profiles),
                )
                effective_registry.register(SandboxedCodeTools(
                    sandbox=sandbox, profiles=profiles, workspace_root=workspace_root,
                ).definition())
                workspace_manager = SandboxWorkspaceManager(root=workspace_root)
            if settings.connectors_json:
                catalog = ConnectorCatalog()
                for endpoint in load_connector_endpoints(
                    settings.connectors_json,
                    tenant_id=settings.tool_worker_tenant_id,
                ):
                    catalog.register(endpoint)
                effective_registry.register(
                    OfficeMessageTools(
                        client=SecureConnectorClient(catalog=catalog),
                        tenant_id=settings.tool_worker_tenant_id,
                        classification=Classification[settings.office_data_classification.upper()],
                    ).definition()
                )
            content = None
            if settings.artifact_store_root:
                from .artifacts.content import ArtifactContentService
                from .artifacts.repository import SQLAlchemyArtifactRepository
                from .artifacts.storage import LocalImmutableArtifactStore
                from .artifacts.task_publication import (
                    TaskArtifactPublicationService,
                    TaskArtifactPublicationTool,
                )
                from .project_process.repository import (
                    SQLAlchemyProjectProcessRepository,
                )
                from .project_process.scheduler import (
                    ProjectProcessScheduler,
                    SQLAlchemyProjectProcessWakeupRepository,
                )

                project_repository = SQLAlchemyProjectProcessRepository(engine)
                scheduler = ProjectProcessScheduler(SQLAlchemyProjectProcessWakeupRepository(engine))

                def enqueue_project_event(connection, event):
                    scheduler.enqueue_in_transaction(connection,
                        process_id=event.process_id, project_id=event.project_id,
                        source_event_id=event.event_id, source_event_type=event.event_type,
                        payload={"event_id": event.event_id}, available_at=event.occurred_at)

                project_repository.set_event_listener(enqueue_project_event)

                content = ArtifactContentService(
                    SQLAlchemyArtifactRepository(engine=engine, audit_log=audit),
                    LocalImmutableArtifactStore(Path(settings.artifact_store_root),
                        max_object_bytes=settings.artifact_max_upload_bytes),
                )
                effective_registry.register(TaskArtifactPublicationTool(
                    TaskArtifactPublicationService(repository=project_repository,
                        runs=runs, jobs=jobs, artifact_content=content),
                ).definition())
            manifests = build_builtin_manifests(
                sandbox_profile_ids=(item.profile_id for item in profiles),
                sandbox_timeout_seconds=max((item.limits.timeout_seconds for item in profiles), default=80) + 10,
                office_connector_configured=bool(settings.connectors_json),
                task_artifact_publication_configured=content is not None,
            )
            validate_registry_manifests(manifests, effective_registry, executor="tool_worker")
            # Internal verification is not part of the model-visible catalog.
            if content is not None and sandbox is not None:
                from .product.notifications import NotificationService
                from .verification.agent_reviews import AgentReviewChecks
                from .verification.sandbox_tool import SandboxedVerificationTool
                from .verification.service import TaskVerificationService
                from .verification.tool_checks import (
                    DurableVerificationChecks,
                    VerificationToolReconciler,
                )

                effective_registry.register(SandboxedVerificationTool(
                    engine=engine, sandbox=sandbox, profiles=profiles,
                    workspace_root=workspace_root, artifact_content=content,
                ).definition())
                verification = TaskVerificationService(
                    repository=project_repository,
                    artifact_content=content, notifier=NotificationService(engine),
                    tool_checks=DurableVerificationChecks(jobs=jobs, profiles=profiles),
                    review_checks=AgentReviewChecks(
                        repository=project_repository,
                        runs=runs, artifact_content=content,
                    ),
                )
                reconciler = VerificationToolReconciler(
                    coordinator=coordinator, verifier=verification,
                )
        return ToolWorkerRuntime(
            runner=DurableToolWorkerRunner(
                repository=jobs,
                registry=effective_registry,
                reconciler=reconciler,
                identity_provider=identity,
                tenant_id=settings.tool_worker_tenant_id,
                idle_poll_seconds=settings.tool_worker_idle_poll_seconds,
                lease_seconds=settings.tool_worker_lease_seconds,
                heartbeat_seconds=settings.tool_worker_heartbeat_seconds,
                workspace_manager=workspace_manager,
            ),
            tokens=tokens,
            verifier=verifier,
            engine=engine,
        )
    except Exception:
        if tokens is not None:
            await tokens.aclose()
        await verifier.aclose()
        engine.dispose()
        raise


async def run_tool_worker(settings: Settings) -> None:
    runtime = await build_tool_worker_runtime(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is None:
            continue
        try:
            loop.add_signal_handler(value, stop.set)
            installed.append(value)
        except (NotImplementedError, RuntimeError):
            continue
    logger.info("durable Tool Worker started")
    try:
        await runtime.runner.run(stop=stop)
    finally:
        for value in installed:
            loop.remove_signal_handler(value)
        await runtime.aclose()
        logger.info("durable Tool Worker stopped")


def main() -> int:
    try:
        settings = load_environment_settings(Path.cwd() / ".env")
        asyncio.run(run_tool_worker(settings))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports failure without secrets
        logger.error("durable Tool Worker failed error_type=%s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
