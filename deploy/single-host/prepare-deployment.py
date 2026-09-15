"""Prepare root-only single-host configuration without printing secrets."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def encoded_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def write_env(path: Path, values: dict[str, str]) -> None:
    content = "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def random_hex() -> str:
    return secrets.token_hex(32)


def compliant_password() -> str:
    """Generate a password satisfying the imported production realm policy."""
    return f"Aa1!{secrets.token_hex(20)}"


def prepare_realm(
    template: Path,
    *,
    public_origin: str,
    client_secrets: dict[str, str],
) -> tuple[dict[str, object], dict[str, str]]:
    realm = json.loads(template.read_text(encoding="utf-8"))
    for client in realm["clients"]:
        client_id = client["clientId"]
        if client_id == "coifesp-local-ui":
            client.update({
                "rootUrl": public_origin,
                "baseUrl": f"{public_origin}/",
                "redirectUris": [f"{public_origin}/*"],
                "webOrigins": [public_origin],
            })
            client.setdefault("attributes", {})["post.logout.redirect.uris"] = (
                f"{public_origin}/*"
            )
        if client_id in client_secrets:
            client["secret"] = client_secrets[client_id]

    if not any(group["name"] == "team-c" for group in realm["groups"]):
        realm["groups"].append({
            "name": "team-c",
            "attributes": {
                "tenant_id": ["team-c"],
                "clearance": ["internal"],
                "compartments": ["project-x"],
            },
        })

    demo_passwords = {
        team: compliant_password() for team in ("team-a", "team-b", "team-c")
    }
    roles = {"team-a": ["lead", "tool_approver"], "team-b": ["contributor"], "team-c": ["reviewer"]}
    demo_users = [
        {
            "username": f"{team}-demo",
            "enabled": True,
            "firstName": team.upper(),
            "lastName": "Demo",
            "emailVerified": True,
            "email": f"{team}-demo@example.invalid",
            "attributes": {
                "tenant_id": [team],
                "clearance": ["internal"],
                "compartments": ["project-x"],
            },
            "groups": [f"/{team}"],
            "realmRoles": team_roles,
            "credentials": [{"type": "password", "value": demo_passwords[team], "temporary": False}],
        }
        for team, team_roles in roles.items()
    ]
    service_attributes = {
        "tenant_id": ["platform"],
        "clearance": ["internal"],
        "compartments": ["platform"],
    }
    service_users = [
        {
            "username": "service-account-coifesp-agent-worker",
            "enabled": True,
            "serviceAccountClientId": "coifesp-agent-worker",
            "attributes": service_attributes,
            "realmRoles": ["agent_worker"],
        },
        {
            "username": "service-account-coifesp-tool-worker",
            "enabled": True,
            "serviceAccountClientId": "coifesp-tool-worker",
            "attributes": service_attributes,
            "realmRoles": ["tool_worker"],
        },
        {
            "username": "service-account-coifesp-directory-reader",
            "enabled": True,
            "serviceAccountClientId": "coifesp-directory-reader",
            "clientRoles": {
                "realm-management": ["view-users", "query-users", "query-groups"]
            },
        },
    ]
    realm["users"] = demo_users + service_users
    return realm, demo_passwords


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--github-source-env", type=Path, required=True)
    parser.add_argument("--realm-template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--github-origin", required=True)
    parser.add_argument("--identity-origin", required=True)
    parser.add_argument("--ssl-cert-file", default="")
    args = parser.parse_args()

    if not all(
        value.startswith("https://")
        for value in (args.public_origin, args.github_origin, args.identity_origin)
    ):
        raise SystemExit("public origins must use https")
    outputs = {
        name: args.output_dir / name
        for name in (
            "infrastructure.env",
            "app.env",
            "github-adapter.env",
            "realm-import.json",
            "demo-users.env",
        )
    }
    if any(path.exists() for path in outputs.values()):
        raise SystemExit("refusing to overwrite existing deployment secrets")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.chmod(0o700)

    source = read_env(args.source_env)
    github_source = read_env(args.github_source_env)
    registry_raw = source.get("COIFESP_LLM_PROVIDERS_JSON", "")
    registry = json.loads(registry_raw)
    if not registry or any(item.get("max_data_classification") != "public" for item in registry):
        raise SystemExit("production seed providers must be restricted to PUBLIC data")
    provider_keys = {item["api_key_env"] for item in registry}
    if any(not source.get(key) for key in provider_keys):
        raise SystemExit("an LLM provider API key is missing")
    sandbox_profiles = source.get("COIFESP_SANDBOX_PROFILES_JSON", "")
    if not sandbox_profiles:
        raise SystemExit("sandbox profiles are missing")
    github_token = github_source.get("COIFESP_GITHUB_TOKEN", "")
    if not github_token:
        raise SystemExit("GitHub test token is missing")

    infrastructure = {
        "COMPOSE_PROJECT_NAME": "team-cooperation-prod",
        "POSTGRES_HOST_PORT": "5433",
        "KEYCLOAK_HOST_PORT": "8180",
        "PUBLIC_ORIGIN": args.public_origin,
        "POSTGRES_SUPERUSER_PASSWORD": random_hex(),
        "TEAMCOOP_DB_PASSWORD": random_hex(),
        "KEYCLOAK_DB_PASSWORD": random_hex(),
        "KEYCLOAK_ADMIN_USERNAME": "teamcoop-bootstrap-admin",
        "KEYCLOAK_ADMIN_PASSWORD": random_hex(),
    }
    clients = {
        "coifesp-agent-worker": random_hex(),
        "coifesp-tool-worker": random_hex(),
        "coifesp-directory-reader": random_hex(),
    }
    connector_secret = random_hex()
    oidc_origin = f"{args.public_origin}/auth/realms/coifesp"
    identity_oidc_origin = f"{args.identity_origin}/auth/realms/coifesp"
    app = {
        "COIFESP_ENV": "production",
        "COIFESP_AUTH_MODE": "oidc",
        "COIFESP_DATABASE_URL": (
            "postgresql+psycopg://team_cooperation_app:"
            f"{quote(infrastructure['TEAMCOOP_DB_PASSWORD'], safe='')}"
            "@127.0.0.1:5433/team_cooperation"
        ),
        "COIFESP_OIDC_ISSUER": oidc_origin,
        "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
        "COIFESP_OIDC_AUTHORIZED_PARTIES": (
            "coifesp-local-ui,coifesp-agent-worker,coifesp-tool-worker"
        ),
        "COIFESP_UI_OIDC_CLIENT_ID": "coifesp-local-ui",
        "COIFESP_OIDC_JWKS_URL": (
            f"{identity_oidc_origin}/protocol/openid-connect/certs"
        ),
        "COIFESP_OIDC_ALGORITHMS": "RS256",
        "COIFESP_WORKER_TOKEN_ENDPOINT": (
            f"{identity_oidc_origin}/protocol/openid-connect/token"
        ),
        "COIFESP_WORKER_CLIENT_ID": "coifesp-agent-worker",
        "COIFESP_WORKER_CLIENT_SECRET": clients["coifesp-agent-worker"],
        "COIFESP_WORKER_TENANTS": "team-a,team-b,team-c",
        "COIFESP_DIRECTORY_API_BASE_URL": f"{args.identity_origin}/auth/admin",
        "COIFESP_DIRECTORY_REALM": "coifesp",
        "COIFESP_DIRECTORY_TOKEN_ENDPOINT": (
            f"{identity_oidc_origin}/protocol/openid-connect/token"
        ),
        "COIFESP_DIRECTORY_CLIENT_ID": "coifesp-directory-reader",
        "COIFESP_DIRECTORY_CLIENT_SECRET": clients["coifesp-directory-reader"],
        "COIFESP_TOOL_WORKER_TOKEN_ENDPOINT": (
            f"{identity_oidc_origin}/protocol/openid-connect/token"
        ),
        "COIFESP_TOOL_WORKER_CLIENT_ID": "coifesp-tool-worker",
        "COIFESP_TOOL_WORKER_CLIENT_SECRET": clients["coifesp-tool-worker"],
        "COIFESP_TOOL_WORKER_TENANTS": "team-a,team-b,team-c",
        "COIFESP_SANDBOX_RUNTIME": "podman",
        "COIFESP_SANDBOX_WORKSPACE_ROOT": "/opt/team-cooperation/runtime-data/sandbox",
        "COIFESP_SANDBOX_PROFILES_JSON": sandbox_profiles,
        "COIFESP_CONNECTORS_JSON": json.dumps([{
            "connector_id": "github-main",
            "tenant_id": "team-a",
            "base_url": args.github_origin,
            "token_endpoint": f"{args.github_origin}/oauth/token",
            "client_id": "team-a-github",
            "client_secret_env": "COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET",
            "scopes": ["github.adapter"],
            "allowed_paths": [
                "/v1/github/issues",
                "/v1/github/workflow-dispatches",
                "/v1/github/commit-checks",
            ],
            "max_classification": "internal",
            "timeout_seconds": 15,
            "max_response_bytes": 1048576,
            "max_attempts": 3,
            "circuit_failure_threshold": 5,
            "circuit_cooldown_seconds": 30,
        }], separators=(",", ":")),
        "COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET": connector_secret,
        "COIFESP_OFFICE_DATA_CLASSIFICATION": "internal",
        "COIFESP_ARTIFACT_STORE_ROOT": "/opt/team-cooperation/runtime-data/artifacts",
        "COIFESP_ARTIFACT_MAX_UPLOAD_BYTES": "104857600",
        "COIFESP_AUDIT_KEY_ID": "production-v1",
        "COIFESP_AUDIT_SIGNING_KEY": random_hex(),
        "COIFESP_ENVELOPE_KEY_ID": "production-v1",
        "COIFESP_ENVELOPE_SIGNING_KEY": random_hex(),
        "COIFESP_MEMORY_KEY_ID": "production-v1",
        "COIFESP_MEMORY_MASTER_KEY": encoded_key(),
        "COIFESP_LLM_PROVIDERS_JSON": registry_raw,
        "COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS": "",
        "COIFESP_SERVICE_NAME": "team-cooperation",
        "COIFESP_TELEMETRY_ENABLED": "false",
        "COIFESP_METRICS_ENABLED": "false",
        "COIFESP_STRUCTURED_LOGGING": "true",
        "COIFESP_LOG_LEVEL": "INFO",
    }
    app.update({key: source[key] for key in provider_keys})
    if args.ssl_cert_file:
        app["SSL_CERT_FILE"] = args.ssl_cert_file
        app["COIFESP_TLS_CA_BUNDLE"] = args.ssl_cert_file
    github = {
        "COIFESP_GITHUB_ADAPTER_CLIENT_ID": "team-a-github",
        "COIFESP_GITHUB_ADAPTER_CLIENT_SECRET": connector_secret,
        "COIFESP_GITHUB_TOKEN": github_token,
        "COIFESP_GITHUB_REPOSITORIES": json.dumps([
            "Ethan-Martinez-creater/Team-Cooperation-Test"
        ]),
        "COIFESP_GITHUB_ADAPTER_LEDGER": (
            "/opt/team-cooperation/runtime-data/github/adapter.sqlite3"
        ),
    }
    realm, demo_passwords = prepare_realm(
        args.realm_template,
        public_origin=args.public_origin,
        client_secrets=clients,
    )

    write_env(outputs["infrastructure.env"], infrastructure)
    write_env(outputs["app.env"], app)
    write_env(outputs["github-adapter.env"], github)
    outputs["realm-import.json"].write_text(json.dumps(realm, indent=2) + "\n", encoding="utf-8")
    outputs["realm-import.json"].chmod(0o600)
    write_env(
        outputs["demo-users.env"],
        {f"{team.upper().replace('-', '_')}_USERNAME": f"{team}-demo" for team in demo_passwords}
        | {f"{team.upper().replace('-', '_')}_PASSWORD": password for team, password in demo_passwords.items()},
    )
    print(json.dumps({"status": "prepared", "provider_count": len(registry), "tenant_count": 3}))


if __name__ == "__main__":
    main()
