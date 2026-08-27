from __future__ import annotations

import json
import logging
import math
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings
from ..security.redaction import SecretRedactor

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class SafeJsonFormatter(logging.Formatter):
    """Bounded JSON logs that omit arbitrary record extras and exception text."""

    def __init__(self, *, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name
        self.redactor = SecretRedactor()

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = "log_message_format_failed"
        redacted = self.redactor.redact(message).text
        span = trace.get_current_span().get_span_context()
        value: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": redacted[:8192],
        }
        request_id = getattr(record, "request_id", None)
        if isinstance(request_id, str) and request_id:
            value["request_id"] = request_id[:128]
        event_name = getattr(record, "event_name", None)
        if isinstance(event_name, str) and event_name:
            value["event_name"] = event_name[:128]
        if span.is_valid:
            value["trace_id"] = format(span.trace_id, "032x")
            value["span_id"] = format(span.span_id, "016x")
        if record.exc_info:
            value["error_type"] = record.exc_info[0].__name__
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def configure_structured_logging(settings: Settings) -> logging.Handler | None:
    if not settings.structured_logging:
        return None
    logger = logging.getLogger("coifesp")
    for handler in logger.handlers:
        if getattr(handler, "_coifesp_structured", False):
            return handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeJsonFormatter(service_name=settings.service_name))
    handler._coifesp_structured = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, settings.log_level))
    logger.propagate = False
    return handler


class ObservabilityRuntime:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "coifesp_http_requests_total",
            "Control-plane HTTP requests.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "coifesp_http_request_duration_seconds",
            "Control-plane HTTP request duration.",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.http_inflight = Gauge(
            "coifesp_http_requests_inflight",
            "In-flight control-plane requests.",
            registry=self.registry,
        )
        self.agent_worker_outcomes = Counter(
            "coifesp_agent_worker_outcomes_total",
            "Durable Agent Worker outcomes.",
            ("outcome", "error_code"),
            registry=self.registry,
        )
        self.model_attempts = Counter(
            "coifesp_model_attempts_total",
            "Model provider attempts after policy routing.",
            ("provider_id", "outcome", "failure_kind"),
            registry=self.registry,
        )
        self.model_duration = Histogram(
            "coifesp_model_attempt_duration_seconds",
            "Model provider attempt duration including concurrency wait.",
            ("provider_id", "outcome"),
            registry=self.registry,
        )
        self.model_input_tokens = Counter(
            "coifesp_model_input_tokens_total",
            "Input tokens reported by successful model provider attempts.",
            ("provider_id",),
            registry=self.registry,
        )
        self.model_output_tokens = Counter(
            "coifesp_model_output_tokens_total",
            "Output tokens reported by successful model provider attempts.",
            ("provider_id",),
            registry=self.registry,
        )
        self.model_cost_microusd = Counter(
            "coifesp_model_cost_microusd_total",
            "Estimated model cost in millionths of a US dollar.",
            ("provider_id",),
            registry=self.registry,
        )
        self._provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.service_name,
                    "deployment.environment.name": settings.environment.value,
                }
            ),
            sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
        )
        if settings.telemetry_enabled:
            assert settings.otlp_traces_endpoint is not None
            exporter = OTLPSpanExporter(endpoint=settings.otlp_traces_endpoint)
            self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self.tracer = self._provider.get_tracer("coifesp_harness.control_plane")
        self.logger = logging.getLogger("coifesp.observability")

    def middleware(self, app: ASGIApp) -> "ObservabilityMiddleware":
        return ObservabilityMiddleware(app, runtime=self)

    def metrics_payload(self, authorization: str | None) -> tuple[int, bytes, list]:
        if not self.settings.metrics_enabled:
            return 404, b"", [(b"content-length", b"0")]
        configured = self.settings.metrics_bearer_token
        if configured is not None:
            expected = f"Bearer {configured.reveal()}"
            if authorization is None or not secrets.compare_digest(authorization, expected):
                return 401, b"", [
                    (b"content-length", b"0"),
                    (b"www-authenticate", b"Bearer"),
                ]
        body = generate_latest(self.registry)
        return 200, body, [
            (b"content-type", _CONTENT_TYPE.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]

    def record_worker_outcome(self, *, outcome: str, error_code: str | None) -> None:
        safe_error = error_code or "none"
        if not _safe_metric_value(outcome) or not _safe_metric_value(safe_error):
            outcome, safe_error = "invalid", "invalid"
        self.agent_worker_outcomes.labels(outcome=outcome, error_code=safe_error).inc()

    def record_model_attempt(
        self,
        *,
        provider_id: str,
        outcome: str,
        failure_kind: str | None,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
    ) -> None:
        safe_failure = failure_kind or "none"
        if (
            not _safe_metric_value(provider_id)
            or outcome not in {"succeeded", "failed"}
            or not _safe_metric_value(safe_failure)
            or not math.isfinite(duration_seconds)
            or min(duration_seconds, input_tokens, output_tokens, cost_microusd) < 0
        ):
            provider_id, outcome, safe_failure = "invalid", "invalid", "invalid"
            duration_seconds = max(0.0, duration_seconds)
            input_tokens = output_tokens = cost_microusd = 0
        self.model_attempts.labels(
            provider_id=provider_id,
            outcome=outcome,
            failure_kind=safe_failure,
        ).inc()
        self.model_duration.labels(
            provider_id=provider_id,
            outcome=outcome,
        ).observe(duration_seconds)
        if outcome == "succeeded":
            self.model_input_tokens.labels(provider_id=provider_id).inc(input_tokens)
            self.model_output_tokens.labels(provider_id=provider_id).inc(output_tokens)
            self.model_cost_microusd.labels(provider_id=provider_id).inc(cost_microusd)

    def shutdown(self) -> None:
        self._provider.force_flush(timeout_millis=5000)
        self._provider.shutdown()


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, *, runtime: ObservabilityRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == self.runtime.settings.metrics_path:
            await self._metrics(scope, send)
            return
        method = str(scope.get("method", "UNKNOWN")).upper()
        headers = _headers(scope)
        parent = propagate.extract(headers)
        started = time.perf_counter()
        status_code = 500
        self.runtime.http_inflight.inc()

        async def observed_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            with self.runtime.tracer.start_as_current_span(
                f"HTTP {method}",
                context=parent,
                kind=SpanKind.SERVER,
                attributes={"http.request.method": method},
            ) as span:
                try:
                    await self.app(scope, receive, observed_send)
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    span.set_attribute("error.type", type(exc).__name__)
                    raise
                finally:
                    route = _route(scope)
                    span.update_name(f"{method} {route}")
                    span.set_attribute("http.route", route)
                    span.set_attribute("http.response.status_code", status_code)
                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))
        finally:
            duration = time.perf_counter() - started
            route = _route(scope)
            self.runtime.http_inflight.dec()
            self.runtime.http_requests.labels(
                method=method,
                route=route,
                status_class=f"{status_code // 100}xx",
            ).inc()
            self.runtime.http_duration.labels(method=method, route=route).observe(duration)
            self.runtime.logger.info(
                "http_request_completed method=%s route=%s status_class=%s duration_ms=%d",
                method,
                route,
                f"{status_code // 100}xx",
                int(duration * 1000),
                extra={
                    "request_id": scope.get("state", {}).get("request_id", ""),
                    "event_name": "http.request.completed",
                },
            )

    async def _metrics(self, scope: Scope, send: Send) -> None:
        authorization_values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"authorization"
        ]
        authorization = authorization_values[0] if len(authorization_values) == 1 else None
        status, body, headers = self.runtime.metrics_payload(authorization)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _headers(scope: Scope) -> dict[str, str]:
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers", [])
    }


def _route(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path.startswith("/") and len(path) <= 256:
        return path
    return "unmatched"


def _safe_metric_value(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(
        character.islower() or character.isdigit() or character == "_"
        for character in value
    )
