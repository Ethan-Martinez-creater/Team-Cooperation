import runpy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "single-host"


def test_compose_uses_isolated_names_and_loopback_ports() -> None:
    document = yaml.safe_load((BUNDLE / "compose.yaml").read_text(encoding="utf-8"))
    assert set(document["services"]) == {"postgres", "keycloak"}
    assert document["volumes"]["postgres-data"]["name"] == (
        "team-cooperation-prod-postgres"
    )
    ports = [
        value
        for service in document["services"].values()
        for value in service.get("ports", [])
    ]
    assert ports
    assert all(str(value).startswith("127.0.0.1:") for value in ports)
    rendered = (BUNDLE / "compose.yaml").read_text(encoding="utf-8")
    assert "/opt/coifesp" not in rendered
    assert "/var/run/docker.sock" not in rendered
    assert "privileged:" not in rendered


def test_systemd_units_are_dedicated_and_hardened() -> None:
    units = [
        BUNDLE / "systemd" / name
        for name in (
            "team-cooperation-agent-worker.service",
            "team-cooperation-control-plane.service",
            "team-cooperation-github-adapter.service",
            "team-cooperation-tool-worker.service",
        )
    ]
    for unit in units:
        text = unit.read_text(encoding="utf-8")
        assert unit.name.startswith("team-cooperation-")
        assert "User=teamcoop" in text
        assert "NoNewPrivileges=true" in text
        assert "ProtectSystem=strict" in text
        assert "WorkingDirectory=/opt/team-cooperation/current" in text
        assert "/opt/coifesp" not in text
        assert "8000" not in text and "8010" not in text and "5432" not in text

    github = (BUNDLE / "systemd" / "team-cooperation-github-adapter.service").read_text(
        encoding="utf-8"
    )
    assert "-m coifesp_harness.connectors.github_adapter" in github
    assert "--factory" not in github

    tool = (BUNDLE / "systemd" / "team-cooperation-tool-worker.service").read_text(
        encoding="utf-8"
    )
    assert "Delegate=yes" in tool
    assert "run-tool-worker.sh" in tool
    assert "ProtectHome=false" in tool
    wrapper = (BUNDLE / "run-tool-worker.sh").read_text(encoding="utf-8")
    assert 'runtime_dir="/run/user/$(id -u)"' in wrapper
    assert "-S" in wrapper


def test_nginx_keeps_backends_on_loopback_and_disables_sse_buffering() -> None:
    http = (BUNDLE / "nginx-http.conf").read_text(encoding="utf-8")
    text = (BUNDLE / "nginx-https.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://127.0.0.1:8020" in text
    assert "proxy_pass http://127.0.0.1:8180" in text
    assert "proxy_pass http://127.0.0.1:8011" in text
    assert "proxy_buffering off" in text
    assert "server_name __PUBLIC_HOST__" in text
    assert "server_name __GITHUB_HOST__" in text
    assert "listen 127.0.0.1:8444 ssl" in text
    assert "listen 127.0.0.1:8445 ssl" in text
    assert "server_name __IDENTITY_HOST__" in text
    assert "identity-internal.crt" in text
    assert "default_server" not in text
    assert ".well-known/acme-challenge" in http
    assert "listen 443 ssl" not in http


def test_short_lived_ip_certificate_has_automatic_renewal() -> None:
    service = (BUNDLE / "systemd" / "team-cooperation-certbot-renew.service").read_text(
        encoding="utf-8"
    )
    timer = (BUNDLE / "systemd" / "team-cooperation-certbot-renew.timer").read_text(
        encoding="utf-8"
    )
    assert "certbot renew" in service
    assert "systemctl reload nginx" in service
    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer


def test_secret_examples_do_not_ship_values() -> None:
    lines = (BUNDLE / "infrastructure.env.example").read_text(
        encoding="utf-8"
    ).splitlines()
    secret_names = {
        "POSTGRES_SUPERUSER_PASSWORD",
        "TEAMCOOP_DB_PASSWORD",
        "KEYCLOAK_DB_PASSWORD",
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
    }
    values = {
        key: value
        for line in lines
        if line and not line.startswith("#")
        for key, value in [line.split("=", 1)]
    }
    assert all(values[name] == "" for name in secret_names)


def test_generator_wires_explicit_internal_tls_ca_bundle() -> None:
    text = (BUNDLE / "prepare-deployment.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--identity-origin", required=True)' in text
    assert 'app["COIFESP_TLS_CA_BUNDLE"] = args.ssl_cert_file' in text
    assert '"serviceAccountClientId": "coifesp-agent-worker"' in text
    assert '"serviceAccountClientId": "coifesp-tool-worker"' in text
    assert '"tenant_id": ["platform"]' in text


def test_prepared_realm_contains_bounded_service_accounts() -> None:
    prepare_realm = runpy.run_path(str(BUNDLE / "prepare-deployment.py"))[
        "prepare_realm"
    ]
    realm, _ = prepare_realm(
        ROOT / "deploy" / "keycloak" / "coifesp-realm.json",
        public_origin="https://public.example.test",
        client_secrets={
            "coifesp-agent-worker": "a" * 64,
            "coifesp-tool-worker": "b" * 64,
            "coifesp-directory-reader": "c" * 64,
        },
    )
    services = {
        user["serviceAccountClientId"]: user
        for user in realm["users"]
        if "serviceAccountClientId" in user
    }
    assert services["coifesp-agent-worker"]["attributes"]["tenant_id"] == [
        "platform"
    ]
    assert services["coifesp-agent-worker"]["realmRoles"] == ["agent_worker"]
    assert services["coifesp-tool-worker"]["realmRoles"] == ["tool_worker"]
    assert services["coifesp-directory-reader"]["clientRoles"] == {
        "realm-management": ["view-users", "query-users", "query-groups"]
    }
    demo_users = {
        user["username"]: user
        for user in realm["users"]
        if user["username"].endswith("-demo")
    }
    assert set(demo_users) == {"team-a-demo", "team-b-demo", "team-c-demo"}
    assert all(user["firstName"] for user in demo_users.values())
    assert all(user["lastName"] == "Demo" for user in demo_users.values())
    scopes = {scope["name"]: scope for scope in realm["clientScopes"]}
    assert scopes["basic"]["protocol"] == "openid-connect"
    assert any(
        mapper["protocolMapper"] == "oidc-sub-mapper"
        and mapper["config"]["access.token.claim"] == "true"
        for mapper in scopes["basic"]["protocolMappers"]
    )
    ui = next(client for client in realm["clients"] if client["clientId"] == "coifesp-local-ui")
    assert "basic" in ui["defaultClientScopes"]
