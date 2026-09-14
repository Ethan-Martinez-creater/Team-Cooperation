from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from ..config import SecretValue
from ..security import Classification
from .models import ConnectorEndpoint

_SECRET_ENV = re.compile(r"^COIFESP_CONNECTOR_[A-Z0-9_]{1,80}_CLIENT_SECRET$")
_FIELDS = frozenset(
    {
        "connector_id",
        "tenant_id",
        "base_url",
        "token_endpoint",
        "client_id",
        "client_secret_env",
        "scopes",
        "allowed_paths",
        "max_classification",
        "timeout_seconds",
        "max_response_bytes",
        "max_attempts",
        "circuit_failure_threshold",
        "circuit_cooldown_seconds",
    }
)


def configured_connector_paths(raw: str | None) -> frozenset[str]:
    """Read non-secret capability paths for model and worker catalog parity."""
    return frozenset(path for paths in configured_connector_path_sets(raw) for path in paths)


def configured_connector_path_sets(raw: str | None) -> tuple[frozenset[str], ...]:
    """Return each configured connector's paths without resolving any secret."""
    if not raw:
        return ()
    if len(raw.encode("utf-8")) > 262_144:
        raise ValueError("connector registry is too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("connector registry is invalid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("connector registry must contain 1 to 64 endpoints")
    path_sets = []
    for value in values:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError("connector registry fields are invalid")
        configured = value.get("allowed_paths")
        if not isinstance(configured, list) or any(not isinstance(path, str) for path in configured):
            raise ValueError("connector scopes or paths are invalid")
        path_sets.append(frozenset(configured))
    return tuple(path_sets)


def configured_connector_tenants(
    raw: str | None, *, required_paths: frozenset[str]
) -> frozenset[str]:
    """Return tenants whose configured connector contains every required path."""
    if not raw:
        return frozenset()
    if len(raw.encode("utf-8")) > 262_144:
        raise ValueError("connector registry is too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("connector registry is invalid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("connector registry must contain 1 to 64 endpoints")
    tenants: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError("connector registry fields are invalid")
        tenant_id = value.get("tenant_id")
        paths = value.get("allowed_paths")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or not isinstance(paths, list)
            or any(not isinstance(path, str) for path in paths)
        ):
            raise ValueError("connector tenant or paths are invalid")
        if required_paths.issubset(paths):
            tenants.add(tenant_id)
    return frozenset(tenants)


def load_connector_endpoints(
    raw: str, *, tenant_id: str, environment: Mapping[str, str] | None = None
) -> tuple[ConnectorEndpoint, ...]:
    source = environment if environment is not None else os.environ
    if len(raw.encode("utf-8")) > 262_144:
        raise ValueError("connector registry is too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("connector registry is invalid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("connector registry must contain 1 to 64 endpoints")
    endpoints = []
    for value in values:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError("connector registry fields are invalid")
        if value["tenant_id"] != tenant_id:
            raise ValueError("connector tenant differs from the Tool Worker tenant")
        secret_env = value["client_secret_env"]
        if not isinstance(secret_env, str) or not _SECRET_ENV.fullmatch(secret_env):
            raise ValueError("connector secret environment reference is invalid")
        secret = source.get(secret_env, "").strip()
        if not secret:
            raise ValueError("connector client secret is missing")
        scopes = value["scopes"]
        paths = value["allowed_paths"]
        if not isinstance(scopes, list) or not isinstance(paths, list):
            raise TypeError("connector scopes or paths are invalid")
        try:
            endpoint = ConnectorEndpoint(
                connector_id=value["connector_id"],
                tenant_id=value["tenant_id"],
                base_url=value["base_url"],
                token_endpoint=value["token_endpoint"],
                client_id=value["client_id"],
                client_secret=SecretValue(secret),
                scopes=tuple(scopes),
                allowed_paths=frozenset(paths),
                max_classification=Classification[value["max_classification"].upper()],
                timeout_seconds=value["timeout_seconds"],
                max_response_bytes=value["max_response_bytes"],
                max_attempts=value["max_attempts"],
                circuit_failure_threshold=value["circuit_failure_threshold"],
                circuit_cooldown_seconds=value["circuit_cooldown_seconds"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("connector registry value is invalid") from exc
        endpoints.append(endpoint)
    if len({item.connector_id for item in endpoints}) != len(endpoints):
        raise ValueError("connector IDs are duplicated")
    return tuple(endpoints)


def load_connector_endpoints_for_tenants(
    raw: str,
    *,
    tenant_ids: tuple[str, ...],
    environment: Mapping[str, str] | None = None,
) -> tuple[ConnectorEndpoint, ...]:
    """Load one deployment document while rejecting endpoints outside the pool scope."""
    if not tenant_ids or len(set(tenant_ids)) != len(tenant_ids):
        raise ValueError("connector tenant scope is invalid")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("connector registry is invalid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("connector registry must contain 1 to 64 endpoints")
    allowed = set(tenant_ids)
    if any(not isinstance(value, dict) or value.get("tenant_id") not in allowed for value in values):
        raise ValueError("connector tenant is outside the Tool Worker pool")
    endpoints = []
    for tenant_id in tenant_ids:
        selected = [value for value in values if value.get("tenant_id") == tenant_id]
        if selected:
            endpoints.extend(load_connector_endpoints(
                json.dumps(selected), tenant_id=tenant_id, environment=environment,
            ))
    return tuple(endpoints)
