import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.connectors import SQLAlchemyConnectorRegistry
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import AuthenticationError
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal


class Verifier:
    def __init__(self, values):
        self.values = values

    async def verify(self, token):
        if token not in self.values:
            raise AuthenticationError()
        return self.values[token]

    async def aclose(self):
        pass


def identity(pid, tenant="team-a", roles=("contributor",)):
    return VerifiedIdentity(
        Principal(pid, tenant, frozenset(roles), Classification.RESTRICTED, frozenset()),
        "https://id.test",
        "control",
        datetime.now(UTC) + timedelta(minutes=5),
        None,
    )


async def call(app, method, path, token=None, **kwargs):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://control.test",
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def stack():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    audit = SQLAlchemyAuditLog(
        engine=engine, keyring=AuditSigningKeyring(active_key_id="a", verification_keys={"a": b"a" * 32})
    )
    audit.create_schema()
    registry = SQLAlchemyConnectorRegistry(engine=engine, audit_log=audit)
    registry.create_schema()
    settings = Settings.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_OIDC_ISSUER": "https://id.test",
            "COIFESP_OIDC_AUDIENCE": "control",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "client",
            "COIFESP_OIDC_JWKS_URL": "https://id.test/jwks",
        }
    )
    app = create_app(
        settings=settings,
        verifier=Verifier(
            {
                "admin": identity("admin", roles=("connector_administrator",)),
                "reviewer": identity("reviewer", roles=("connector_reviewer",)),
                "member": identity("member", roles=("contributor",)),
                "other-team": identity("other", tenant="team-b", roles=("contributor",)),
            }
        ),
        connector_registry=registry,
    )
    return app, registry


def proposal(connector_id="office-main"):
    return {
        "connector_id": connector_id,
        "base_url": "https://api.office.test",
        "token_endpoint": "https://id.office.test/token",
        "client_id": "client",
        "client_secret_env": "COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET",
        "scopes": ["message.send"],
        "allowed_paths": ["/v1/messages"],
        "max_classification": "internal",
        "timeout_seconds": 15,
        "max_response_bytes": 1048576,
        "max_attempts": 3,
        "circuit_failure_threshold": 5,
        "circuit_cooldown_seconds": 30,
    }


def test_available_reports_not_configured_without_any_connector():
    app, _ = stack()
    response = asyncio.run(call(app, "GET", "/v1/connectors/available", token="member"))
    assert response.status_code == 200
    assert response.json() == []


def test_available_tracks_pending_and_active_lifecycle_without_secrets():
    app, _ = stack()
    proposed = asyncio.run(
        call(app, "POST", "/v1/connectors", "admin", json=proposal())
    )
    assert proposed.status_code == 202
    pending = asyncio.run(call(app, "GET", "/v1/connectors/available", token="member"))
    assert pending.status_code == 200
    assert pending.json() == [
        {
            "connector_id": "office-main",
            "auth_type": "client_credentials",
            "status": "pending",
            "proposed_action": "activate",
            "version": 1,
            "created_at": pending.json()[0]["created_at"],
            "reviewed_at": None,
        }
    ]
    # No endpoint URLs, client ids or secret references are projected.
    assert "api.office.test" not in pending.text
    assert "CLIENT_SECRET" not in pending.text
    assert "client_id" not in pending.text

    approved = asyncio.run(
        call(
            app,
            "POST",
            "/v1/connectors/office-main/revisions/1:review",
            "reviewer",
            json={"approve": True, "reason": "reviewed"},
        )
    )
    assert approved.status_code == 200
    active = asyncio.run(call(app, "GET", "/v1/connectors/available", token="member"))
    assert active.json()[0]["status"] == "active"


def test_available_is_isolated_per_tenant():
    app, _ = stack()
    asyncio.run(call(app, "POST", "/v1/connectors", "admin", json=proposal()))
    other = asyncio.run(call(app, "GET", "/v1/connectors/available", token="other-team"))
    assert other.status_code == 200
    assert other.json() == []
    assert "office-main" not in other.text


def test_available_requires_authentication():
    app, _ = stack()
    response = asyncio.run(call(app, "GET", "/v1/connectors/available"))
    assert response.status_code == 401


def test_available_lists_multiple_connectors_stably():
    app, _ = stack()
    asyncio.run(call(app, "POST", "/v1/connectors", "admin", json=proposal("b-connector")))
    asyncio.run(call(app, "POST", "/v1/connectors", "admin", json=proposal("a-connector")))
    response = asyncio.run(call(app, "GET", "/v1/connectors/available", token="member"))
    ids = [item["connector_id"] for item in response.json()]
    assert ids == ["a-connector", "b-connector"]
