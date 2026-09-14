from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from ..config import SecretValue
from ..security import Classification

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,1023}$")


class ConnectorAuth(str, Enum):
    OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"


@dataclass(frozen=True, slots=True)
class ConnectorEndpoint:
    connector_id: str
    tenant_id: str
    base_url: str
    token_endpoint: str
    client_id: str
    client_secret: SecretValue
    scopes: tuple[str, ...]
    allowed_paths: frozenset[str]
    max_classification: Classification = Classification.INTERNAL
    timeout_seconds: float = 15
    max_response_bytes: int = 1_048_576
    max_attempts: int = 3
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 30

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.connector_id) or not self.tenant_id:
            raise ValueError("connector identity is invalid")
        base = urlparse(self.base_url)
        token = urlparse(self.token_endpoint)
        for parsed in (base, token):
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("connector URL must be credential-free HTTPS")
            hostname = parsed.hostname.lower()
            if hostname == "localhost" or hostname.endswith(".localhost"):
                raise ValueError("connector hostname cannot be local")
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                pass
            else:
                raise ValueError("connector hostname cannot be an IP literal")
        if base.path not in {"", "/"}:
            raise ValueError("connector base URL cannot contain a path")
        if not self.client_id or len(self.client_secret.reveal().encode()) < 16:
            raise ValueError("connector OAuth credential is invalid")
        if not 1 <= len(self.scopes) <= 32 or any(
            not value or len(value) > 256 or any(char.isspace() for char in value)
            for value in self.scopes
        ):
            raise ValueError("connector OAuth scopes are invalid")
        if not self.allowed_paths or any(not _PATH.fullmatch(path) for path in self.allowed_paths):
            raise ValueError("connector path allowlist is invalid")
        if not 0.1 <= self.timeout_seconds <= 120:
            raise ValueError("connector timeout is invalid")
        if not 1024 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("connector response limit is invalid")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("connector retry budget is invalid")
        if not 1 <= self.circuit_failure_threshold <= 100:
            raise ValueError("connector circuit threshold is invalid")
        if not 1 <= self.circuit_cooldown_seconds <= 3600:
            raise ValueError("connector circuit cooldown is invalid")


@dataclass(frozen=True, slots=True)
class ConnectorRequest:
    connector_id: str
    tenant_id: str
    path: str
    body: dict
    idempotency_key: str
    classification: Classification


@dataclass(frozen=True, slots=True)
class ConnectorResponse:
    status_code: int
    body: dict
    provider_request_id: str | None = None
