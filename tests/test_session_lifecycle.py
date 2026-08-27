import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.auth import FetchResult
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.control_plane.session_lifecycle import SessionLifecycleService
from coifesp_harness.errors import IdentityProviderUnavailable
from coifesp_harness.product import ProductAccountService, ProjectDirectoryService


def builtin_app():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    app = create_app(
        settings=Settings.from_environment({"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}),
        product_account_service=accounts,
        project_directory_service=ProjectDirectoryService(engine),
    )
    return app


def local_app():
    return create_app(
        settings=Settings.from_environment({"COIFESP_ENV": "development", "COIFESP_AUTH_MODE": "local"})
    )


async def call(app, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def register_admin(app):
    team = asyncio.run(
        call(app, "POST", "/v1/teams/register", json={"handle": "alpha-team", "name": "Alpha Team"})
    )
    details = team.json()
    return (
        details["administrator_username"],
        details["administrator_initial_password"],
        details["team"]["team_id"],
    )


def admin_login(app, username, initial_password):
    updated = asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": username,
                "current_password": initial_password,
                "new_password": "correct-horse-battery-staple-42",
            },
        )
    )
    assert updated.status_code == 204
    session = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": username, "password": "correct-horse-battery-staple-42"},
        )
    )
    assert session.status_code == 200, session.text
    return session.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_builtin_session_renewal_rotates_token_and_extends_expiry():
    app = builtin_app()
    username, initial_password, _ = register_admin(app)
    login = admin_login(app, username, initial_password)
    old_token = login["access_token"]
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(old_token))).status_code == 200

    renewed = asyncio.run(
        call(app, "POST", "/v1/sessions/current:renew", headers=bearer(old_token))
    )
    assert renewed.status_code == 200, renewed.text
    body = renewed.json()
    new_token = body["access_token"]
    assert new_token != old_token
    new_expiry = datetime.fromisoformat(body["expires_at"])
    old_expiry = datetime.fromisoformat(login["expires_at"])
    assert new_expiry > old_expiry.replace(tzinfo=UTC)
    # The new token works, the old token is revoked immediately.
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(new_token))).status_code == 200
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(old_token))).status_code == 401
    # The old token can no longer be renewed either.
    stale = asyncio.run(
        call(app, "POST", "/v1/sessions/current:renew", headers=bearer(old_token))
    )
    assert stale.status_code == 401
    # Renewing with the fresh token keeps the session alive.
    again = asyncio.run(
        call(app, "POST", "/v1/sessions/current:renew", headers=bearer(new_token))
    )
    assert again.status_code == 200


def test_builtin_renewal_rejects_invalid_or_missing_credentials():
    app = builtin_app()
    missing = asyncio.run(call(app, "POST", "/v1/sessions/current:renew"))
    assert missing.status_code == 401
    forged = asyncio.run(
        call(app, "POST", "/v1/sessions/current:renew", headers=bearer("not-a-real-token"))
    )
    assert forged.status_code == 401


def test_builtin_global_logout_revokes_every_session():
    app = builtin_app()
    username, initial_password, _ = register_admin(app)
    login = admin_login(app, username, initial_password)
    first = login["access_token"]
    # A second independent session for the same account.
    second_login = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": username, "password": "correct-horse-battery-staple-42"},
        )
    ).json()
    second = second_login["access_token"]
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(first))).status_code == 200
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(second))).status_code == 200

    revoked = asyncio.run(call(app, "POST", "/v1/sessions:revoke-all", headers=bearer(first)))
    assert revoked.status_code == 204
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(first))).status_code == 401
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(second))).status_code == 401


def test_local_session_renewal_and_revoke():
    app = local_app()
    login = asyncio.run(
        call(app, "POST", "/app/local-session", json={"profile_id": "lead"})
    )
    assert login.status_code == 200, login.text
    old_token = login.json()["access_token"]
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(old_token))).status_code == 200

    renewed = asyncio.run(
        call(app, "POST", "/app/local-session:renew", headers=bearer(old_token))
    )
    assert renewed.status_code == 200, renewed.text
    new_token = renewed.json()["access_token"]
    assert new_token != old_token
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(new_token))).status_code == 200
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(old_token))).status_code == 401

    revoked = asyncio.run(
        call(app, "POST", "/app/local-session:revoke", headers=bearer(new_token))
    )
    assert revoked.status_code == 204
    assert asyncio.run(call(app, "GET", "/v1/auth/me", headers=bearer(new_token))).status_code == 401


def test_local_session_renewal_rejects_unknown_token():
    app = local_app()
    response = asyncio.run(
        call(app, "POST", "/app/local-session:renew", headers=bearer("bogus"))
    )
    assert response.status_code == 401


ISSUER = "https://identity.example.test"
AUDIENCE = "coifesp-control-plane"


class StubFetcher:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        pass


def oidc_settings():
    return Settings.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_OIDC_ISSUER": ISSUER,
            "COIFESP_OIDC_AUDIENCE": AUDIENCE,
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "coifesp-local-ui",
            "COIFESP_UI_OIDC_CLIENT_ID": "coifesp-local-ui",
        }
    )


def discovery(extra=None):
    document = {
        "issuer": ISSUER,
        "jwks_uri": f"{ISSUER}/jwks",
        "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
    }
    if extra:
        document.update(extra)
    return FetchResult(document=document, max_age_seconds=300)


def test_oidc_session_config_returns_end_session_endpoint_from_discovery():
    fetcher = StubFetcher(
        {f"{ISSUER}/.well-known/openid-configuration": discovery()}
    )
    service = SessionLifecycleService(settings=oidc_settings(), fetcher=fetcher)

    async def run():
        return await service.oidc_session_config(redirect_uri="https://app.test/app/")

    config = asyncio.run(run())
    assert config["auth_mode"] == "oidc"
    assert config["issuer"] == ISSUER
    assert config["client_id"] == "coifesp-local-ui"
    assert config["audience"] == AUDIENCE
    assert config["end_session_endpoint"] == f"{ISSUER}/protocol/openid-connect/logout"
    assert config["post_logout_redirect_uri"] == "https://app.test/app/"
    assert len(fetcher.calls) == 1
    # The discovery result is cached; a second call does not refetch.
    asyncio.run(service.oidc_session_config(redirect_uri="https://app.test/app/"))
    assert len(fetcher.calls) == 1


def test_oidc_session_config_fails_closed_on_issuer_mismatch():
    fetcher = StubFetcher(
        {f"{ISSUER}/.well-known/openid-configuration": discovery({"issuer": "https://evil.test"})}
    )
    service = SessionLifecycleService(settings=oidc_settings(), fetcher=fetcher)
    with pytest.raises(IdentityProviderUnavailable):
        asyncio.run(service.oidc_session_config(redirect_uri="https://app.test/app/"))


def test_oidc_session_config_rejects_unsafe_end_session_endpoint():
    fetcher = StubFetcher(
        {
            f"{ISSUER}/.well-known/openid-configuration": discovery(
                {"end_session_endpoint": "file:///etc/passwd"}
            )
        }
    )
    service = SessionLifecycleService(settings=oidc_settings(), fetcher=fetcher)
    with pytest.raises(IdentityProviderUnavailable):
        asyncio.run(service.oidc_session_config(redirect_uri="https://app.test/app/"))


def test_oidc_session_config_allows_missing_end_session_endpoint():
    fetcher = StubFetcher(
        {f"{ISSUER}/.well-known/openid-configuration": discovery({"end_session_endpoint": None})}
    )
    service = SessionLifecycleService(settings=oidc_settings(), fetcher=fetcher)
    config = asyncio.run(service.oidc_session_config(redirect_uri="https://app.test/app/"))
    assert config["end_session_endpoint"] is None


def test_workspace_config_returns_end_session_endpoint_when_wired():
    fetcher = StubFetcher(
        {f"{ISSUER}/.well-known/openid-configuration": discovery()}
    )
    service = SessionLifecycleService(settings=oidc_settings(), fetcher=fetcher)
    app = create_app(settings=oidc_settings(), verifier=None, session_lifecycle=service)
    response = asyncio.run(call(app, "GET", "/app/config"))
    assert response.status_code == 200
    body = response.json()
    assert body["end_session_endpoint"] == f"{ISSUER}/protocol/openid-connect/logout"
    assert "secret" not in response.text.lower()
