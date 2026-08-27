import asyncio

import httpx
import pytest

from coifesp_harness.auth import (
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    KeycloakDirectoryConfig,
    KeycloakPrincipalResolver,
)
from coifesp_harness.config import SecretValue
from coifesp_harness.errors import AuthenticationError, IntegrityError, PolicyDenied
from coifesp_harness.security import Classification


TOKEN_URL = "https://identity.example.test/realms/coifesp/protocol/openid-connect/token"


def token_config(**overrides):
    values = {
        "token_endpoint": TOKEN_URL,
        "client_id": "directory-reader",
        "client_secret": SecretValue("x" * 32),
        "refresh_skew_seconds": 10,
    }
    values.update(overrides)
    return ClientCredentialsConfig(**values)


def test_client_credentials_cache_is_concurrency_safe_and_secret_safe() -> None:
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        assert request.url == TOKEN_URL
        assert b"client_secret=" in await request.aread()
        return httpx.Response(
            200, json={"access_token": "token-" + "s" * 20, "token_type": "Bearer", "expires_in": 300}
        )

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ClientCredentialsTokenProvider(token_config(), client=client)
        values = await asyncio.gather(*(provider.token() for _ in range(20)))
        await client.aclose()
        return provider, values

    provider, values = asyncio.run(run())
    assert calls == 1
    assert len({value.reveal() for value in values}) == 1
    assert "token-" not in repr(provider._cached)


def test_client_credentials_rejects_invalid_client_without_response_leak() -> None:
    async def handler(_):
        return httpx.Response(401, json={"error": "invalid_client", "secret": "do-not-log"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ClientCredentialsTokenProvider(token_config(), client=client)
        try:
            with pytest.raises(AuthenticationError, match="invalid_client"):
                await provider.token()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_keycloak_resolver_maps_current_attributes_and_roles() -> None:
    async def handler(request):
        if request.url.path.endswith("/role-mappings/realm/composite"):
            return httpx.Response(200, json=[{"name": "contributor"}, {"name": "observer"}])
        if request.url.path.endswith("/groups"):
            assert request.url.params["briefRepresentation"] == "false"
            assert request.url.params["max"] == "129"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "group-a",
                        "attributes": {
                            "tenant_id": ["team-a"],
                            "clearance": ["confidential"],
                            "compartments": ["project-x"],
                        },
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "user-123",
                "enabled": True,
                "attributes": {
                    "compartments": ["contract-7"],
                },
            },
        )

    class Tokens:
        async def token(self):
            return SecretValue("directory-access-token")

        def invalidate(self):
            raise AssertionError("valid token must not be invalidated")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resolver = KeycloakPrincipalResolver(
            config=KeycloakDirectoryConfig(
                "https://identity.example.test", "coifesp"
            ),
            tokens=Tokens(),
            client=client,
        )
        try:
            return await resolver.resolve(tenant_id="team-a", principal_id="user-123")
        finally:
            await client.aclose()

    principal = asyncio.run(run())
    assert principal.principal_id == "user-123"
    assert principal.tenant_id == "team-a"
    assert principal.roles == frozenset({"contributor", "observer"})
    assert principal.clearance is Classification.CONFIDENTIAL
    assert principal.compartments == frozenset({"project-x", "contract-7"})
    assert principal.is_service is False


@pytest.mark.parametrize(
    ("user", "error"),
    [
        ({"id": "different", "enabled": True, "attributes": {}}, IntegrityError),
        (
            {
                "id": "user-123",
                "enabled": True,
                "serviceAccountClientId": "worker",
                "attributes": {},
            },
            PolicyDenied,
        ),
        (
            {
                "id": "user-123",
                "enabled": True,
                "attributes": {
                    "tenant_id": ["team-b"],
                    "clearance": ["internal"],
                },
            },
            IntegrityError,
        ),
    ],
)
def test_keycloak_resolver_fails_closed_on_identity_mismatch(user, error) -> None:
    async def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=user)

    class Tokens:
        async def token(self):
            return SecretValue("directory-access-token")

        def invalidate(self):
            pass

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resolver = KeycloakPrincipalResolver(
            config=KeycloakDirectoryConfig("https://identity.example.test", "coifesp"),
            tokens=Tokens(),
            client=client,
        )
        try:
            with pytest.raises(error):
                await resolver.resolve(tenant_id="team-a", principal_id="user-123")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_keycloak_resolver_rejects_conflicting_group_tenants() -> None:
    async def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(
                200,
                json=[
                    {"attributes": {"tenant_id": ["team-a"], "clearance": ["internal"]}},
                    {"attributes": {"tenant_id": ["team-b"]}},
                ],
            )
        if request.url.path.endswith("/role-mappings/realm/composite"):
            return httpx.Response(200, json=[{"name": "contributor"}])
        return httpx.Response(200, json={"id": "user-123", "enabled": True})

    class Tokens:
        async def token(self):
            return SecretValue("directory-access-token")

        def invalidate(self):
            pass

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resolver = KeycloakPrincipalResolver(
            config=KeycloakDirectoryConfig("https://identity.example.test", "coifesp"),
            tokens=Tokens(),
            client=client,
        )
        try:
            with pytest.raises(PolicyDenied, match="singular"):
                await resolver.resolve(tenant_id="team-a", principal_id="user-123")
        finally:
            await client.aclose()

    asyncio.run(run())
