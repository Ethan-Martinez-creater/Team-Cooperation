from __future__ import annotations

import copy
import json

import pytest

from scripts.check_observability_assets import (
    ASSET_ROOT,
    AssetValidationError,
    load_yaml_documents,
    parse_promql,
    validate_all,
    validate_collector_config,
)


def test_observability_assets_pass_fail_closed_validator() -> None:
    validate_all()


def test_promql_parser_rejects_invalid_expression() -> None:
    with pytest.raises(AssetValidationError, match="invalid PromQL"):
        parse_promql("sum(rate(metric[5m])", source="test")


def test_collector_validator_rejects_plaintext_receiver() -> None:
    config = load_yaml_documents(ASSET_ROOT / "otel-collector-config.yaml")[0]
    insecure = copy.deepcopy(config)
    del insecure["receivers"]["otlp"]["protocols"]["grpc"]["tls"]
    with pytest.raises(AssetValidationError, match="has no TLS"):
        validate_collector_config(insecure, source="test")


def test_collector_validator_rejects_non_durable_queue() -> None:
    config = load_yaml_documents(ASSET_ROOT / "otel-collector-config.yaml")[0]
    volatile = copy.deepcopy(config)
    volatile["exporters"]["otlp/traces"]["sending_queue"].pop("storage")
    with pytest.raises(AssetValidationError, match="durable queue"):
        validate_collector_config(volatile, source="test")


def test_dashboard_contains_no_high_cardinality_security_dimensions() -> None:
    dashboard = json.loads((ASSET_ROOT / "grafana-dashboard.json").read_text(encoding="utf-8"))
    raw = json.dumps(dashboard).lower()
    for forbidden in ("tenant_id", "principal_id", "run_id", "artifact_id", "prompt"):
        assert forbidden not in raw
