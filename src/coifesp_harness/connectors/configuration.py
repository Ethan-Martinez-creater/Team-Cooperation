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
            raise ValueError("connector scopes or paths are invalid")
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
