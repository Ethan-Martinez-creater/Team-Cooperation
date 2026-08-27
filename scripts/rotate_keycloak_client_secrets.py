from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ENV = PROJECT_ROOT / ".env"
KEYCLOAK_ENV = Path(r"E:\keyclock\runtime\keycloak.env")
BASE_URL = "http://127.0.0.1:8080"
REALM = "coifesp"
TOKEN_URL = f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token"
_ENV_LINE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)


class RotationFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClientTarget:
    client_id: str
    env_key: str
    expected_role: str | None


TARGETS = (
    ClientTarget("coifesp-agent-worker", "COIFESP_WORKER_CLIENT_SECRET", "agent_worker"),
    ClientTarget("coifesp-directory-reader", "COIFESP_DIRECTORY_CLIENT_SECRET", None),
    ClientTarget("coifesp-tool-worker", "COIFESP_TOOL_WORKER_CLIENT_SECRET", "tool_worker"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RotationFailure(message)


def checked_json(response: httpx.Response, expected: int, operation: str):
    require(
        response.status_code == expected, f"{operation} failed with HTTP {response.status_code}"
    )
    try:
        return response.json()
    except ValueError as exc:
        raise RotationFailure(f"{operation} returned invalid JSON") from exc


def admin_token(client: httpx.Client, environment: dict[str, str]) -> str:
    username = environment.get("KC_BOOTSTRAP_ADMIN_USERNAME", "")
    password = environment.get("KC_BOOTSTRAP_ADMIN_PASSWORD", "")
    require(bool(username and password), "Keycloak bootstrap administrator credentials are absent")
    payload = checked_json(
        client.post(
            f"{BASE_URL}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            },
        ),
        200,
        "administrator authentication",
    )
    token = payload.get("access_token")
    require(isinstance(token, str) and bool(token), "administrator token is absent")
    return token


def lookup_clients(client: httpx.Client, token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    result: dict[str, str] = {}
    for target in TARGETS:
        values = checked_json(
            client.get(
                f"{BASE_URL}/admin/realms/{REALM}/clients",
                params={"clientId": target.client_id},
                headers=headers,
            ),
            200,
            f"{target.client_id} lookup",
        )
        require(isinstance(values, list) and len(values) == 1, f"{target.client_id} is not unique")
        internal_id = values[0].get("id")
        require(
            isinstance(internal_id, str) and bool(internal_id),
            f"{target.client_id} has no internal ID",
        )
        result[target.client_id] = internal_id
    return result


def rotate_secret(client: httpx.Client, token: str, internal_id: str, client_id: str) -> str:
    payload = checked_json(
        client.post(
            f"{BASE_URL}/admin/realms/{REALM}/clients/{internal_id}/client-secret",
            headers={"Authorization": f"Bearer {token}"},
        ),
        200,
        f"{client_id} secret rotation",
    )
    secret = payload.get("value")
    require(
        isinstance(secret, str) and len(secret) >= 16, f"{client_id} returned an invalid secret"
    )
    return secret


def client_token(client: httpx.Client, client_id: str, secret: str) -> tuple[int, str | None]:
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
        },
    )
    if response.status_code != 200:
        return response.status_code, None
    payload = checked_json(response, 200, f"{client_id} token grant")
    token = payload.get("access_token")
    require(isinstance(token, str) and bool(token), f"{client_id} access token is absent")
    return response.status_code, token


def token_claims(token: str) -> dict:
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise RotationFailure("Keycloak returned a malformed access token") from exc
    require(isinstance(value, dict), "Keycloak access token claims are invalid")
    return value


def verify_identity_claims(target: ClientTarget, token: str) -> None:
    claims = token_claims(token)
    require(claims.get("azp") == target.client_id, f"{target.client_id} azp is invalid")
    require(
        claims.get("client_id", target.client_id) == target.client_id,
        f"{target.client_id} client identity is invalid",
    )
    if target.expected_role:
        realm_access = claims.get("realm_access", {})
        roles = set(realm_access.get("roles", [])) if isinstance(realm_access, dict) else set()
        mapped_roles = claims.get("roles", [])
        if isinstance(mapped_roles, list):
            roles.update(value for value in mapped_roles if isinstance(value, str))
        require(target.expected_role in roles, f"{target.client_id} required role is absent")
        require(claims.get("tenant_id") == "team-a", f"{target.client_id} tenant claim is invalid")


def verify_directory_permissions(client: httpx.Client, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    readable = client.get(
        f"{BASE_URL}/admin/realms/{REALM}/users",
        params={"max": 1},
        headers=headers,
    )
    require(readable.status_code == 200, "directory reader cannot query users")
    forbidden = client.post(
        f"{BASE_URL}/admin/realms/{REALM}/users",
        headers=headers,
        json={"username": "coifesp-rotation-must-not-create", "enabled": False},
    )
    require(forbidden.status_code == 403, "directory reader unexpectedly has user write access")


def replace_env_values(path: Path, replacements: dict[str, str]) -> None:
    original = path.read_text(encoding="utf-8")
    found: set[str] = set()
    output: list[str] = []
    for line in original.splitlines(keepends=True):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(ending)] if ending else line
        match = _ENV_LINE.match(body)
        if match and match.group("key") in replacements:
            key = match.group("key")
            require(key not in found, f"{key} appears more than once in .env")
            output.append(f"{match.group('prefix')}{key}={replacements[key]}{ending}")
            found.add(key)
        else:
            output.append(line)
    missing = set(replacements) - found
    require(not missing, "required client secret entries are absent from .env")

    temporary = path.with_name(f".{path.name}.rotate-{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(output))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    require(PROJECT_ENV.is_file(), "project .env is absent")
    require(KEYCLOAK_ENV.is_file(), "Keycloak environment file is absent")
    keycloak_environment = {
        key: value for key, value in dotenv_values(KEYCLOAK_ENV).items() if isinstance(value, str)
    }
    project_environment = {
        key: value for key, value in dotenv_values(PROJECT_ENV).items() if isinstance(value, str)
    }
    old_secrets: dict[str, str] = {}
    for target in TARGETS:
        value = project_environment.get(target.env_key, "")
        require(len(value) >= 16, f"{target.env_key} is absent or invalid")
        old_secrets[target.client_id] = value

    with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
        token = admin_token(client, keycloak_environment)
        internal_ids = lookup_clients(client, token)
        new_secrets: dict[str, str] = {}
        for target in TARGETS:
            new_secret = rotate_secret(
                client, token, internal_ids[target.client_id], target.client_id
            )
            # Persist immediately after Keycloak commits the rotation. If a later
            # client fails, already-rotated clients never lose their recoverable value.
            replace_env_values(PROJECT_ENV, {target.env_key: new_secret})
            new_secrets[target.client_id] = new_secret
            status, access_token = client_token(client, target.client_id, new_secret)
            require(
                status == 200 and access_token is not None,
                f"{target.client_id} new secret token grant failed",
            )
            verify_identity_claims(target, access_token)
            if target.expected_role is None:
                verify_directory_permissions(client, access_token)
            old_status, _ = client_token(client, target.client_id, old_secrets[target.client_id])
            require(old_status in {400, 401}, f"{target.client_id} old secret is still accepted")
    for target in TARGETS:
        print(
            f"KEYCLOAK_CLIENT_SECRET_ROTATED client={target.client_id} "
            f"new_token=yes old_secret_rejected=yes fingerprint={fingerprint(new_secrets[target.client_id])}"
        )
    print("KEYCLOAK_CLIENT_SECRET_ROTATION_OK clients=3 env_updated=yes secrets=redacted")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RotationFailure, httpx.HTTPError, OSError) as exc:
        print(f"KEYCLOAK_CLIENT_SECRET_ROTATION_FAILED reason={exc}")
        raise SystemExit(1) from None
