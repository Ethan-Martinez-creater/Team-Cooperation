import json

import httpx
import pytest

from coifesp_harness.config import SecretValue
from coifesp_harness.connectors import (
    ConnectorCatalog,
    ConnectorEndpoint,
    ConnectorError,
    ConnectorRequest,
    SecureConnectorClient,
    OfficeMessageTools,
    load_connector_endpoints,
)
from coifesp_harness.errors import PolicyDenied
from coifesp_harness.security import Classification
from coifesp_harness.tool_jobs.worker import ToolExecutionContext, _CONTEXT


def endpoint(**overrides):
    values = {
        "connector_id": "teams-main",
        "tenant_id": "team-a",
        "base_url": "https://api.office.test",
        "token_endpoint": "https://identity.office.test/oauth/token",
        "client_id": "office-client",
        "client_secret": SecretValue("x" * 32),
        "scopes": ("message.send",),
        "allowed_paths": frozenset({"/v1/messages"}),
        "max_classification": Classification.INTERNAL,
        "max_attempts": 2,
        "circuit_failure_threshold": 2,
    }
    values.update(overrides)
    return ConnectorEndpoint(**values)


def request(**overrides):
    values = {
        "connector_id": "teams-main",
        "tenant_id": "team-a",
        "path": "/v1/messages",
        "body": {"channel": "delivery", "text": "status update"},
        "idempotency_key": "run-1:call-1",
        "classification": Classification.INTERNAL,
    }
    values.update(overrides)
    return ConnectorRequest(**values)


class Factory:
    def __init__(self, api_handler):
        self.api_handler = api_handler
        self.clients = []

    def __call__(self, **kwargs):
        is_api = "base_url" in kwargs

        def token_handler(value):
            assert value.url == httpx.URL("https://identity.office.test/oauth/token")
            return httpx.Response(
                200,
                json={"access_token": "t" * 32, "token_type": "Bearer", "expires_in": 300},
            )

        kwargs["transport"] = httpx.MockTransport(self.api_handler if is_api else token_handler)
        client = httpx.AsyncClient(**kwargs)
        self.clients.append(client)
        return client


@pytest.mark.asyncio
async def test_connector_uses_fixed_origin_oauth_scope_and_provider_idempotency() -> None:
    observed = []

    def api_handler(value):
        observed.append(value)
        assert value.url == httpx.URL("https://api.office.test/v1/messages")
        assert value.headers["authorization"] == "Bearer " + "t" * 32
        assert value.headers["idempotency-key"] == "run-1:call-1"
        assert json.loads(value.content) == {"channel": "delivery", "text": "status update"}
        return httpx.Response(
            201, json={"message_id": "provider-1"}, headers={"x-request-id": "req-1"}
        )

    catalog = ConnectorCatalog()
    catalog.register(endpoint())
    factory = Factory(api_handler)
    response = await SecureConnectorClient(catalog=catalog, client_factory=factory).execute(
        request()
    )
    assert response.status_code == 201
    assert response.body == {"message_id": "provider-1"}
    assert response.provider_request_id == "req-1"
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_connector_hides_other_tenants_and_rejects_paths_and_classification() -> None:
    catalog = ConnectorCatalog()
    catalog.register(endpoint())
    client = SecureConnectorClient(catalog=catalog, client_factory=Factory(lambda _: None))
    with pytest.raises(PolicyDenied, match="hidden"):
        await client.execute(request(tenant_id="team-b"))
    with pytest.raises(PolicyDenied, match="path"):
        await client.execute(request(path="/v1/admin"))
    with pytest.raises(PolicyDenied, match="classification"):
        await client.execute(request(classification=Classification.CONFIDENTIAL))


@pytest.mark.asyncio
async def test_connector_rejects_redirect_and_never_follows_attacker_location() -> None:
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "http://127.0.0.1/secrets"})

    catalog = ConnectorCatalog()
    catalog.register(endpoint())
    with pytest.raises(ConnectorError, match="redirect_rejected"):
        await SecureConnectorClient(catalog=catalog, client_factory=Factory(handler)).execute(
            request()
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_connector_retries_transient_status_with_same_idempotency_key_and_opens_circuit() -> (
    None
):
    calls = []

    def handler(value):
        calls.append(value.headers["idempotency-key"])
        return httpx.Response(503, json={"error": "unavailable"})

    class NoDelay(SecureConnectorClient):
        async def _backoff(self, attempt):
            return None

    catalog = ConnectorCatalog()
    catalog.register(endpoint())
    client = NoDelay(catalog=catalog, client_factory=Factory(handler))
    for _ in range(2):
        with pytest.raises(ConnectorError) as captured:
            await client.execute(request())
        assert captured.value.retryable
    with pytest.raises(ConnectorError, match="circuit_open"):
        await client.execute(request())
    assert calls == ["run-1:call-1"] * 4


def test_endpoint_rejects_plaintext_credentials_paths_and_unpinned_origin_shape() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        endpoint(base_url="http://api.office.test")
    with pytest.raises(ValueError, match="credential"):
        endpoint(base_url="https://user:pass@api.office.test")
    with pytest.raises(ValueError, match="path"):
        endpoint(base_url="https://api.office.test/v1")
    with pytest.raises(ValueError, match="path allowlist"):
        endpoint(allowed_paths=frozenset({"https://attacker.test/v1"}))
    with pytest.raises(ValueError, match="local"):
        endpoint(base_url="https://localhost")
    with pytest.raises(ValueError, match="IP literal"):
        endpoint(base_url="https://169.254.169.254")


@pytest.mark.asyncio
async def test_office_tool_uses_worker_tenant_stable_idempotency_and_safe_approval_projection() -> (
    None
):
    observed = None

    class Client:
        async def execute(self, value):
            nonlocal observed
            observed = value
            from coifesp_harness.connectors import ConnectorResponse

            return ConnectorResponse(201, {"message_id": "m-1"}, "req-1")

    tool = OfficeMessageTools(
        client=Client(), tenant_id="team-a", classification=Classification.INTERNAL
    ).definition()
    from coifesp_harness.security import RiskLevel

    assert tool.risk is RiskLevel.HIGH
    assert tool.approval_review is not None
    disclosures = {field.name: field.disclosure.value for field in tool.approval_review.fields}
    assert disclosures == {
        "connector": "value",
        "target": "value",
        "text_digest": "hash",
        "text_chars": "count",
    }
    token = _CONTEXT.set(
        ToolExecutionContext("team-a", "job-1", "run-1", "call-1", "provider-idem")
    )
    try:
        result = await tool.handler(
            {"connector_id": "teams-main", "target": "channel-1", "text": "private update"}
        )
    finally:
        _CONTEXT.reset(token)
    assert result["provider_request_id"] == "req-1"
    assert observed.tenant_id == "team-a"
    assert observed.idempotency_key == "provider-idem"
    assert observed.classification is Classification.INTERNAL
    assert observed.body == {"target": "channel-1", "text": "private update"}


def test_connector_registry_references_secrets_and_is_single_tenant() -> None:
    value = {
        "connector_id": "teams-main",
        "tenant_id": "team-a",
        "base_url": "https://api.office.test",
        "token_endpoint": "https://identity.office.test/oauth/token",
        "client_id": "office-client",
        "client_secret_env": "COIFESP_CONNECTOR_TEAMS_CLIENT_SECRET",
        "scopes": ["message.send"],
        "allowed_paths": ["/v1/messages"],
        "max_classification": "internal",
        "timeout_seconds": 15,
        "max_response_bytes": 1048576,
        "max_attempts": 3,
        "circuit_failure_threshold": 5,
        "circuit_cooldown_seconds": 30,
    }
    loaded = load_connector_endpoints(
        json.dumps([value]),
        tenant_id="team-a",
        environment={"COIFESP_CONNECTOR_TEAMS_CLIENT_SECRET": "s" * 32},
    )
    assert loaded[0].client_secret.reveal() == "s" * 32
    assert "s" * 32 not in json.dumps(value)
    with pytest.raises(ValueError, match="tenant"):
        load_connector_endpoints(
            json.dumps([{**value, "tenant_id": "team-b"}]),
            tenant_id="team-a",
            environment={"COIFESP_CONNECTOR_TEAMS_CLIENT_SECRET": "s" * 32},
        )
    with pytest.raises(ValueError, match="secret"):
        load_connector_endpoints(json.dumps([value]), tenant_id="team-a", environment={})
