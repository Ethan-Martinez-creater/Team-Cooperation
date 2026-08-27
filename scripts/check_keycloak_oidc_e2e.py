"""Run a secret-safe Keycloak-to-Harness OIDC acceptance check.

The check uses the existing Keycloak bootstrap administrator only to configure the
dedicated worker service account. Passwords, client secrets, and tokens remain in
memory and are never printed or written to disk.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.auth import OIDCVerifier
from coifesp_harness.control_plane.app import create_app
from coifesp_harness.control_plane.bootstrap import load_environment_settings


KEYCLOAK_ENV = Path(r"E:\keyclock\runtime\keycloak.env")
BASE_URL = "http://127.0.0.1:8080"
REALM = "coifesp"
CLIENT_ID = "coifesp-agent-worker"
DIRECTORY_CLIENT_ID = "coifesp-directory-reader"
APPLICATION_ROLES = {
    "lead": "Plans and governs collaborative work",
    "contributor": "Accepts and delivers assigned work",
    "reviewer": "Reviews collaborative deliverables",
    "observer": "Read-only collaboration participant",
    "tool_approver": "Approves bound high-risk tool executions",
    "memory_curator": "Curates trusted shared memory records",
    "collaboration_creator": "Creates cross-team collaboration programs",
    "agent_run_controller": "Controls same-tenant Agent Runs",
    "execution_controller": "Reads same-tenant execution tasks",
    "platform_administrator": "Tenant-scoped platform administration",
    "agent_worker": "Durable Agent Worker service identity",
    "execution_worker": "Durable execution task worker service identity",
}


class AcceptanceFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def load_keycloak_environment() -> dict[str, str]:
    require(KEYCLOAK_ENV.is_file(), "Keycloak environment file is absent")
    values: dict[str, str] = {}
    for raw_line in KEYCLOAK_ENV.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("KC_BOOTSTRAP_ADMIN_USERNAME", "KC_BOOTSTRAP_ADMIN_PASSWORD"):
        require(bool(values.get(key)), f"{key} is absent")
    return values


def checked_json(response: httpx.Response, expected: int, operation: str) -> Any:
    require(response.status_code == expected, f"{operation} failed with HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise AcceptanceFailure(f"{operation} returned invalid JSON") from exc


def admin_token(client: httpx.Client, environment: dict[str, str]) -> str:
    response = client.post(
        f"{BASE_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": environment["KC_BOOTSTRAP_ADMIN_USERNAME"],
            "password": environment["KC_BOOTSTRAP_ADMIN_PASSWORD"],
        },
    )
    payload = checked_json(response, 200, "bootstrap administrator authentication")
    token = payload.get("access_token")
    require(isinstance(token, str) and bool(token), "administrator token is absent")
    return token


def configure_worker(client: httpx.Client, token: str) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    clients = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients",
            params={"clientId": CLIENT_ID},
            headers=headers,
        ),
        200,
        "worker client lookup",
    )
    require(isinstance(clients, list) and len(clients) == 1, "worker client is not unique")
    internal_client_id = clients[0].get("id")
    require(isinstance(internal_client_id, str), "worker internal client id is absent")

    service_user = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_client_id}/service-account-user",
            headers=headers,
        ),
        200,
        "worker service account lookup",
    )
    service_user_id = service_user.get("id")
    require(isinstance(service_user_id, str), "worker service account id is absent")
    service_user["attributes"] = {
        "tenant_id": ["team-a"],
        "clearance": ["internal"],
        "compartments": ["project-x"],
    }
    updated = client.put(
        f"{BASE_URL}/admin/realms/{REALM}/users/{service_user_id}",
        headers=headers,
        json=service_user,
    )
    require(updated.status_code == 204, f"worker attributes update failed with HTTP {updated.status_code}")

    role = checked_json(
        client.get(f"{BASE_URL}/admin/realms/{REALM}/roles/agent_worker", headers=headers),
        200,
        "agent worker role lookup",
    )
    assigned = client.post(
        f"{BASE_URL}/admin/realms/{REALM}/users/{service_user_id}/role-mappings/realm",
        headers=headers,
        json=[role],
    )
    require(assigned.status_code == 204, f"worker role assignment failed with HTTP {assigned.status_code}")

    secret_payload = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_client_id}/client-secret",
            headers=headers,
        ),
        200,
        "worker client secret lookup",
    )
    secret = secret_payload.get("value")
    require(isinstance(secret, str) and len(secret) >= 16, "worker client secret is invalid")
    return service_user_id, secret


def configure_directory_reader(client: httpx.Client, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    clients = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients",
            params={"clientId": DIRECTORY_CLIENT_ID},
            headers=headers,
        ),
        200,
        "directory client lookup",
    )
    if not clients:
        created = client.post(
            f"{BASE_URL}/admin/realms/{REALM}/clients",
            headers=headers,
            json={
                "clientId": DIRECTORY_CLIENT_ID,
                "name": "COIFESP Worker Identity Directory Reader",
                "description": (
                    "Separately credentialed least-privilege client for resolving "
                    "current worker owner identities"
                ),
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
                "clientAuthenticatorType": "client-secret",
            },
        )
        require(created.status_code == 201, f"directory client creation failed with HTTP {created.status_code}")
        clients = checked_json(
            client.get(
                f"{BASE_URL}/admin/realms/{REALM}/clients",
                params={"clientId": DIRECTORY_CLIENT_ID},
                headers=headers,
            ),
            200,
            "created directory client lookup",
        )
    require(isinstance(clients, list) and len(clients) == 1, "directory client is not unique")
    internal_id = clients[0].get("id")
    require(isinstance(internal_id, str), "directory internal client id is absent")
    service_user = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_id}/service-account-user",
            headers=headers,
        ),
        200,
        "directory service account lookup",
    )
    user_id = service_user.get("id")
    require(isinstance(user_id, str), "directory service account id is absent")

    management_clients = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients",
            params={"clientId": "realm-management"},
            headers=headers,
        ),
        200,
        "realm management client lookup",
    )
    require(
        isinstance(management_clients, list) and len(management_clients) == 1,
        "realm management client is not unique",
    )
    management_id = management_clients[0].get("id")
    require(isinstance(management_id, str), "realm management client id is absent")
    roles = []
    for role_name in ("view-users", "query-users", "query-groups"):
        roles.append(
            checked_json(
                client.get(
                    f"{BASE_URL}/admin/realms/{REALM}/clients/{management_id}/roles/{role_name}",
                    headers=headers,
                ),
                200,
                f"realm management {role_name} role lookup",
            )
        )
    assigned = client.post(
        f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}/role-mappings/clients/{management_id}",
        headers=headers,
        json=roles,
    )
    require(
        assigned.status_code == 204,
        f"directory read role assignment failed with HTTP {assigned.status_code}",
    )


def configure_application_roles(client: httpx.Client, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    for name, description in APPLICATION_ROLES.items():
        response = client.get(
            f"{BASE_URL}/admin/realms/{REALM}/roles/{name}", headers=headers
        )
        if response.status_code == 200:
            continue
        require(response.status_code == 404, f"application role {name} lookup failed with HTTP {response.status_code}")
        created = client.post(
            f"{BASE_URL}/admin/realms/{REALM}/roles",
            headers=headers,
            json={"name": name, "description": description},
        )
        require(created.status_code == 201, f"application role {name} creation failed with HTTP {created.status_code}")


def worker_token(client: httpx.Client, secret: str) -> str:
    payload = checked_json(
        client.post(
            f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": secret,
            },
        ),
        200,
        "worker token grant",
    )
    token = payload.get("access_token")
    require(isinstance(token, str) and bool(token), "worker access token is absent")
    return token


async def verify_harness(token: str) -> None:
    settings = load_environment_settings(PROJECT_ROOT / ".env")
    verifier = OIDCVerifier(settings=settings)
    try:
        identity = await verifier.verify(token)
        require(identity.principal.tenant_id == "team-a", "tenant claim mapping failed")
        require(identity.principal.clearance.name.lower() == "internal", "clearance claim mapping failed")
        require(identity.principal.compartments == frozenset({"project-x"}), "compartment mapping failed")
        require("agent_worker" in identity.principal.roles, "realm role mapping failed")
        require(identity.principal.is_service, "service identity marker mapping failed")

        app = create_app(settings=settings, verifier=verifier)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://harness.test") as client:
            accepted = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
            require(accepted.status_code == 200, f"real token API request failed with HTTP {accepted.status_code}")
            body = accepted.json()
            require(body.get("tenant_id") == "team-a", "API identity tenant projection failed")
            require(body.get("clearance") == "internal", "API identity clearance projection failed")
            require("agent_worker" in body.get("roles", []), "API identity role projection failed")

            missing = await client.get("/v1/auth/me")
            require(missing.status_code == 401, "missing bearer token did not fail closed")
            tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
            rejected = await client.get(
                "/v1/auth/me", headers={"Authorization": f"Bearer {tampered}"}
            )
            require(rejected.status_code == 401, "tampered bearer token did not fail closed")
    finally:
        await verifier.aclose()


def main() -> int:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    environment = load_keycloak_environment()
    with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
        token = admin_token(client, environment)
        configure_application_roles(client, token)
        _, secret = configure_worker(client, token)
        configure_directory_reader(client, token)
        access_token = worker_token(client, secret)
    asyncio.run(verify_harness(access_token))
    print(
        "OIDC_E2E_OK issuer=keycloak audience=coifesp-control-plane "
        "azp=coifesp-agent-worker tenant=team-a role=agent_worker "
        "directory_reader=least_privilege negative_tests=missing,tampered secrets=redacted"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceFailure as exc:
        print(f"OIDC_E2E_FAILED reason={exc}")
        raise SystemExit(1) from None
