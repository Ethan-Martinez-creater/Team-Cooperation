"""Configure and validate the dedicated Tool Worker Keycloak client.

Administrator credentials and the client secret remain in memory and are never
printed or written by this script.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx

KEYCLOAK_ENV = Path(r"E:\keyclock\runtime\keycloak.env")
BASE_URL = "http://127.0.0.1:8080"
REALM = "coifesp"
CLIENT_ID = "coifesp-tool-worker"
REQUIRED_SCOPES = ("roles", "coifesp-identity", "coifesp-service-identity")
FORBIDDEN_ROLES = {
    "agent_worker",
    "execution_worker",
    "platform_administrator",
}


class ConfigurationFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigurationFailure(message)


def checked_json(response: httpx.Response, status: int, operation: str) -> Any:
    require(response.status_code == status, f"{operation} failed with HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ConfigurationFailure(f"{operation} returned invalid JSON") from exc


def load_admin_environment() -> dict[str, str]:
    require(KEYCLOAK_ENV.is_file(), "Keycloak environment file is absent")
    values: dict[str, str] = {}
    for raw in KEYCLOAK_ENV.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("KC_BOOTSTRAP_ADMIN_USERNAME", "KC_BOOTSTRAP_ADMIN_PASSWORD"):
        require(bool(values.get(key)), f"{key} is absent")
    return values


def admin_headers(client: httpx.Client, environment: dict[str, str]) -> dict[str, str]:
    payload = checked_json(
        client.post(
            f"{BASE_URL}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": environment["KC_BOOTSTRAP_ADMIN_USERNAME"],
                "password": environment["KC_BOOTSTRAP_ADMIN_PASSWORD"],
            },
        ),
        200,
        "administrator authentication",
    )
    token = payload.get("access_token")
    require(isinstance(token, str) and bool(token), "administrator token is absent")
    return {"Authorization": f"Bearer {token}"}


def unique_client(client: httpx.Client, headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
    values = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients",
            params={"clientId": CLIENT_ID},
            headers=headers,
        ),
        200,
        "Tool Worker client lookup",
    )
    require(isinstance(values, list) and len(values) == 1, "Tool Worker client is absent or not unique")
    value = values[0]
    internal_id = value.get("id")
    require(isinstance(internal_id, str), "Tool Worker internal client id is absent")
    return internal_id, value


def validate_capabilities(value: dict[str, Any]) -> None:
    require(value.get("publicClient") is False, "client must be confidential")
    require(value.get("serviceAccountsEnabled") is True, "service accounts must be enabled")
    require(value.get("standardFlowEnabled") is False, "standard flow must be disabled")
    require(value.get("directAccessGrantsEnabled") is False, "direct grants must be disabled")
    require(value.get("implicitFlowEnabled") is False, "implicit flow must be disabled")
    require(value.get("authorizationServicesEnabled") is not True, "authorization services must be disabled")


def assign_default_scopes(client: httpx.Client, headers: dict[str, str], internal_id: str) -> None:
    scopes = checked_json(
        client.get(f"{BASE_URL}/admin/realms/{REALM}/client-scopes", headers=headers),
        200,
        "client scope lookup",
    )
    by_name = {
        item.get("name"): item.get("id")
        for item in scopes
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for name in REQUIRED_SCOPES:
        if isinstance(by_name.get(name), str):
            continue
        require(name == "roles", f"required client scope is absent: {name}")
        created = client.post(
            f"{BASE_URL}/admin/realms/{REALM}/client-scopes",
            headers=headers,
            json={
                "name": "roles",
                "description": "Realm roles for COIFESP service identities",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "false",
                    "display.on.consent.screen": "false",
                },
            },
        )
        require(created.status_code == 201, f"roles client scope creation failed with HTTP {created.status_code}")
        refreshed = checked_json(
            client.get(f"{BASE_URL}/admin/realms/{REALM}/client-scopes", headers=headers),
            200,
            "created roles scope lookup",
        )
        roles_scope = next(
            (item for item in refreshed if isinstance(item, dict) and item.get("name") == "roles"),
            None,
        )
        require(isinstance(roles_scope, dict) and isinstance(roles_scope.get("id"), str), "created roles scope is absent")
        roles_scope_id = roles_scope["id"]
        mapper = client.post(
            f"{BASE_URL}/admin/realms/{REALM}/client-scopes/{roles_scope_id}/protocol-mappers/models",
            headers=headers,
            json={
                "name": "realm roles",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-realm-role-mapper",
                "consentRequired": False,
                "config": {
                    "multivalued": "true",
                    "access.token.claim": "true",
                    "id.token.claim": "true",
                    "userinfo.token.claim": "true",
                    "introspection.token.claim": "true",
                    "claim.name": "realm_access.roles",
                    "jsonType.label": "String",
                },
            },
        )
        require(mapper.status_code == 201, f"realm role mapper creation failed with HTTP {mapper.status_code}")
        by_name["roles"] = roles_scope_id
    assigned = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_id}/default-client-scopes",
            headers=headers,
        ),
        200,
        "assigned default scope lookup",
    )
    assigned_names = {
        item.get("name") for item in assigned if isinstance(item, dict)
    }
    for name in REQUIRED_SCOPES:
        if name in assigned_names:
            continue
        response = client.put(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_id}/default-client-scopes/{by_name[name]}",
            headers=headers,
        )
        require(response.status_code == 204, f"assigning default scope {name} failed with HTTP {response.status_code}")


def configure_service_account(client: httpx.Client, headers: dict[str, str], internal_id: str) -> None:
    user = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_id}/service-account-user",
            headers=headers,
        ),
        200,
        "service account lookup",
    )
    user_id = user.get("id")
    require(isinstance(user_id, str), "service account id is absent")
    attributes = user.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    attributes.update(
        {
            "tenant_id": ["team-a"],
            "clearance": ["internal"],
            "compartments": ["project-x"],
        }
    )
    user["attributes"] = attributes
    response = client.put(
        f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}",
        headers=headers,
        json=user,
    )
    require(response.status_code == 204, f"service account attribute update failed with HTTP {response.status_code}")

    tool_role = checked_json(
        client.get(f"{BASE_URL}/admin/realms/{REALM}/roles/tool_worker", headers=headers),
        200,
        "tool_worker role lookup",
    )
    response = client.post(
        f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}/role-mappings/realm",
        headers=headers,
        json=[tool_role],
    )
    require(response.status_code == 204, f"tool_worker role assignment failed with HTTP {response.status_code}")
    effective_roles = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/users/{user_id}/role-mappings/realm/composite",
            headers=headers,
        ),
        200,
        "service account effective role lookup",
    )
    names = {
        item.get("name") for item in effective_roles if isinstance(item, dict)
    }
    require("tool_worker" in names, "tool_worker role is not effective")
    forbidden = sorted(names.intersection(FORBIDDEN_ROLES))
    require(not forbidden, f"service account has forbidden application roles: {forbidden}")


def validate_secret(client: httpx.Client, headers: dict[str, str], internal_id: str) -> None:
    payload = checked_json(
        client.get(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_id}/client-secret",
            headers=headers,
        ),
        200,
        "client secret lookup",
    )
    secret = payload.get("value")
    require(isinstance(secret, str) and len(secret.encode("utf-8")) >= 16, "client secret is invalid")
    token = checked_json(
        client.post(
            f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": secret,
            },
        ),
        200,
        "Tool Worker client credentials grant",
    ).get("access_token")
    require(isinstance(token, str) and bool(token), "Tool Worker token is absent")


def main() -> int:
    try:
        environment = load_admin_environment()
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            headers = admin_headers(client, environment)
            internal_id, representation = unique_client(client, headers)
            validate_capabilities(representation)
            assign_default_scopes(client, headers, internal_id)
            configure_service_account(client, headers, internal_id)
            validate_secret(client, headers, internal_id)
        print(
            "TOOL_WORKER_KEYCLOAK_CONFIG_OK client=coifesp-tool-worker "
            "default_scopes=roles,coifesp-identity,coifesp-service-identity "
            "tenant=team-a role=tool_worker forbidden_roles=absent "
            "client_credentials=yes secrets=redacted"
        )
        return 0
    except Exception as exc:
        print(
            f"TOOL_WORKER_KEYCLOAK_CONFIG_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
