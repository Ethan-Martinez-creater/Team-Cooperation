import asyncio
import json
import logging

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from coifesp_harness.config import ConfigurationError, Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.observability import ObservabilityRuntime, SafeJsonFormatter


def settings(**overrides):
    values = {
        "COIFESP_ENV": "test",
        "COIFESP_OIDC_ISSUER": "https://identity.example.test",
        "COIFESP_OIDC_AUDIENCE": "coifesp",
        "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
    }
    values.update(overrides)
    return Settings.from_environment(values)


async def request(app, path, *, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://control.example.test",
    ) as client:
        return await client.get(path, headers=headers)


def test_observability_configuration_fails_closed() -> None:
    missing_endpoint = settings(COIFESP_TELEMETRY_ENABLED="true")
    with pytest.raises(ConfigurationError, match="OTLP_TRACES_ENDPOINT"):
        missing_endpoint.validate()
    insecure_production_metrics = Settings.from_environment(
        {
            "COIFESP_ENV": "production",
            "COIFESP_DATABASE_URL": "postgresql+psycopg://db.invalid/coifesp",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test",
            "COIFESP_OIDC_AUDIENCE": "coifesp",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
            "COIFESP_AUDIT_KEY_ID": "audit-v1",
            "COIFESP_AUDIT_SIGNING_KEY": "a" * 32,
            "COIFESP_ENVELOPE_SIGNING_KEY": "b" * 32,
            "COIFESP_MEMORY_KEY_ID": "memory-v1",
            "COIFESP_MEMORY_MASTER_KEY": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
            "COIFESP_METRICS_ENABLED": "true",
        }
    )
    with pytest.raises(ConfigurationError, match="METRICS_BEARER_TOKEN"):
        insecure_production_metrics.validate()


def test_metrics_endpoint_requires_constant_scope_token_and_has_bounded_labels() -> None:
    value = settings(
        COIFESP_METRICS_ENABLED="true",
        COIFESP_METRICS_BEARER_TOKEN="metrics-token-that-is-not-logged",
    )
    runtime = ObservabilityRuntime(settings=value)
    app = create_app(settings=value, observability=runtime)
    assert asyncio.run(request(app, "/health/live")).status_code == 200
    denied = asyncio.run(request(app, "/internal/metrics"))
    assert denied.status_code == 401
    metrics = asyncio.run(
        request(
            app,
            "/internal/metrics",
            headers={"Authorization": "Bearer metrics-token-that-is-not-logged"},
        )
    )
    assert metrics.status_code == 200
    assert "coifesp_http_requests_total" in metrics.text
    assert 'route="/health/live"' in metrics.text
    assert "metrics-token-that-is-not-logged" not in metrics.text
    runtime.shutdown()


def test_model_gateway_metrics_have_bounded_labels_and_account_usage() -> None:
    runtime = ObservabilityRuntime(
        settings=settings(COIFESP_METRICS_ENABLED="true")
    )
    runtime.record_model_attempt(
        provider_id="deepseek",
        outcome="succeeded",
        failure_kind=None,
        duration_seconds=0.25,
        input_tokens=12,
        output_tokens=4,
        cost_microusd=19,
    )
    runtime.record_model_attempt(
        provider_id="tenant-controlled-value-with-hyphens",
        outcome="failed",
        failure_kind="server",
        duration_seconds=0.1,
        input_tokens=0,
        output_tokens=0,
        cost_microusd=0,
    )
    payload = runtime.metrics_payload(None)[1].decode("utf-8")
    assert 'provider_id="deepseek"' in payload
    assert 'failure_kind="none"' in payload
    assert 'provider_id="invalid"' in payload
    assert "tenant-controlled-value-with-hyphens" not in payload
    assert 'coifesp_model_input_tokens_total{provider_id="deepseek"} 12.0' in payload
    assert 'coifesp_model_cost_microusd_total{provider_id="deepseek"} 19.0' in payload
    runtime.shutdown()


def test_http_trace_uses_route_template_and_omits_raw_query_and_identity() -> None:
    value = settings()
    runtime = ObservabilityRuntime(settings=value)
    exporter = InMemorySpanExporter()
    runtime._provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = create_app(settings=value, observability=runtime)
    response = asyncio.run(request(app, "/health/live?secret=do-not-record"))
    assert response.status_code == 200
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "GET /health/live"
    assert span.attributes["http.route"] == "/health/live"
    serialized = repr(span.attributes)
    assert "secret" not in serialized
    assert "tenant" not in serialized
    runtime.shutdown()


def test_json_formatter_redacts_secrets_and_does_not_emit_exception_text() -> None:
    formatter = SafeJsonFormatter(service_name="coifesp-test")
    try:
        raise RuntimeError("api_key=super-secret-value")
    except RuntimeError:
        record = logging.LogRecord(
            "coifesp.test",
            logging.ERROR,
            __file__,
            1,
            "request failed api_key=super-secret-value",
            (),
            exc_info=__import__("sys").exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert payload["message"].startswith("request failed [REDACTED")
    assert payload["error_type"] == "RuntimeError"
    assert "super-secret-value" not in json.dumps(payload)
