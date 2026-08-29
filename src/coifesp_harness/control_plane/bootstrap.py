from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..auth import OIDCVerifier
from ..approvals import ApprovalService, SQLAlchemyApprovalRepository
from ..artifacts import (
    ArtifactContentService,
    ArtifactDeliveryGuard,
    LocalImmutableArtifactStore,
    SQLAlchemyArtifactRepository,
)
from ..agent_runs import (
    AgentCheckpointKeyring,
    AgentControlKeyring,
    AgentRunService,
    SQLAlchemyAgentRunRepository,
)
from ..collaboration import (
    DurableCollaborationTransport,
    GovernanceService,
    SQLAlchemyGovernanceRepository,
    SignedEnvelopeCodec,
)
from ..capabilities import CapabilityDirectoryService, SQLAlchemyCapabilityRepository
from ..config import ConfigurationError, Settings
from ..contracts import ContractCoordinationService, SQLAlchemyContractRepository
from ..connectors import SQLAlchemyConnectorRegistry
from ..context import SemanticCheckpointKeyring, SemanticCheckpointService
from ..execution import SQLAlchemyTaskRepository, TaskExecutionService
from ..memory import (
    MemoryAdmissionPolicy,
    MemoryService,
    MemoryLifecycleService,
    SQLAlchemyMemoryRepository,
    TenantMemoryKeyring,
)
from ..observability import ObservabilityRuntime, configure_structured_logging
from ..postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from ..product import (
    CodeWorkspaceService,
    DocumentWorkspaceService,
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectResourceService,
    TeamCollaborationService,
)
from ..product.demo_bootstrap import ensure_local_demo
from ..product.turn_projection import AgentTurnProjection
from ..product.exchange import AgentExchangeService
from ..product.planning import ProjectPlanningService
from ..product.workspace import ProjectWorkspaceService
from ..security import PolicyEngine
from ..work_graph import ProjectWorkGraphService, SQLAlchemyWorkGraphRepository
from .app import create_app
from .session_lifecycle import SessionLifecycleService

SCHEMA_REVISION = "20260829_46"
REQUIRED_RLS_TABLES = (
    "audit_events",
    "audit_heads",
    "approval_requests",
    "agent_run_events",
    "agent_run_commands",
    "agent_runs",
    "collaboration_inbox",
    "collaboration_change_impacts",
    "collaboration_contract_commands",
    "collaboration_contract_dependencies",
    "collaboration_contract_events",
    "collaboration_contract_outbox",
    "collaboration_contract_releases",
    "collaboration_contracts",
    "execution_task_dependencies",
    "execution_task_events",
    "execution_tasks",
    "governance_assignment_artifacts",
    "governance_assignment_dependencies",
    "governance_assignments",
    "governance_commands",
    "governance_discussion_items",
    "governance_events",
    "governance_members",
    "governance_outbox",
    "governance_plan_approvers",
    "governance_plan_deliverables",
    "governance_plans",
    "governance_programs",
    "memory_idempotency_claims",
    "memory_records",
    "memory_search_terms",
    "memory_legal_holds",
    "memory_deletion_requests",
    "tool_job_events",
    "tool_jobs",
    "team_capabilities",
    "team_capability_commands",
    "team_capability_events",
    "team_capability_capacity",
    "team_capacity_reservations",
    "team_capacity_negotiations",
    "semantic_checkpoints",
    "artifact_manifests",
    "artifact_manifest_commands",
    "connector_registrations",
)
REQUIRED_AUDIT_TRIGGERS = (
    "trg_audit_events_reject_truncate",
    "trg_audit_events_reject_update_delete",
    "trg_agent_run_events_reject_truncate",
    "trg_agent_run_events_reject_update_delete",
    "trg_agent_run_commands_reject_delete",
    "trg_agent_run_commands_reject_truncate",
    "trg_agent_run_commands_validate_update",
    "trg_contract_events_immutable",
    "trg_contract_events_reject_truncate",
    "trg_execution_task_events_reject_truncate",
    "trg_execution_task_events_reject_update_delete",
    "trg_governance_events_reject_truncate",
    "trg_governance_events_reject_update_delete",
    "trg_tool_job_events_reject_truncate",
    "trg_tool_job_events_reject_update_delete",
    "trg_capability_events_immutable",
    "trg_capability_events_reject_truncate",
)


@dataclass(frozen=True, slots=True)
class DatabaseReadinessProbe:
    """Fail-closed database and security-schema readiness check."""

    engine: Engine
    expected_revision: str = SCHEMA_REVISION

    def __call__(self) -> bool:
        with self.engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                raise ConfigurationError("PostgreSQL is required by the durable control plane")
            connection.execute(text("SELECT 1")).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if revision != self.expected_revision:
                raise ConfigurationError("database schema is not at the required revision")

            role = connection.execute(text("""
                    SELECT rolsuper, rolbypassrls
                    FROM pg_catalog.pg_roles
                    WHERE rolname = current_user
                    """)).one()
            if role.rolsuper or role.rolbypassrls:
                raise ConfigurationError("runtime database role can bypass tenant isolation")

            protected_tables = set(
                connection.execute(
                    text("""
                        SELECT c.relname
                        FROM pg_catalog.pg_class AS c
                        JOIN pg_catalog.pg_namespace AS n
                          ON n.oid = c.relnamespace
                        WHERE n.nspname = current_schema()
                          AND c.relname = ANY(:table_names)
                          AND c.relrowsecurity
                          AND c.relforcerowsecurity
                        """),
                    {"table_names": list(REQUIRED_RLS_TABLES)},
                )
                .scalars()
                .all()
            )
            if protected_tables != set(REQUIRED_RLS_TABLES):
                raise ConfigurationError("required tables are not protected by forced RLS")

            audit_triggers = set(
                connection.execute(
                    text("""
                        SELECT t.tgname
                        FROM pg_catalog.pg_trigger AS t
                        JOIN pg_catalog.pg_class AS c
                          ON c.oid = t.tgrelid
                        JOIN pg_catalog.pg_namespace AS n
                          ON n.oid = c.relnamespace
                        WHERE n.nspname = current_schema()
                          AND c.relname IN (
                            'agent_run_commands',
                            'agent_run_events',
                            'audit_events',
                            'collaboration_contract_events',
                            'execution_task_events',
                            'governance_events',
                            'tool_job_events'
                            ,'team_capability_events'
                          )
                          AND t.tgname = ANY(:trigger_names)
                          AND NOT t.tgisinternal
                          AND t.tgenabled = 'O'
                        """),
                    {"trigger_names": list(REQUIRED_AUDIT_TRIGGERS)},
                )
                .scalars()
                .all()
            )
            if audit_triggers != set(REQUIRED_AUDIT_TRIGGERS):
                raise ConfigurationError("immutable audit triggers are not enabled")
        return True


def _run_conversation_reader(agent_run_service):
    """Read a run's user-visible conversation using an internal controller identity."""
    from ..security import Classification, Principal

    def read(run):
        principal = Principal(
            run.owner_principal_id,
            run.tenant_id,
            frozenset({"agent_run_controller"}),
            Classification.RESTRICTED,
            frozenset(),
        )
        return agent_run_service.conversation(principal=principal, run_id=run.run_id)

    return read


def _run_context_reader(agent_run_service):
    """Read a run's checkpoint context items using an internal controller identity."""
    from ..agent_runs.checkpoint import AgentRunCheckpointCodec
    from ..security import Classification, Principal

    def read(run):
        principal = Principal(
            run.owner_principal_id,
            run.tenant_id,
            frozenset({"agent_run_controller"}),
            Classification.RESTRICTED,
            frozenset(),
        )
        checkpoint = agent_run_service.load_checkpoint(principal=principal, run_id=run.run_id)
        decoded = AgentRunCheckpointCodec().decode(checkpoint)
        return decoded.get("context_items", ())

    return read


def load_environment_settings(env_file: Path | None = None) -> Settings:
    """Load process environment, optionally supplemented by an explicit .env file."""
    from dotenv import load_dotenv

    candidate = env_file if env_file is not None else Path.cwd() / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)
    return Settings.from_environment()


def build_application(
    *,
    settings: Settings,
    engine: Engine | None = None,
    verifier: OIDCVerifier | None = None,
    readiness_probe: Callable[[], bool | None] | None = None,
) -> FastAPI:
    """Assemble the durable control plane and its owned resource lifecycle."""
    settings.validate(require_auth=True, require_memory=True)
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required by the control plane")

    owns_engine = engine is None
    runtime_engine = engine or create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        hide_parameters=True,
    )
    observability = ObservabilityRuntime(settings=settings)
    configure_structured_logging(settings)
    try:
        audit = SQLAlchemyAuditLog(
            engine=runtime_engine,
            keyring=AuditSigningKeyring.from_settings(settings),
        )
        memory_keyring = TenantMemoryKeyring.from_settings(settings)
        memory_service = MemoryService(
            repository=SQLAlchemyMemoryRepository(runtime_engine),
            keyring=memory_keyring,
            policy=PolicyEngine(),
            admission=MemoryAdmissionPolicy(),
            audit=audit,
        )
        memory_lifecycle_service = MemoryLifecycleService(engine=runtime_engine, audit_log=audit)
        semantic_checkpoint_service = SemanticCheckpointService(
            engine=runtime_engine,
            keyring=SemanticCheckpointKeyring.from_settings(settings),
            audit=audit,
        )
        artifact_repository = SQLAlchemyArtifactRepository(engine=runtime_engine, audit_log=audit)
        artifact_content_service = None
        if settings.artifact_store_root:
            artifact_content_service = ArtifactContentService(
                artifact_repository,
                LocalImmutableArtifactStore(
                    Path(settings.artifact_store_root),
                    max_object_bytes=settings.artifact_max_upload_bytes,
                ),
            )
        connector_registry = SQLAlchemyConnectorRegistry(engine=runtime_engine, audit_log=audit)
        product_account_service = ProductAccountService(runtime_engine)
        project_directory_service = ProjectDirectoryService(runtime_engine)
        project_resource_service = ProjectResourceService(
            runtime_engine, artifact_repository=artifact_repository
        )
        notification_service = NotificationService(runtime_engine)
        team_collaboration_service = TeamCollaborationService(
            runtime_engine, notifier=notification_service
        )
        project_workspace_service = ProjectWorkspaceService(runtime_engine)
        agent_exchange_service = AgentExchangeService(runtime_engine)
        project_work_graph_service = ProjectWorkGraphService(
            SQLAlchemyWorkGraphRepository(runtime_engine)
        )
        project_planning_service = ProjectPlanningService(
            runtime_engine,
            collaboration=team_collaboration_service,
            work_graph=project_work_graph_service,
        )
        if settings.auth_mode == "local":
            ensure_local_demo(engine=runtime_engine)
        # Capability projection and the signed skill catalog come from
        # deployment configuration; invalid skill configuration fails closed
        # instead of silently exposing an empty catalog.
        from ..tool_catalog import build_builtin_manifests
        from ..worker_main import build_skill_catalog_from_settings

        skill_catalog = build_skill_catalog_from_settings(settings)
        sandbox_profile_ids = ()
        if settings.sandbox_profiles_json:
            from ..sandbox import load_code_profiles

            sandbox_profile_ids = tuple(
                profile.profile_id for profile in load_code_profiles(settings.sandbox_profiles_json)
            )
        from .agent_capabilities import AgentCapabilityService

        agent_capabilities = AgentCapabilityService(
            manifests=build_builtin_manifests(
                sandbox_profile_ids=sandbox_profile_ids,
                office_connector_configured=bool(settings.connectors_json),
            ),
            skill_catalog=skill_catalog,
            policy=PolicyEngine(),
            llm_providers=settings.llm_providers,
        )
        contract_service = ContractCoordinationService(
            SQLAlchemyContractRepository(engine=runtime_engine, audit_log=audit)
        )
        governance_service = GovernanceService(
            SQLAlchemyGovernanceRepository(
                engine=runtime_engine,
                audit_log=audit,
            ),
            impact_guard=contract_service,
            artifact_guard=ArtifactDeliveryGuard(artifact_repository),
        )
        task_repository = SQLAlchemyTaskRepository(
            engine=runtime_engine,
            audit_log=audit,
        )
        task_execution_service = TaskExecutionService(
            repository=task_repository,
            governance=governance_service,
        )
        approval_service = ApprovalService(
            SQLAlchemyApprovalRepository(
                engine=runtime_engine,
                audit_log=audit,
            )
        )
        agent_run_service = AgentRunService(
            SQLAlchemyAgentRunRepository(
                engine=runtime_engine,
                keyring=AgentCheckpointKeyring.from_settings(settings),
                control_keyring=AgentControlKeyring.from_settings(settings),
                audit_log=audit,
            ),
            approval_service=approval_service,
        )
        _projection = AgentTurnProjection(
            runtime_engine,
            workspace=project_workspace_service,
            planning=project_planning_service,
            exchange=agent_exchange_service,
            run_reader=_run_conversation_reader(agent_run_service),
            context_reader=_run_context_reader(agent_run_service),
        )
        agent_run_service.terminal_callback = _projection.on_run_terminal
        capability_service = CapabilityDirectoryService(
            SQLAlchemyCapabilityRepository(engine=runtime_engine, audit_log=audit)
        )
        collaboration_transport = None
        if settings.envelope_signing_key is not None or settings.envelope_keys:
            collaboration_transport = DurableCollaborationTransport(
                engine=runtime_engine,
                audit_log=audit,
                codec=SignedEnvelopeCodec.from_settings(settings),
            )
        effective_probe = readiness_probe or DatabaseReadinessProbe(runtime_engine)
        session_lifecycle = SessionLifecycleService(settings=settings)
        shutdown_callbacks = ((runtime_engine.dispose,) if owns_engine else ()) + (
            observability.shutdown,
            session_lifecycle.aclose,
        )
        code_workspace_service = CodeWorkspaceService(
            engine=runtime_engine,
            audit_log=audit,
            workspace_root=(
                Path(settings.sandbox_workspace_root) if settings.sandbox_workspace_root else None
            ),
        )
        document_workspace_service = DocumentWorkspaceService(
            engine=runtime_engine,
            resource_service=project_resource_service,
            content_service=artifact_content_service,
            audit_log=audit,
        )
        app = create_app(
            settings=settings,
            verifier=verifier,
            memory_service=memory_service,
            memory_lifecycle_service=memory_lifecycle_service,
            semantic_checkpoint_service=semantic_checkpoint_service,
            artifact_repository=artifact_repository,
            artifact_content_service=artifact_content_service,
            connector_registry=connector_registry,
            product_account_service=product_account_service,
            project_directory_service=project_directory_service,
            project_resource_service=project_resource_service,
            team_collaboration_service=team_collaboration_service,
            project_workspace_service=project_workspace_service,
            agent_exchange_service=agent_exchange_service,
            project_planning_service=project_planning_service,
            notification_service=notification_service,
            agent_capabilities=agent_capabilities,
            skill_catalog=skill_catalog,
            governance_service=governance_service,
            task_execution_service=task_execution_service,
            approval_service=approval_service,
            agent_run_service=agent_run_service,
            capability_service=capability_service,
            code_workspace_service=code_workspace_service,
            document_workspace_service=document_workspace_service,
            session_lifecycle=session_lifecycle,
            observability=observability,
            readiness_probe=effective_probe,
            shutdown_callbacks=shutdown_callbacks,
        )
    except Exception:
        observability.shutdown()
        if owns_engine:
            runtime_engine.dispose()
        raise

    app.state.database_engine = runtime_engine
    app.state.audit_log = audit
    app.state.turn_projection = _projection
    app.state.governance_service = governance_service
    app.state.task_repository = task_repository
    app.state.task_execution_service = task_execution_service
    app.state.approval_service = approval_service
    app.state.agent_run_service = agent_run_service
    app.state.capability_service = capability_service
    app.state.memory_lifecycle_service = memory_lifecycle_service
    app.state.semantic_checkpoint_service = semantic_checkpoint_service
    app.state.artifact_repository = artifact_repository
    app.state.artifact_content_service = artifact_content_service
    app.state.connector_registry = connector_registry
    app.state.product_account_service = product_account_service
    app.state.project_directory_service = project_directory_service
    app.state.project_resource_service = project_resource_service
    app.state.team_collaboration_service = team_collaboration_service
    app.state.project_workspace_service = project_workspace_service
    app.state.agent_exchange_service = agent_exchange_service
    app.state.project_planning_service = project_planning_service
    app.state.project_work_graph_service = project_work_graph_service
    app.state.collaboration_transport = collaboration_transport
    app.state.code_workspace_service = code_workspace_service
    app.state.document_workspace_service = document_workspace_service
    app.state.session_lifecycle = session_lifecycle
    app.state.database_readiness = effective_probe
    app.state.observability = observability
    return app


def create_application() -> FastAPI:
    """Uvicorn application factory using environment-backed configuration."""
    return build_application(settings=load_environment_settings())
