"""Fail-closed validation for COIFESP observability deployment assets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

try:
    import promql_parser
except ImportError as exc:  # pragma: no cover - explicit operator error path
    raise SystemExit(
        "OBSERVABILITY_ASSETS_FAILED reason=promql-parser-is-not-installed"
    ) from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "deploy" / "observability"
RUNTIME_METRICS = {
    "coifesp_http_requests_total",
    "coifesp_http_request_duration_seconds_bucket",
    "coifesp_http_request_duration_seconds_count",
    "coifesp_http_request_duration_seconds_sum",
    "coifesp_http_requests_inflight",
    "coifesp_agent_worker_outcomes_total",
    "coifesp_model_attempts_total",
    "coifesp_model_attempt_duration_seconds_bucket",
    "coifesp_model_attempt_duration_seconds_count",
    "coifesp_model_attempt_duration_seconds_sum",
    "coifesp_model_input_tokens_total",
    "coifesp_model_output_tokens_total",
    "coifesp_model_cost_microusd_total",
}
COLLECTOR_METRICS = {
    "otelcol_exporter_send_failed_spans_total",
    "otelcol_exporter_queue_size",
    "otelcol_exporter_queue_capacity",
}
FORBIDDEN_DIMENSIONS = {
    "tenant",
    "tenant_id",
    "principal",
    "principal_id",
    "run_id",
    "artifact_id",
    "prompt",
    "document",
    "project",
    "user_id",
}
REQUIRED_RUNBOOKS = {
    "http-availability",
    "http-latency",
    "agent-worker",
    "model-provider",
    "model-cost",
    "telemetry-export",
}


class AssetValidationError(RuntimeError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssetValidationError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssetValidationError(message)


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    try:
        values = list(yaml.load_all(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader))
    except (OSError, yaml.YAMLError) as exc:
        raise AssetValidationError(f"invalid YAML {path.name}: {type(exc).__name__}") from exc
    require(values and all(isinstance(value, dict) for value in values), f"{path.name} is empty")
    return values


def parse_promql(expression: str, *, source: str) -> set[str]:
    require(isinstance(expression, str) and bool(expression.strip()), f"empty PromQL in {source}")
    try:
        parsed = promql_parser.parse(expression)
    except Exception as exc:
        raise AssetValidationError(f"invalid PromQL in {source}: {type(exc).__name__}") from exc
    metrics: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, promql_parser.VectorSelector) and node.name is not None:
            metrics.add(node.name)

    promql_parser.walk(parsed, pre_visit=visit)
    return metrics


def validate_slo() -> dict[str, Any]:
    slo = load_yaml_documents(ASSET_ROOT / "slo.yaml")[0]
    require(slo.get("schema") == "coifesp.slo.v1", "unsupported SLO schema")
    require(slo.get("service") == "coifesp-control-plane", "SLO service is invalid")
    require(slo.get("window") == "30d", "SLO window must be 30d")
    objectives = slo.get("objectives")
    require(isinstance(objectives, list) and len(objectives) == 3, "three SLO objectives are required")
    expected = {
        "http-availability": 0.999,
        "http-latency": 0.99,
        "durable-worker-success": 0.995,
    }
    names: set[str] = set()
    for objective in objectives:
        require(isinstance(objective, dict), "SLO objective must be an object")
        name = objective.get("name")
        require(name in expected and name not in names, "SLO objective is unknown or duplicated")
        names.add(name)
        require(objective.get("target") == expected[name], f"SLO target mismatch for {name}")
        indicator = objective.get("indicator")
        require(isinstance(indicator, dict) and indicator.get("kind") == "ratio", f"invalid indicator for {name}")
        for field in ("goodEvents", "totalEvents"):
            metrics = parse_promql(indicator.get(field), source=f"slo:{name}:{field}")
            require(metrics <= RUNTIME_METRICS, f"SLO {name} uses an unknown runtime metric")
    availability = next(item for item in objectives if item["name"] == "http-availability")
    alerts = availability.get("burnRateAlerts")
    require(
        alerts == [
            {"severity": "page", "shortWindow": "5m", "longWindow": "1h", "multiplier": 14.4},
            {"severity": "ticket", "shortWindow": "30m", "longWindow": "6h", "multiplier": 6},
        ],
        "availability multi-window burn-rate policy is invalid",
    )
    return slo


def validate_rules() -> tuple[dict[str, Any], set[str]]:
    document = load_yaml_documents(ASSET_ROOT / "prometheus-rules.yaml")[0]
    require(document.get("apiVersion") == "monitoring.coreos.com/v1", "PrometheusRule apiVersion is invalid")
    require(document.get("kind") == "PrometheusRule", "rules are not a PrometheusRule")
    groups = document.get("spec", {}).get("groups")
    require(isinstance(groups, list) and len(groups) >= 2, "recording and alert groups are required")
    recordings: set[str] = set()
    alerts: set[str] = set()
    expressions: list[tuple[str, str]] = []
    runbooks: set[str] = set()
    for group in groups:
        require(isinstance(group, dict) and isinstance(group.get("rules"), list), "invalid rule group")
        for rule in group["rules"]:
            require(isinstance(rule, dict), "rule must be an object")
            name = rule.get("record") or rule.get("alert")
            require(isinstance(name, str) and bool(name), "rule name is absent")
            require(("record" in rule) != ("alert" in rule), f"rule {name} has an invalid type")
            if "record" in rule:
                require(name not in recordings, f"duplicate recording rule {name}")
                recordings.add(name)
            else:
                require(name not in alerts, f"duplicate alert {name}")
                alerts.add(name)
                labels = rule.get("labels")
                annotations = rule.get("annotations")
                require(isinstance(labels, dict) and labels.get("severity") in {"page", "ticket"}, f"alert {name} has no severity")
                require(isinstance(annotations, dict) and isinstance(annotations.get("summary"), str), f"alert {name} has no summary")
                runbook = annotations.get("runbook")
                require(runbook in REQUIRED_RUNBOOKS, f"alert {name} has an unknown runbook")
                runbooks.add(runbook)
            require(isinstance(rule.get("expr"), str), f"rule {name} has no expression")
            expressions.append((name, rule["expr"]))
    require({"COIFESPHttpAvailabilityFastBurn", "COIFESPHttpAvailabilitySlowBurn"} <= alerts, "multi-window SLO alerts are absent")
    allowed = RUNTIME_METRICS | COLLECTOR_METRICS | recordings
    for name, expression in expressions:
        metrics = parse_promql(expression, source=f"rule:{name}")
        require(metrics <= allowed, f"rule {name} uses unknown metrics: {sorted(metrics - allowed)}")
    require(runbooks == REQUIRED_RUNBOOKS, "not every runbook is referenced by an alert")
    return document, recordings


def validate_collector_config(config: dict[str, Any], *, source: str) -> None:
    receiver = config.get("receivers", {}).get("otlp", {}).get("protocols", {})
    for protocol in ("grpc", "http"):
        tls = receiver.get(protocol, {}).get("tls")
        require(isinstance(tls, dict), f"{source} {protocol} receiver has no TLS")
        require(bool(tls.get("cert_file")) and bool(tls.get("key_file")), f"{source} {protocol} TLS files are absent")
    limiter = config.get("processors", {}).get("memory_limiter")
    require(isinstance(limiter, dict) and limiter.get("limit_mib") == 384, f"{source} memory limiter is invalid")
    storage = config.get("extensions", {}).get("file_storage")
    require(isinstance(storage, dict) and str(storage.get("directory", "")).startswith("/var/lib/otelcol/"), f"{source} persistent storage is absent")
    exporter = config.get("exporters", {}).get("otlp/traces")
    require(isinstance(exporter, dict), f"{source} OTLP exporter is absent")
    require(exporter.get("tls", {}).get("insecure") is False, f"{source} upstream TLS must be verified")
    require(bool(exporter.get("tls", {}).get("ca_file")), f"{source} upstream CA is absent")
    queue = exporter.get("sending_queue")
    require(isinstance(queue, dict) and queue.get("storage") == "file_storage", f"{source} durable queue is absent")
    require(exporter.get("retry_on_failure", {}).get("max_elapsed_time") == "300s", f"{source} retry budget is unbounded")
    service = config.get("service", {})
    require(set(service.get("extensions", [])) == {"health_check", "file_storage"}, f"{source} extensions are not activated")
    traces = service.get("pipelines", {}).get("traces", {})
    require(traces.get("exporters") == ["otlp/traces"], f"{source} trace pipeline is invalid")
    readers = service.get("telemetry", {}).get("metrics", {}).get("readers")
    require(isinstance(readers, list) and readers, f"{source} self-metrics are absent")


def validate_kubernetes() -> None:
    documents = load_yaml_documents(ASSET_ROOT / "otel-collector-kubernetes.yaml")
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        by_kind.setdefault(str(document.get("kind")), []).append(document)
    for kind in ("ServiceAccount", "ConfigMap", "StatefulSet", "Service", "ServiceMonitor", "PodDisruptionBudget", "NetworkPolicy"):
        require(kind in by_kind, f"Kubernetes {kind} is absent")
    require("Secret" not in by_kind, "deployment asset must not contain Secret values")
    account = by_kind["ServiceAccount"][0]
    require(account.get("automountServiceAccountToken") is False, "collector service account token must not be mounted")
    config_text = by_kind["ConfigMap"][0].get("data", {}).get("config.yaml")
    require(isinstance(config_text, str), "collector ConfigMap is invalid")
    config = yaml.load(config_text, Loader=UniqueKeyLoader)
    require(isinstance(config, dict), "embedded collector config is invalid")
    validate_collector_config(config, source="Kubernetes collector")

    stateful = by_kind["StatefulSet"][0]
    spec = stateful.get("spec", {})
    require(spec.get("replicas", 0) >= 2, "collector must have at least two replicas")
    require(bool(spec.get("volumeClaimTemplates")), "collector durable queue has no PVC")
    pod = spec.get("template", {}).get("spec", {})
    require(pod.get("automountServiceAccountToken") is False, "collector pod token must not be mounted")
    security = pod.get("securityContext", {})
    require(security.get("runAsNonRoot") is True and security.get("seccompProfile", {}).get("type") == "RuntimeDefault", "collector pod security context is invalid")
    require(len(pod.get("topologySpreadConstraints", [])) >= 2, "zone and node topology spread are required")
    containers = pod.get("containers")
    require(isinstance(containers, list) and len(containers) == 1, "collector must have one bounded container")
    container = containers[0]
    image = container.get("image", "")
    require(
        re.fullmatch(r"ghcr\.io/open-telemetry/.+:0\.153\.0@sha256:[0-9a-f]{64}", image) is not None,
        "collector image must be versioned and digest pinned",
    )
    context = container.get("securityContext", {})
    require(context.get("allowPrivilegeEscalation") is False, "privilege escalation must be disabled")
    require(context.get("readOnlyRootFilesystem") is True, "root filesystem must be read-only")
    require(context.get("capabilities", {}).get("drop") == ["ALL"], "all capabilities must be dropped")
    require(bool(container.get("resources", {}).get("requests")) and bool(container.get("resources", {}).get("limits")), "collector resources are unbounded")
    require(bool(container.get("readinessProbe")) and bool(container.get("livenessProbe")), "collector probes are absent")
    env = container.get("env", [])
    secret_env = {item.get("name") for item in env if "secretKeyRef" in item.get("valueFrom", {})}
    require(secret_env == {"COIFESP_OTEL_UPSTREAM_ENDPOINT", "COIFESP_OTEL_UPSTREAM_AUTHORIZATION"}, "upstream settings must come only from Secret refs")

    policy = by_kind["NetworkPolicy"][0].get("spec", {})
    require(set(policy.get("policyTypes", [])) == {"Ingress", "Egress"}, "collector network policy is incomplete")
    serialized = json.dumps(policy, separators=(",", ":"))
    require('"namespaceSelector":{}' not in serialized, "empty namespace selector is forbidden")
    require("0.0.0.0/0" not in serialized and "::/0" not in serialized, "unrestricted egress is forbidden")
    require("coifesp.dev/telemetry-upstream" in serialized, "upstream egress namespace boundary is absent")


def _dashboard_expressions(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        if isinstance(value.get("expr"), str):
            yield str(value.get("refId", "unknown")), value["expr"]
        for nested in value.values():
            yield from _dashboard_expressions(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _dashboard_expressions(nested)


def validate_dashboard(recordings: set[str]) -> None:
    try:
        dashboard = json.loads((ASSET_ROOT / "grafana-dashboard.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetValidationError(f"invalid Grafana dashboard: {type(exc).__name__}") from exc
    require(dashboard.get("uid") == "coifesp-production-slo", "dashboard UID is invalid")
    require(dashboard.get("editable") is False, "production dashboard must not be editable")
    require(dashboard.get("schemaVersion", 0) >= 40, "dashboard schema is obsolete")
    panels = dashboard.get("panels")
    require(isinstance(panels, list) and len(panels) >= 8, "dashboard coverage is incomplete")
    panel_ids = [panel.get("id") for panel in panels]
    require(len(panel_ids) == len(set(panel_ids)), "dashboard panel IDs are duplicated")
    allowed = RUNTIME_METRICS | COLLECTOR_METRICS | recordings
    count = 0
    for ref, expression in _dashboard_expressions(dashboard):
        count += 1
        metrics = parse_promql(expression, source=f"dashboard:{ref}")
        require(metrics <= allowed, f"dashboard query uses unknown metrics: {sorted(metrics - allowed)}")
    require(count >= 9, "dashboard has insufficient PromQL coverage")
    raw = json.dumps(dashboard).lower()
    for dimension in FORBIDDEN_DIMENSIONS:
        require(re.search(rf"\b{re.escape(dimension)}\b", raw) is None, f"dashboard exposes forbidden dimension {dimension}")


def validate_runbooks() -> None:
    text = (PROJECT_ROOT / "docs" / "observability-runbooks.md").read_text(encoding="utf-8")
    for runbook in REQUIRED_RUNBOOKS:
        require(f'<a id="{runbook}"></a>' in text, f"runbook {runbook} is absent")
    require("Authorization" not in text and "api_key=" not in text, "runbook contains credential-like data")


def validate_all() -> None:
    validate_slo()
    _, recordings = validate_rules()
    standalone = load_yaml_documents(ASSET_ROOT / "otel-collector-config.yaml")[0]
    validate_collector_config(standalone, source="standalone collector")
    validate_kubernetes()
    validate_dashboard(recordings)
    validate_runbooks()


def main() -> int:
    validate_all()
    print(
        "OBSERVABILITY_ASSETS_OK slo=3 promql=parsed burn_rate=multi_window "
        "collector=tls_durable_queue image=digest_pinned network=default_deny "
        "dashboard=validated secrets=references_only"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssetValidationError as exc:
        print(f"OBSERVABILITY_ASSETS_FAILED reason={exc}")
        raise SystemExit(1) from None
