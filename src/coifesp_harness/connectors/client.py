from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..auth import ClientCredentialsConfig, ClientCredentialsTokenProvider
from ..errors import PolicyDenied
from .catalog import ConnectorCatalog
from .models import ConnectorRequest, ConnectorResponse


@dataclass(slots=True)
class _Circuit:
    failures: int = 0
    open_until: float = 0


class ConnectorError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class SecureConnectorClient:
    """Fixed-origin OAuth client with bounded retries and provider idempotency."""

    def __init__(
        self,
        *,
        catalog: ConnectorCatalog,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
        random_source: random.Random | None = None,
    ) -> None:
        self.catalog = catalog
        self.client_factory = client_factory or httpx.AsyncClient
        self.clock = clock
        self.random = random_source or random.SystemRandom()
        self._circuits: dict[tuple[str, str], _Circuit] = {}

    async def execute(self, request: ConnectorRequest) -> ConnectorResponse:
        endpoint = self.catalog.get(tenant_id=request.tenant_id, connector_id=request.connector_id)
        if endpoint is None:
            raise PolicyDenied("connector is absent or hidden")
        if request.path not in endpoint.allowed_paths:
            raise PolicyDenied("connector path is not allowlisted")
        if request.classification > endpoint.max_classification:
            raise PolicyDenied("connector data classification exceeds its policy")
        if not request.idempotency_key or len(request.idempotency_key) > 128:
            raise ConnectorError("invalid_idempotency_key")
        try:
            encoded = json.dumps(
                request.body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ConnectorError("invalid_request_body") from exc
        if len(encoded) > 1_048_576:
            raise ConnectorError("request_too_large")

        key = (request.tenant_id, request.connector_id)
        circuit = self._circuits.setdefault(key, _Circuit())
        if circuit.open_until > self.clock():
            raise ConnectorError("circuit_open", retryable=True)
        auth_client = self.client_factory(
            timeout=httpx.Timeout(endpoint.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        token_provider = ClientCredentialsTokenProvider(
            ClientCredentialsConfig(
                token_endpoint=endpoint.token_endpoint,
                client_id=endpoint.client_id,
                client_secret=endpoint.client_secret,
                scopes=endpoint.scopes,
            ),
            client=auth_client,
        )
        client = self.client_factory(
            base_url=endpoint.base_url,
            timeout=httpx.Timeout(endpoint.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "coifesp-connector/0.1"},
        )
        try:
            token = await token_provider.token()
            for attempt in range(endpoint.max_attempts):
                try:
                    response = await client.post(
                        request.path,
                        content=encoded,
                        headers={
                            "Authorization": f"Bearer {token.reveal()}",
                            "Content-Type": "application/json",
                            "Idempotency-Key": request.idempotency_key,
                        },
                    )
                except httpx.HTTPError as exc:
                    if attempt + 1 == endpoint.max_attempts:
                        self._failure(endpoint, circuit)
                        raise ConnectorError("network_failure", retryable=True) from exc
                    await self._backoff(attempt)
                    continue
                if 300 <= response.status_code < 400:
                    self._failure(endpoint, circuit)
                    raise ConnectorError("redirect_rejected")
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    if attempt + 1 < endpoint.max_attempts:
                        await self._backoff(attempt)
                        continue
                    self._failure(endpoint, circuit)
                    raise ConnectorError("provider_transient_failure", retryable=True)
                if response.status_code < 200 or response.status_code >= 300:
                    self._failure(endpoint, circuit)
                    raise ConnectorError("provider_rejected")
                body = self._bounded_json(response, endpoint.max_response_bytes)
                circuit.failures = 0
                circuit.open_until = 0
                provider_request_id = response.headers.get("x-request-id")
                if provider_request_id is not None and len(provider_request_id) > 256:
                    provider_request_id = None
                return ConnectorResponse(response.status_code, body, provider_request_id)
            raise AssertionError("connector retry loop is unreachable")
        finally:
            await token_provider.aclose()
            await auth_client.aclose()
            await client.aclose()

    def _failure(self, endpoint, circuit: _Circuit) -> None:
        circuit.failures += 1
        if circuit.failures >= endpoint.circuit_failure_threshold:
            circuit.open_until = self.clock() + endpoint.circuit_cooldown_seconds

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(2.0, 0.1 * (2**attempt)) * self.random.uniform(0.8, 1.2))

    @staticmethod
    def _bounded_json(response: httpx.Response, maximum: int) -> dict:
        if len(response.content) > maximum:
            raise ConnectorError("response_too_large")
        try:
            value = response.json()
        except ValueError as exc:
            raise ConnectorError("invalid_provider_response") from exc
        if not isinstance(value, dict):
            raise ConnectorError("invalid_provider_response")
        return value
