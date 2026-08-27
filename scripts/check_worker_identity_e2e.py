"""Verify configured Worker and directory OAuth identities against local Keycloak.

Secrets and tokens remain in memory. A uniquely named credential-less user is
created for the lookup and deleted by exact server-assigned id in a finally block.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.auth import (  # noqa: E402
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    KeycloakDirectoryConfig,
    KeycloakPrincipalResolver,
    OIDCVerifier,
    OIDCWorkerIdentityProvider,
)
from coifesp_harness.config import Environment  # noqa: E402
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402


KEYCLOAK_ENV = Path(r"E:\keyclock\runtime\keycloak.env")
BASE_URL = "http://127.0.0.1:8080"
REALM = "coifesp"


class AcceptanceFailure(RuntimeError):
    pass


def require(value: bool, message: str) -> None:
    if not value:
        raise AcceptanceFailure(message)


def keycloak_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in KEYCLOAK_ENV.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for name in ("KC_BOOTSTRAP_ADMIN_USERNAME", "KC_BOOTSTRAP_ADMIN_PASSWORD"):
        require(bool(values.get(name)), f"{name} is absent")
    return values


def admin_token(client: httpx.Client, values: dict[str, str]) -> str:
    response = client.post(
        f"{BASE_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": values["KC_BOOTSTRAP_ADMIN_USERNAME"],
            "password": values["KC_BOOTSTRAP_ADMIN_PASSWORD"],
        },
    )
    require(response.status_code == 200, f"admin authentication failed with HTTP {response.status_code}")
    token = response.json().get("access_token")
    require(isinstance(token, str) and bool(token), "admin access token is absent")
    return token


def create_owner(client: httpx.Client, token: str) -> str:
    username = f"coifesp-worker-e2e-{uuid.uuid4().hex}"
    response = client.post(
        f"{BASE_URL}/admin/realms/{REALM}/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": username,
            "enabled": True,
            "emailVerified": False,
        },
    )
    require(response.status_code == 201, f"temporary owner creation failed with HTTP {response.status_code}")
    location = response.headers.get("location", "")
    user_id = location.rsplit("/", 1)[-1]
    require(bool(user_id) and user_id != location, "temporary owner id is absent")
    role = client.get(
        f"{BASE_URL}/admin/realms/{REALM}/roles/contributor",
        headers={"Authorization": f"Bearer {token}"},
    )
    require(role.status_code == 200, f"contributor role lookup failed with HTTP {role.status_code}")
    assigned = client.post(
        f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}/role-mappings/realm",
        headers={"Authorization": f"Bearer {token}"},
        json=[role.json()],
    )
    require(assigned.status_code == 204, f"temporary owner role assignment failed with HTTP {assigned.status_code}")
    groups = client.get(
        f"{BASE_URL}/admin/realms/{REALM}/groups",
        params={"search": "team-a", "exact": "true"},
        headers={"Authorization": f"Bearer {token}"},
    )
    require(groups.status_code == 200, f"team group lookup failed with HTTP {groups.status_code}")
    matches = [item for item in groups.json() if item.get("name") == "team-a"]
    require(len(matches) == 1, "team-a group is absent or ambiguous")
    group_id = matches[0].get("id")
    require(isinstance(group_id, str) and bool(group_id), "team-a group id is absent")
    membership = client.put(
        f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}/groups/{group_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    require(membership.status_code == 204, f"team membership failed with HTTP {membership.status_code}")
    return user_id


def delete_owner(client: httpx.Client, token: str, user_id: str) -> None:
    response = client.delete(
        f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    require(response.status_code == 204, f"temporary owner cleanup failed with HTTP {response.status_code}")


async def accept(user_id: str) -> None:
    settings = load_environment_settings(PROJECT_ROOT / ".env")
    settings.validate(require_auth=True, require_worker=True)
    allow_http = settings.environment is not Environment.PRODUCTION
    assert settings.worker_token_endpoint is not None
    assert settings.worker_client_id is not None
    assert settings.worker_client_secret is not None
    assert settings.worker_tenant_id is not None
    assert settings.directory_token_endpoint is not None
    assert settings.directory_client_id is not None
    assert settings.directory_client_secret is not None
    assert settings.directory_api_base_url is not None
    assert settings.directory_realm is not None

    worker_tokens = ClientCredentialsTokenProvider(
        ClientCredentialsConfig(
            settings.worker_token_endpoint,
            settings.worker_client_id,
            settings.worker_client_secret,
            allow_insecure_http=allow_http,
        )
    )
    directory_tokens = ClientCredentialsTokenProvider(
        ClientCredentialsConfig(
            settings.directory_token_endpoint,
            settings.directory_client_id,
            settings.directory_client_secret,
            allow_insecure_http=allow_http,
        )
    )
    verifier = OIDCVerifier(settings=settings)
    directory = KeycloakPrincipalResolver(
        config=KeycloakDirectoryConfig(
            settings.directory_api_base_url,
            settings.directory_realm,
            allow_insecure_http=allow_http,
        ),
        tokens=directory_tokens,
    )
    try:
        worker = await OIDCWorkerIdentityProvider(
            tokens=worker_tokens,
            verifier=verifier,
            expected_tenant_id=settings.worker_tenant_id,
        ).resolve()
        require(worker.is_service and "agent_worker" in worker.roles, "worker identity mapping failed")
        owner = await directory.resolve(tenant_id="team-a", principal_id=user_id)
        require(not owner.is_service, "directory resolved owner as a service")
        require(owner.roles == frozenset({"contributor"}), "directory role mapping failed")
        require(owner.clearance.name.lower() == "internal", "directory clearance mapping failed")
        require(owner.compartments == frozenset({"project-x"}), "directory compartment mapping failed")

        directory_token = await directory_tokens.token()
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            denied = await client.get(
                f"{BASE_URL}/admin/realms/{REALM}/clients",
                headers={"Authorization": f"Bearer {directory_token.reveal()}"},
            )
        require(denied.status_code == 403, "directory client has excess client-management visibility")
    finally:
        await directory.aclose()
        await directory_tokens.aclose()
        await worker_tokens.aclose()
        await verifier.aclose()


def main() -> int:
    values = keycloak_environment()
    user_id: str | None = None
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        token = admin_token(client, values)
        try:
            user_id = create_owner(client, token)
            asyncio.run(accept(user_id))
        finally:
            if user_id is not None:
                delete_owner(client, token, user_id)
    print(
        "WORKER_IDENTITY_E2E_OK worker_token=yes oidc=yes directory_token=yes "
        "live_owner=yes least_privilege=yes temporary_owner=removed secrets=redacted"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceFailure as exc:
        print(f"WORKER_IDENTITY_E2E_FAILED reason={exc} secrets=redacted")
        raise SystemExit(1) from None
