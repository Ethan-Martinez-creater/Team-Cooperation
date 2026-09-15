import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import PolicyDenied, ResourceNotFound
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    TeamAccountRole,
)
from coifesp_harness.security import Classification, Principal


class StubVerifier:
    def __init__(self, identity: VerifiedIdentity) -> None:
        self.identity = identity

    async def verify(self, token: str) -> VerifiedIdentity:
        assert token == "federated-token"
        return self.identity


def identity(
    *, subject: str = "subject-1", tenant: str = "team-a", service: bool = False
):
    return VerifiedIdentity(
        principal=Principal(
            principal_id=subject,
            tenant_id=tenant,
            roles=frozenset({"agent_worker"} if service else {"lead"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset({"project-x"}),
            is_service=service,
        ),
        issuer="https://identity.example.test",
        audience="coifesp-control-plane",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        token_id="token-1",
    )


def services():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    return accounts, ProjectDirectoryService(engine)


async def get(app, path: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(
            path, headers={"Authorization": "Bearer federated-token"}
        )


def oidc_settings():
    return Settings.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_AUTH_MODE": "oidc",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test",
            "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
            "COIFESP_OIDC_JWKS_URL": "https://identity.example.test/jwks",
        }
    )


def test_oidc_human_is_provisioned_before_product_routes_run():
    accounts, directory = services()
    app = create_app(
        settings=oidc_settings(),
        verifier=StubVerifier(identity()),
        product_account_service=accounts,
        project_directory_service=directory,
    )

    response = asyncio.run(get(app, "/v1/projects"))

    assert response.status_code == 200
    account = accounts.get_account("subject-1")
    assert account.team_id == "team-a"
    assert account.team_role is TeamAccountRole.OWNER
    assert accounts.get_team("team-a").handle == "team-a"


def test_federated_accounts_are_idempotent_and_first_account_is_owner():
    accounts, _directory = services()
    first = accounts.ensure_federated_account(account_id="subject-1", team_id="team-a")
    repeated = accounts.ensure_federated_account(
        account_id="subject-1", team_id="team-a"
    )
    second = accounts.ensure_federated_account(account_id="subject-2", team_id="team-a")

    assert repeated == first
    assert first.team_role is TeamAccountRole.OWNER
    assert second.team_role is TeamAccountRole.MEMBER
    with pytest.raises(PolicyDenied):
        accounts.ensure_federated_account(account_id="subject-1", team_id="team-b")


def test_oidc_service_identity_is_not_provisioned_as_product_account():
    accounts, directory = services()
    app = create_app(
        settings=oidc_settings(),
        verifier=StubVerifier(
            identity(subject="worker-1", tenant="platform", service=True)
        ),
        product_account_service=accounts,
        project_directory_service=directory,
    )

    response = asyncio.run(get(app, "/v1/auth/me"))

    assert response.status_code == 200
    with pytest.raises(ResourceNotFound):
        accounts.get_account("worker-1")
