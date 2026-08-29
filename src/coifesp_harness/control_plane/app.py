from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..auth import OIDCVerifier
from ..config import Environment, Settings
from ..errors import (
    AuthenticationError,
    HarnessError,
    IdentityProviderUnavailable,
    PolicyDenied,
    ResourceNotFound,
)
from ..observability import ObservabilityMiddleware, ObservabilityRuntime
from ..product import (
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    ProjectResourceService,
    TeamCollaborationService,
)
from ..product.auth import BuiltinAccountVerifier
from ..product.exchange import AgentExchangeService
from ..product.planning import ProjectPlanningService
from ..product.workspace import ProjectWorkspaceService
from .agent_capabilities import AgentCapabilityService
from .agent_capability_routes import build_agent_capability_router
from .agent_control_routes import build_agent_control_router
from .agent_run_routes import build_agent_run_router
from .approval_routes import build_approval_router
from .artifact_routes import build_artifact_router
from .auth import Authenticated, BearerAuthenticator
from .capability_routes import build_capability_router
from .checkpoint_routes import build_checkpoint_router
from .code_workspace_routes import build_code_workspace_router
from .connector_routes import build_connector_router
from .conversation_routes import build_conversation_router
from .document_workspace_routes import build_document_workspace_router
from .exchange_routes import build_exchange_router
from .execution_routes import build_execution_router
from .governance_routes import build_governance_router
from .local_identity import LocalIdentityProvider
from .memory_routes import build_memory_router
from .middleware import BoundedBodyMiddleware, RequestContextMiddleware
from .models import HealthResponse, IdentityResponse
from .planning_routes import build_planning_router
from .product_routes import build_product_router
from .workspace_routes import build_workspace_router

logger = logging.getLogger("coifesp.control_plane")


def create_app(
    *,
    settings: Settings,
    verifier: OIDCVerifier | None = None,
    memory_service=None,
    governance_service=None,
    task_execution_service=None,
    approval_service=None,
    agent_run_service=None,
    capability_service=None,
    memory_lifecycle_service=None,
    semantic_checkpoint_service=None,
    artifact_repository=None,
    artifact_content_service=None,
    connector_registry=None,
    product_account_service: ProductAccountService | None = None,
    project_directory_service: ProjectDirectoryService | None = None,
    project_resource_service: ProjectResourceService | None = None,
    team_collaboration_service: TeamCollaborationService | None = None,
    project_workspace_service: ProjectWorkspaceService | None = None,
    agent_exchange_service: AgentExchangeService | None = None,
    project_planning_service: ProjectPlanningService | None = None,
    notification_service: NotificationService | None = None,
    agent_capabilities: AgentCapabilityService | None = None,
    skill_catalog=None,
    code_workspace_service=None,
    document_workspace_service=None,
    session_lifecycle=None,
    observability: ObservabilityRuntime | None = None,
    readiness_probe: Callable[[], bool | None] | None = None,
    shutdown_callbacks: Sequence[Callable[[], object]] = (),
) -> FastAPI:
    settings.validate(require_auth=True)
    local_identity_provider = LocalIdentityProvider() if settings.auth_mode == "local" else None
    if verifier is not None:
        identity_verifier = verifier
    elif settings.auth_mode == "builtin":
        if product_account_service is None:
            raise ValueError("builtin authentication requires product account service")
        identity_verifier = BuiltinAccountVerifier(product_account_service)
    elif local_identity_provider is not None:
        identity_verifier = local_identity_provider
    else:
        identity_verifier = OIDCVerifier(settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if readiness_probe is not None:
                ready = await run_in_threadpool(readiness_probe)
                if ready is False:
                    raise RuntimeError("control-plane startup readiness check failed")
            # Crash recovery: replay terminal runs whose conversation turn is
            # still active so an interrupted projection is not lost forever.
            agent_run_service = getattr(app.state, "agent_run_service", None)
            if agent_run_service is not None:
                for name in ("turn_projection", "project_planner_projection"):
                    projection = getattr(app.state, name, None)
                    if projection is not None:
                        await run_in_threadpool(
                            projection.replay_pending, agent_run_service
                        )
            yield
        finally:
            for callback in reversed(tuple(shutdown_callbacks)):
                try:
                    result = callback()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logger.error(
                        "control-plane shutdown callback failed error_type=%s",
                        type(exc).__name__,
                    )
            if verifier is None:
                await identity_verifier.aclose()

    expose_docs = settings.environment is not Environment.PRODUCTION
    app = FastAPI(
        title="COIFESP Harness Control Plane",
        version="0.1.0",
        docs_url="/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if expose_docs else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.oidc_verifier = identity_verifier
    app.state.local_identity_provider = local_identity_provider
    app.state.memory_service = memory_service
    app.state.governance_service = governance_service
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
    app.state.notification_service = notification_service
    app.state.code_workspace_service = code_workspace_service
    app.state.document_workspace_service = document_workspace_service
    app.state.session_lifecycle = session_lifecycle
    app.state.observability = observability
    app.state.readiness_probe = readiness_probe
    authenticator = BearerAuthenticator(identity_verifier)

    app.add_middleware(
        BoundedBodyMiddleware,
        max_body_bytes=1_048_576,
        artifact_upload_bytes=(
            settings.artifact_max_upload_bytes if artifact_content_service is not None else None
        ),
    )
    app.add_middleware(RequestContextMiddleware, oidc_issuer=settings.oidc_issuer)
    if observability is not None:
        app.add_middleware(ObservabilityMiddleware, runtime=observability)
    _install_error_handlers(app)

    @app.get(
        "/health/live",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def ready(response: Response) -> HealthResponse:
        if readiness_probe is None:
            return HealthResponse(status="ready")
        try:
            is_ready = await run_in_threadpool(readiness_probe)
        except Exception as exc:
            logger.warning(
                "control-plane readiness check failed error_type=%s",
                type(exc).__name__,
            )
            response.status_code = 503
            return HealthResponse(status="unavailable")
        if is_ready is False:
            response.status_code = 503
            return HealthResponse(status="unavailable")
        return HealthResponse(status="ready")

    @app.get("/v1/auth/me", response_model=IdentityResponse)
    async def current_identity(
        authenticated: Authenticated = Depends(authenticator),
    ) -> IdentityResponse:
        identity = authenticated.identity
        principal = identity.principal
        return IdentityResponse(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            roles=sorted(principal.roles),
            clearance=principal.clearance.name.lower(),
            compartments=sorted(principal.compartments),
            expires_at=identity.expires_at,
            token_id=identity.token_id,
        )

    app.include_router(build_memory_router(authenticator=authenticator))
    app.include_router(build_governance_router(authenticator=authenticator))
    app.include_router(build_execution_router(authenticator=authenticator))
    app.include_router(build_approval_router(authenticator=authenticator))
    app.include_router(build_agent_run_router(authenticator=authenticator))
    app.include_router(build_agent_control_router(authenticator=authenticator))
    app.include_router(build_capability_router(authenticator=authenticator))
    app.include_router(build_checkpoint_router(authenticator=authenticator))
    app.include_router(build_artifact_router(authenticator=authenticator))
    app.include_router(build_connector_router(authenticator=authenticator))
    if product_account_service is not None and project_directory_service is not None:
        app.include_router(build_product_router(authenticator=authenticator,
            accounts=product_account_service, directory=project_directory_service,
            resources=project_resource_service,
            collaboration=team_collaboration_service,
            notifications=notification_service))
    if project_workspace_service is not None:
        app.include_router(build_conversation_router(authenticator=authenticator))
    if agent_exchange_service is not None:
        app.include_router(build_exchange_router(authenticator=authenticator))
    if project_planning_service is not None:
        app.include_router(build_planning_router(authenticator=authenticator))
    if code_workspace_service is not None:
        app.include_router(build_code_workspace_router(authenticator=authenticator))
    if document_workspace_service is not None:
        app.include_router(build_document_workspace_router(authenticator=authenticator))
    if agent_capabilities is not None:
        app.include_router(build_agent_capability_router(
            authenticator=authenticator,
            capabilities=agent_capabilities,
            skill_catalog=skill_catalog,
        ))
        app.state.agent_capabilities = agent_capabilities
        app.state.skill_catalog = skill_catalog
    app.include_router(build_workspace_router())
    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationError)
    async def authentication_error(
        request: Request,
        _: AuthenticationError,
    ) -> JSONResponse:
        return _problem(
            request,
            status=401,
            problem_type="authentication-failed",
            title="Authentication failed",
            detail="A valid bearer token is required.",
            headers={"WWW-Authenticate": ('Bearer realm="coifesp", error="invalid_token"')},
        )

    @app.exception_handler(IdentityProviderUnavailable)
    async def identity_provider_unavailable(
        request: Request,
        _: IdentityProviderUnavailable,
    ) -> JSONResponse:
        return _problem(
            request,
            status=503,
            problem_type="identity-provider-unavailable",
            title="Identity provider unavailable",
            detail="Authentication cannot be verified safely right now.",
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(PolicyDenied)
    async def policy_denied(
        request: Request,
        _: PolicyDenied,
    ) -> JSONResponse:
        return _problem(
            request,
            status=403,
            problem_type="access-denied",
            title="Access denied",
            detail="The authenticated principal is not authorized.",
        )

    @app.exception_handler(ResourceNotFound)
    async def resource_not_found(
        request: Request,
        _: ResourceNotFound,
    ) -> JSONResponse:
        return _problem(
            request,
            status=404,
            problem_type="resource-not-found",
            title="Resource not found",
            detail="The requested resource is not available.",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        errors = [
            {
                "location": ".".join(str(part) for part in item["loc"]),
                "type": item["type"],
                "message": item["msg"],
            }
            for item in error.errors()
        ]
        return _problem(
            request,
            status=422,
            problem_type="validation-failed",
            title="Request validation failed",
            detail="One or more request fields are invalid.",
            errors=errors,
        )

    @app.exception_handler(HarnessError)
    async def harness_error(
        request: Request,
        _: HarnessError,
    ) -> JSONResponse:
        return _problem(
            request,
            status=409,
            problem_type="operation-rejected",
            title="Operation rejected",
            detail="The requested operation could not be completed.",
        )

    @app.exception_handler(Exception)
    async def unexpected_error(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        logger.exception(
            "unhandled control-plane error request_id=%s error_type=%s",
            _request_id(request),
            type(error).__name__,
        )
        return _problem(
            request,
            status=500,
            problem_type="internal-error",
            title="Internal server error",
            detail="The server could not complete the request.",
        )


def _problem(
    request: Request,
    *,
    status: int,
    problem_type: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    body = {
        "type": f"https://coifesp.dev/problems/{problem_type}",
        "title": title,
        "status": status,
        "detail": detail,
        "request_id": _request_id(request),
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")
