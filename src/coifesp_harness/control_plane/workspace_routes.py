from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from ..config import ConfigurationError
from ..errors import AuthenticationError

_ASSETS = Path(__file__).with_name("workspace_assets")


class LocalLoginBody(BaseModel):
    profile_id: str = Field(pattern=r"^(lead|contributor|reviewer)$")


def build_workspace_router() -> APIRouter:
    router = APIRouter(tags=["workspace"])

    @router.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=307)

    @router.get("/app/", include_in_schema=False)
    async def workspace() -> FileResponse:
        return FileResponse(_ASSETS / "index.html", media_type="text/html")

    @router.get("/app/app.css", include_in_schema=False)
    async def stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "app.css", media_type="text/css")

    @router.get("/app/agent-workbench.css", include_in_schema=False)
    async def agent_workbench_stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "agent-workbench.css", media_type="text/css")

    @router.get("/app/project-resources.css", include_in_schema=False)
    async def project_resources_stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "project-resources.css", media_type="text/css")

    @router.get("/app/product-auth.css", include_in_schema=False)
    async def product_auth_stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "product-auth.css", media_type="text/css")

    @router.get("/app/blue-theme.css", include_in_schema=False)
    async def blue_theme_stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "blue-theme.css", media_type="text/css")

    @router.get("/app/app.js", include_in_schema=False)
    async def javascript() -> FileResponse:
        return FileResponse(_ASSETS / "app.js", media_type="text/javascript")

    @router.get("/app/project-workspace.js", include_in_schema=False)
    async def project_workspace_javascript() -> FileResponse:
        return FileResponse(_ASSETS / "project-workspace.js", media_type="text/javascript")

    @router.get("/app/project-workspace.css", include_in_schema=False)
    async def project_workspace_stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "project-workspace.css", media_type="text/css")

    @router.get("/app/session.js", include_in_schema=False)
    async def session_javascript() -> FileResponse:
        return FileResponse(_ASSETS / "session.js", media_type="text/javascript")

    @router.get("/app/silent-callback.html", include_in_schema=False)
    async def silent_callback_page() -> FileResponse:
        return FileResponse(_ASSETS / "silent-callback.html", media_type="text/html")

    @router.get("/app/silent-callback.js", include_in_schema=False)
    async def silent_callback_javascript() -> FileResponse:
        return FileResponse(_ASSETS / "silent-callback.js", media_type="text/javascript")

    @router.get("/app/config", include_in_schema=False)
    async def config(request: Request) -> JSONResponse:
        settings = request.app.state.settings
        workspace_enabled = (
            getattr(request.app.state, "project_workspace_service", None) is not None
        )
        if settings.auth_mode == "builtin":
            return JSONResponse(
                {
                    "auth_mode": "builtin",
                    "product_workspace_enabled": workspace_enabled,
                }
            )
        if settings.auth_mode == "local":
            provider = request.app.state.local_identity_provider
            return JSONResponse(
                {
                    "auth_mode": "local",
                    "product_workspace_enabled": workspace_enabled,
                    "profiles": [
                        {
                            "profile_id": item.profile_id,
                            "display_name": item.display_name,
                            "team_name": item.team_name,
                            "role": item.profile_id,
                        }
                        for item in provider.profiles.values()
                    ],
                }
            )
        client_id = settings.ui_oidc_client_id
        if not settings.oidc_issuer or not client_id:
            raise ConfigurationError("workspace OIDC configuration is unavailable")
        redirect_uri = str(request.url_for("workspace"))
        session_service = getattr(request.app.state, "session_lifecycle", None)
        if session_service is None:
            return JSONResponse(
                {
                    "auth_mode": "oidc",
                    "product_workspace_enabled": workspace_enabled,
                    "issuer": settings.oidc_issuer.rstrip("/"),
                    "client_id": client_id,
                    "audience": settings.oidc_audience,
                    "redirect_uri": redirect_uri,
                    "end_session_endpoint": None,
                    "post_logout_redirect_uri": redirect_uri,
                }
            )
        oidc_config = await session_service.oidc_session_config(redirect_uri=redirect_uri)
        oidc_config["product_workspace_enabled"] = workspace_enabled
        return JSONResponse(oidc_config)

    @router.post("/app/local-session", include_in_schema=False)
    async def local_session(body: LocalLoginBody, request: Request) -> JSONResponse:
        if request.app.state.settings.auth_mode != "local":
            raise ConfigurationError("local workspace login is disabled")
        token, identity = request.app.state.local_identity_provider.issue(body.profile_id)
        profile = request.app.state.local_identity_provider.profiles[body.profile_id]
        return JSONResponse(
            {
                "access_token": token,
                "expires_at": identity.expires_at.isoformat(),
                "profile": {
                    "display_name": profile.display_name,
                    "team_name": profile.team_name,
                },
            }
        )

    @router.post("/app/local-session:renew", include_in_schema=False)
    async def local_session_renew(request: Request) -> JSONResponse:
        if request.app.state.settings.auth_mode != "local":
            raise ConfigurationError("local workspace login is disabled")
        provider = request.app.state.local_identity_provider
        token = _bearer_token(request.headers.get("authorization"))
        new_token, identity = provider.renew(token)
        return JSONResponse(
            {
                "access_token": new_token,
                "expires_at": identity.expires_at.isoformat(),
            }
        )

    @router.post("/app/local-session:revoke", status_code=204, include_in_schema=False)
    async def local_session_revoke(request: Request, response: Response) -> None:
        if request.app.state.settings.auth_mode != "local":
            raise ConfigurationError("local workspace login is disabled")
        provider = request.app.state.local_identity_provider
        provider.revoke(_bearer_token(request.headers.get("authorization")))
        response.status_code = 204

    return router


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthenticationError("invalid_token")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or token.strip() != token
        or " " in token
    ):
        raise AuthenticationError("invalid_token")
    return token
