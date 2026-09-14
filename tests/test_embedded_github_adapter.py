import asyncio
import json

import httpx
import pytest

from coifesp_harness.config import ConfigurationError, Environment, SecretValue
from coifesp_harness.connectors import (
    ConnectorCatalog,
    ConnectorEndpoint,
    ConnectorRequest,
)
from coifesp_harness.connectors.runtime_client import (
    EMBEDDED_GITHUB_ORIGIN,
    build_connector_client,
)
from coifesp_harness.security import Classification


def configuration(tmp_path):
    return {
        "COIFESP_GITHUB_ADAPTER_MODE": "embedded",
        "COIFESP_GITHUB_ADAPTER_CLIENT_ID": "test-client",
        "COIFESP_GITHUB_ADAPTER_CLIENT_SECRET": "s" * 32,
        "COIFESP_GITHUB_TOKEN": "test-token",
        "COIFESP_GITHUB_REPOSITORIES": json.dumps(["owner/repo"]),
        "COIFESP_GITHUB_ADAPTER_LEDGER": str(tmp_path / "ledger.sqlite3"),
    }


def test_embedded_auth_and_checks_need_no_network_to_adapter(tmp_path, monkeypatch):
    from coifesp_harness.connectors import github_native
    calls = []

    class Native:
        def __init__(self, **kwargs):
            assert kwargs["token"] == "test-token"

        async def execute(self, path, body):
            calls.append(path)
            return {**body, "complete": True, "checks": []}

    async def network_forbidden(*args, **kwargs):
        raise AssertionError("adapter must not use DNS or network")

    monkeypatch.setattr(github_native, "NativeGitHubClient", Native)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network_forbidden)
    catalog = ConnectorCatalog()
    catalog.register(ConnectorEndpoint("github-main", "team-a", EMBEDDED_GITHUB_ORIGIN,
        EMBEDDED_GITHUB_ORIGIN + "/oauth/token", "test-client", SecretValue("s" * 32),
        ("github.adapter",), frozenset({"/v1/github/commit-checks"})))
    client = build_connector_client(catalog=catalog, runtime_environment=Environment.DEVELOPMENT,
                                    environment=configuration(tmp_path))
    result = asyncio.run(client.execute(ConnectorRequest("github-main", "team-a",
        "/v1/github/commit-checks", {"repository": "owner/repo", "commit_sha": "a" * 40},
        "test-job", Classification.INTERNAL)))
    assert result.body["complete"] is True
    assert calls == ["/v1/github/commit-checks"]


@pytest.mark.parametrize("mode,environment", [
    ("embedded", Environment.PRODUCTION), ("unexpected", Environment.DEVELOPMENT),
])
def test_mode_is_explicit_and_local_only(mode, environment):
    with pytest.raises(ConfigurationError):
        build_connector_client(catalog=ConnectorCatalog(), runtime_environment=environment,
                               environment={"COIFESP_GITHUB_ADAPTER_MODE": mode})


def test_missing_embedded_configuration_does_not_fall_back():
    with pytest.raises(ConfigurationError, match="incomplete"):
        build_connector_client(catalog=ConnectorCatalog(), runtime_environment=Environment.DEVELOPMENT,
                               environment={"COIFESP_GITHUB_ADAPTER_MODE": "embedded"})


def test_embedded_adapter_rejects_multiple_github_tenants(tmp_path):
    with pytest.raises(ConfigurationError, match="exactly one"):
        build_connector_client(
            catalog=ConnectorCatalog(),
            runtime_environment=Environment.DEVELOPMENT,
            environment=configuration(tmp_path),
            github_tenant_ids=("team-a", "team-b"),
        )


def test_external_default_does_not_require_github_token():
    client = build_connector_client(catalog=ConnectorCatalog(), runtime_environment=Environment.PRODUCTION,
                                    environment={})
    assert client.client_factory is httpx.AsyncClient
