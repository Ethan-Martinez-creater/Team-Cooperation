from __future__ import annotations

import os
import base64
import binascii
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping
from urllib.parse import urlparse

from .errors import HarnessError


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ConfigurationError(HarnessError):
    """Startup configuration is missing or unsafe for the selected environment."""


@dataclass(frozen=True, slots=True)
class SecretValue:
    """A small secret wrapper that cannot leak through repr or str."""

    value: str

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    def __str__(self) -> str:
        return "[REDACTED]"

    def reveal(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    provider_id: str
    kind: str
    model: str
    base_url: str | None
    api_key_env: str
    api_key: SecretValue
    capabilities: frozenset[str]
    max_data_classification: str
    region: str
    external: bool
    context_window_tokens: int
    max_output_tokens: int
    input_microusd_per_million_tokens: int
    output_microusd_per_million_tokens: int
    priority: int
    max_concurrency: int
    timeout_seconds: float
    max_retries: int
    tokenizer_encoding: str | None


@dataclass(frozen=True, slots=True)
class SecretKeyConfig:
    key_id: str
    material: SecretValue


@dataclass(frozen=True, slots=True)
class Settings:
    environment: Environment
    auth_mode: str
    database_url: str | None
    oidc_issuer: str | None
    oidc_audience: str | None
    oidc_authorized_parties: frozenset[str]
    oidc_jwks_url: str | None
    oidc_algorithms: tuple[str, ...]
    oidc_tenant_claim: str
    oidc_roles_claim: str
    oidc_compartments_claim: str
    oidc_clearance_claim: str
    oidc_clock_skew_seconds: int
    oidc_max_token_age_seconds: int
    ui_oidc_client_id: str | None
    worker_token_endpoint: str | None
    worker_client_id: str | None
    worker_client_secret: SecretValue | None
    worker_tenant_id: str | None
    directory_api_base_url: str | None
    directory_realm: str | None
    directory_token_endpoint: str | None
    directory_client_id: str | None
    directory_client_secret: SecretValue | None
    worker_idle_poll_seconds: float
    worker_lease_seconds: int
    worker_heartbeat_seconds: float
    tool_worker_token_endpoint: str | None
    tool_worker_client_id: str | None
    tool_worker_client_secret: SecretValue | None
    tool_worker_tenant_id: str | None
    tool_worker_idle_poll_seconds: float
    tool_worker_lease_seconds: int
    tool_worker_heartbeat_seconds: float
    sandbox_runtime: str | None
    sandbox_workspace_root: str | None
    sandbox_profiles_json: str | None
    connectors_json: str | None
    skills_root: str | None
    skills_trusted_keys_json: str | None
    office_data_classification: str
    artifact_store_root: str | None
    artifact_max_upload_bytes: int
    audit_key_id: str | None
    audit_signing_key: SecretValue | None
    audit_keys: tuple[SecretKeyConfig, ...]
    envelope_key_id: str | None
    envelope_legacy_v1_key_id: str | None
    envelope_signing_key: SecretValue | None
    envelope_keys: tuple[SecretKeyConfig, ...]
    memory_key_id: str | None
    memory_master_key: SecretValue | None
    memory_keys: tuple[SecretKeyConfig, ...]
    llm_provider: str | None
    llm_model: str | None
    llm_base_url: str | None
    llm_api_key: SecretValue | None
    llm_providers: tuple[ModelProviderConfig, ...]
    llm_max_failover_attempts: int
    llm_concurrency_wait_seconds: float
    llm_circuit_failure_threshold: int
    llm_circuit_cooldown_seconds: float
    service_name: str
    telemetry_enabled: bool
    otlp_traces_endpoint: str | None
    trace_sample_ratio: float
    metrics_enabled: bool
    metrics_path: str
    metrics_bearer_token: SecretValue | None
    structured_logging: bool
    log_level: str

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> "Settings":
        source = values if values is not None else os.environ

        def optional(name: str) -> str | None:
            value = source.get(name, "").strip()
            return value or None

        def secret(name: str) -> SecretValue | None:
            value = optional(name)
            return SecretValue(value) if value is not None else None

        def positive_integer(name: str, default: int) -> int:
            value = optional(name)
            if value is None:
                return default
            try:
                parsed = int(value)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be an integer") from exc
            return parsed

        def boolean(name: str, default: bool) -> bool:
            value = optional(name)
            if value is None:
                return default
            normalized = value.lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ConfigurationError(f"{name} must be a boolean")

        def ratio(name: str, default: float) -> float:
            value = optional(name)
            if value is None:
                return default
            try:
                parsed = float(value)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be a number") from exc
            if not 0 <= parsed <= 1:
                raise ConfigurationError(f"{name} must be between 0 and 1")
            return parsed

        def positive_number(name: str, default: float) -> float:
            value = optional(name)
            if value is None:
                return default
            try:
                parsed = float(value)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be a number") from exc
            if parsed <= 0:
                raise ConfigurationError(f"{name} must be positive")
            return parsed

        algorithms = tuple(
            value.strip()
            for value in (optional("COIFESP_OIDC_ALGORITHMS") or "RS256").split(",")
            if value.strip()
        )
        raw_environment = optional("COIFESP_ENV") or Environment.DEVELOPMENT.value
        try:
            environment = Environment(raw_environment.lower())
        except ValueError as exc:
            raise ConfigurationError(
                "COIFESP_ENV must be development, test, or production"
            ) from exc

        return cls(
            environment=environment,
            auth_mode=(optional("COIFESP_AUTH_MODE") or (
                "oidc" if environment is Environment.PRODUCTION or optional("COIFESP_OIDC_ISSUER")
                else "builtin"
            )).lower(),
            database_url=optional("COIFESP_DATABASE_URL"),
            oidc_issuer=optional("COIFESP_OIDC_ISSUER"),
            oidc_audience=optional("COIFESP_OIDC_AUDIENCE"),
            oidc_authorized_parties=frozenset(
                item.strip()
                for item in (optional("COIFESP_OIDC_AUTHORIZED_PARTIES") or "").split(",")
                if item.strip()
            ),
            oidc_jwks_url=optional("COIFESP_OIDC_JWKS_URL"),
            oidc_algorithms=algorithms,
            oidc_tenant_claim=(optional("COIFESP_OIDC_TENANT_CLAIM") or "tenant_id"),
            oidc_roles_claim=optional("COIFESP_OIDC_ROLES_CLAIM") or "roles",
            oidc_compartments_claim=(optional("COIFESP_OIDC_COMPARTMENTS_CLAIM") or "compartments"),
            oidc_clearance_claim=(optional("COIFESP_OIDC_CLEARANCE_CLAIM") or "clearance"),
            oidc_clock_skew_seconds=positive_integer(
                "COIFESP_OIDC_CLOCK_SKEW_SECONDS",
                30,
            ),
            oidc_max_token_age_seconds=positive_integer(
                "COIFESP_OIDC_MAX_TOKEN_AGE_SECONDS",
                3600,
            ),
            ui_oidc_client_id=optional("COIFESP_UI_OIDC_CLIENT_ID"),
            worker_token_endpoint=optional("COIFESP_WORKER_TOKEN_ENDPOINT"),
            worker_client_id=optional("COIFESP_WORKER_CLIENT_ID"),
            worker_client_secret=secret("COIFESP_WORKER_CLIENT_SECRET"),
            worker_tenant_id=optional("COIFESP_WORKER_TENANT_ID"),
            directory_api_base_url=optional("COIFESP_DIRECTORY_API_BASE_URL"),
            directory_realm=optional("COIFESP_DIRECTORY_REALM"),
            directory_token_endpoint=optional("COIFESP_DIRECTORY_TOKEN_ENDPOINT"),
            directory_client_id=optional("COIFESP_DIRECTORY_CLIENT_ID"),
            directory_client_secret=secret("COIFESP_DIRECTORY_CLIENT_SECRET"),
            worker_idle_poll_seconds=positive_number("COIFESP_WORKER_IDLE_POLL_SECONDS", 2.0),
            worker_lease_seconds=positive_integer("COIFESP_WORKER_LEASE_SECONDS", 60),
            worker_heartbeat_seconds=positive_number("COIFESP_WORKER_HEARTBEAT_SECONDS", 20.0),
            tool_worker_token_endpoint=optional("COIFESP_TOOL_WORKER_TOKEN_ENDPOINT"),
            tool_worker_client_id=optional("COIFESP_TOOL_WORKER_CLIENT_ID"),
            tool_worker_client_secret=secret("COIFESP_TOOL_WORKER_CLIENT_SECRET"),
            tool_worker_tenant_id=optional("COIFESP_TOOL_WORKER_TENANT_ID"),
            tool_worker_idle_poll_seconds=positive_number(
                "COIFESP_TOOL_WORKER_IDLE_POLL_SECONDS", 2.0
            ),
            tool_worker_lease_seconds=positive_integer("COIFESP_TOOL_WORKER_LEASE_SECONDS", 60),
            tool_worker_heartbeat_seconds=positive_number(
                "COIFESP_TOOL_WORKER_HEARTBEAT_SECONDS", 20.0
            ),
            sandbox_runtime=optional("COIFESP_SANDBOX_RUNTIME"),
            sandbox_workspace_root=optional("COIFESP_SANDBOX_WORKSPACE_ROOT"),
            sandbox_profiles_json=optional("COIFESP_SANDBOX_PROFILES_JSON"),
            connectors_json=optional("COIFESP_CONNECTORS_JSON"),
            skills_root=optional("COIFESP_SKILLS_ROOT"),
            skills_trusted_keys_json=optional("COIFESP_SKILLS_TRUSTED_KEYS_JSON"),
            office_data_classification=(
                optional("COIFESP_OFFICE_DATA_CLASSIFICATION") or "internal"
            ).lower(),
            artifact_store_root=optional("COIFESP_ARTIFACT_STORE_ROOT"),
            artifact_max_upload_bytes=positive_integer(
                "COIFESP_ARTIFACT_MAX_UPLOAD_BYTES", 104_857_600
            ),
            audit_key_id=optional("COIFESP_AUDIT_KEY_ID"),
            audit_signing_key=secret("COIFESP_AUDIT_SIGNING_KEY"),
            audit_keys=_parse_secret_keyring(optional("COIFESP_AUDIT_KEYS_JSON"), source=source),
            envelope_key_id=optional("COIFESP_ENVELOPE_KEY_ID"),
            envelope_legacy_v1_key_id=optional("COIFESP_ENVELOPE_LEGACY_V1_KEY_ID"),
            envelope_signing_key=secret("COIFESP_ENVELOPE_SIGNING_KEY"),
            envelope_keys=_parse_secret_keyring(
                optional("COIFESP_ENVELOPE_KEYS_JSON"), source=source
            ),
            memory_key_id=optional("COIFESP_MEMORY_KEY_ID"),
            memory_master_key=secret("COIFESP_MEMORY_MASTER_KEY"),
            memory_keys=_parse_secret_keyring(optional("COIFESP_MEMORY_KEYS_JSON"), source=source),
            llm_provider=optional("COIFESP_LLM_PROVIDER"),
            llm_model=optional("COIFESP_LLM_MODEL"),
            llm_base_url=optional("COIFESP_LLM_BASE_URL"),
            llm_api_key=secret("COIFESP_LLM_API_KEY"),
            llm_providers=_parse_model_providers(
                optional("COIFESP_LLM_PROVIDERS_JSON"),
                source=source,
                environment=environment,
            ),
            llm_max_failover_attempts=positive_integer(
                "COIFESP_LLM_MAX_FAILOVER_ATTEMPTS",
                2,
            ),
            llm_concurrency_wait_seconds=positive_number(
                "COIFESP_LLM_CONCURRENCY_WAIT_SECONDS",
                2.0,
            ),
            llm_circuit_failure_threshold=positive_integer(
                "COIFESP_LLM_CIRCUIT_FAILURE_THRESHOLD",
                3,
            ),
            llm_circuit_cooldown_seconds=positive_number(
                "COIFESP_LLM_CIRCUIT_COOLDOWN_SECONDS",
                30.0,
            ),
            service_name=optional("COIFESP_SERVICE_NAME") or "coifesp-harness",
            telemetry_enabled=boolean("COIFESP_TELEMETRY_ENABLED", False),
            otlp_traces_endpoint=optional("COIFESP_OTLP_TRACES_ENDPOINT"),
            trace_sample_ratio=ratio(
                "COIFESP_TRACE_SAMPLE_RATIO",
                0.1 if environment is Environment.PRODUCTION else 1.0,
            ),
            metrics_enabled=boolean("COIFESP_METRICS_ENABLED", False),
            metrics_path=optional("COIFESP_METRICS_PATH") or "/internal/metrics",
            metrics_bearer_token=secret("COIFESP_METRICS_BEARER_TOKEN"),
            structured_logging=boolean(
                "COIFESP_STRUCTURED_LOGGING",
                environment is Environment.PRODUCTION,
            ),
            log_level=(optional("COIFESP_LOG_LEVEL") or "INFO").upper(),
        )

    def validate(
        self,
        *,
        require_llm: bool = False,
        require_memory: bool = False,
        require_auth: bool = False,
        require_worker: bool = False,
        require_tool_worker: bool = False,
        require_sandbox: bool = False,
    ) -> None:
        problems: list[str] = []
        if self.auth_mode not in {"builtin", "oidc", "local"}:
            problems.append("COIFESP_AUTH_MODE must be builtin, oidc, or local")
        if self.environment is Environment.PRODUCTION and self.auth_mode != "oidc":
            problems.append("production requires COIFESP_AUTH_MODE=oidc")
        if self.artifact_max_upload_bytes > 2_147_483_648:
            problems.append("COIFESP_ARTIFACT_MAX_UPLOAD_BYTES must not exceed 2147483648")
        if self.environment is Environment.PRODUCTION:
            required = {
                "COIFESP_DATABASE_URL": self.database_url,
                "COIFESP_OIDC_ISSUER": self.oidc_issuer,
                "COIFESP_OIDC_AUDIENCE": self.oidc_audience,
                "COIFESP_AUDIT_KEY_ID": self.audit_key_id,
                "COIFESP_MEMORY_KEY_ID": self.memory_key_id,
            }
            problems.extend(
                f"{name} is required in production"
                for name, value in required.items()
                if value is None
            )
            for name, legacy, keys in (
                (
                    "COIFESP_AUDIT_SIGNING_KEY or COIFESP_AUDIT_KEYS_JSON",
                    self.audit_signing_key,
                    self.audit_keys,
                ),
                (
                    "COIFESP_ENVELOPE_SIGNING_KEY or COIFESP_ENVELOPE_KEYS_JSON",
                    self.envelope_signing_key,
                    self.envelope_keys,
                ),
                (
                    "COIFESP_MEMORY_MASTER_KEY or COIFESP_MEMORY_KEYS_JSON",
                    self.memory_master_key,
                    self.memory_keys,
                ),
            ):
                if legacy is None and not keys:
                    problems.append(f"{name} is required in production")
            if self.database_url and self.database_url.startswith("sqlite"):
                problems.append("production control plane requires a non-SQLite database")
            if self.oidc_issuer and not _is_secure_http_url(self.oidc_issuer):
                problems.append("COIFESP_OIDC_ISSUER must be an https URL")
            for name, value in (
                ("COIFESP_AUDIT_SIGNING_KEY", self.audit_signing_key),
                ("COIFESP_ENVELOPE_SIGNING_KEY", self.envelope_signing_key),
            ):
                if value is not None and len(value.reveal().encode("utf-8")) < 32:
                    problems.append(f"{name} must contain at least 32 bytes")
        for name, key_id in (
            ("COIFESP_AUDIT_KEY_ID", self.audit_key_id),
            ("COIFESP_ENVELOPE_KEY_ID", self.envelope_key_id),
            ("COIFESP_ENVELOPE_LEGACY_V1_KEY_ID", self.envelope_legacy_v1_key_id),
            ("COIFESP_MEMORY_KEY_ID", self.memory_key_id),
        ):
            if key_id is not None and not _KEY_ID.fullmatch(key_id):
                problems.append(f"{name} is invalid")
        for prefix, active, legacy, keys in (
            ("AUDIT", self.audit_key_id, self.audit_signing_key, self.audit_keys),
            (
                "ENVELOPE",
                self.envelope_key_id,
                self.envelope_signing_key,
                self.envelope_keys,
            ),
            ("MEMORY", self.memory_key_id, self.memory_master_key, self.memory_keys),
        ):
            if keys and active not in {item.key_id for item in keys}:
                problems.append(f"COIFESP_{prefix}_KEY_ID must identify an available key")
            if keys and legacy is not None:
                matching = next((item for item in keys if item.key_id == active), None)
                if matching is not None and matching.material.reveal() != legacy.reveal():
                    problems.append(
                        f"legacy COIFESP_{prefix} key conflicts with its active keyring entry"
                    )
        if self.envelope_legacy_v1_key_id and self.envelope_legacy_v1_key_id not in {
            item.key_id for item in self.envelope_keys
        }:
            problems.append(
                "COIFESP_ENVELOPE_LEGACY_V1_KEY_ID must identify an available envelope key"
            )

        if (require_auth or self.environment is Environment.PRODUCTION) and self.auth_mode == "oidc":
            if not self.oidc_issuer:
                problems.append("COIFESP_OIDC_ISSUER is required for control-plane authentication")
            elif not _is_http_url(self.oidc_issuer):
                problems.append("COIFESP_OIDC_ISSUER must be an HTTP(S) URL")
            if not self.oidc_audience:
                problems.append(
                    "COIFESP_OIDC_AUDIENCE is required for control-plane authentication"
                )
            if not self.oidc_authorized_parties:
                problems.append(
                    "COIFESP_OIDC_AUTHORIZED_PARTIES is required for control-plane authentication"
                )
            if self.ui_oidc_client_id is not None and self.ui_oidc_client_id not in self.oidc_authorized_parties:
                problems.append("COIFESP_UI_OIDC_CLIENT_ID must be an authorized OIDC party")
            if self.oidc_jwks_url and not _is_http_url(self.oidc_jwks_url):
                problems.append("COIFESP_OIDC_JWKS_URL must be an HTTP(S) URL")
            if (
                self.environment is Environment.PRODUCTION
                and self.oidc_jwks_url
                and not _is_secure_http_url(self.oidc_jwks_url)
            ):
                problems.append("COIFESP_OIDC_JWKS_URL must be an https URL in production")
            asymmetric_algorithms = {
                "RS256",
                "RS384",
                "RS512",
                "PS256",
                "PS384",
                "PS512",
                "ES256",
                "ES384",
                "ES512",
                "EdDSA",
            }
            if not self.oidc_algorithms:
                problems.append("COIFESP_OIDC_ALGORITHMS cannot be empty")
            elif not set(self.oidc_algorithms).issubset(asymmetric_algorithms):
                problems.append(
                    "COIFESP_OIDC_ALGORITHMS must contain only approved " "asymmetric algorithms"
                )
            for name, value in (
                ("COIFESP_OIDC_TENANT_CLAIM", self.oidc_tenant_claim),
                ("COIFESP_OIDC_ROLES_CLAIM", self.oidc_roles_claim),
                (
                    "COIFESP_OIDC_COMPARTMENTS_CLAIM",
                    self.oidc_compartments_claim,
                ),
                ("COIFESP_OIDC_CLEARANCE_CLAIM", self.oidc_clearance_claim),
            ):
                if not value or len(value) > 256 or any(character.isspace() for character in value):
                    problems.append(f"{name} is invalid")
            if len(self.oidc_authorized_parties) > 64 or any(
                not value or len(value) > 128 or any(character.isspace() for character in value)
                for value in self.oidc_authorized_parties
            ):
                problems.append("COIFESP_OIDC_AUTHORIZED_PARTIES is invalid")
            if not 0 <= self.oidc_clock_skew_seconds <= 300:
                problems.append("COIFESP_OIDC_CLOCK_SKEW_SECONDS must be between 0 and 300")
            if not 60 <= self.oidc_max_token_age_seconds <= 86400:
                problems.append("COIFESP_OIDC_MAX_TOKEN_AGE_SECONDS must be between 60 and 86400")

        if require_worker:
            worker_required = {
                "COIFESP_WORKER_TOKEN_ENDPOINT": self.worker_token_endpoint,
                "COIFESP_WORKER_CLIENT_ID": self.worker_client_id,
                "COIFESP_WORKER_CLIENT_SECRET": self.worker_client_secret,
                "COIFESP_WORKER_TENANT_ID": self.worker_tenant_id,
                "COIFESP_DIRECTORY_API_BASE_URL": self.directory_api_base_url,
                "COIFESP_DIRECTORY_REALM": self.directory_realm,
                "COIFESP_DIRECTORY_TOKEN_ENDPOINT": self.directory_token_endpoint,
                "COIFESP_DIRECTORY_CLIENT_ID": self.directory_client_id,
                "COIFESP_DIRECTORY_CLIENT_SECRET": self.directory_client_secret,
            }
            problems.extend(
                f"{name} is required by the durable worker"
                for name, value in worker_required.items()
                if value is None
            )
            for name, value in (
                ("COIFESP_WORKER_TOKEN_ENDPOINT", self.worker_token_endpoint),
                ("COIFESP_DIRECTORY_API_BASE_URL", self.directory_api_base_url),
                ("COIFESP_DIRECTORY_TOKEN_ENDPOINT", self.directory_token_endpoint),
            ):
                if value and not _is_http_url(value):
                    problems.append(f"{name} must be an HTTP(S) URL")
                elif (
                    value
                    and self.environment is Environment.PRODUCTION
                    and not _is_secure_http_url(value)
                ):
                    problems.append(f"{name} must use https in production")
            for name, value in (
                ("COIFESP_WORKER_CLIENT_ID", self.worker_client_id),
                ("COIFESP_WORKER_TENANT_ID", self.worker_tenant_id),
                ("COIFESP_DIRECTORY_REALM", self.directory_realm),
                ("COIFESP_DIRECTORY_CLIENT_ID", self.directory_client_id),
            ):
                if value and (len(value) > 128 or not _REGION_ID.fullmatch(value)):
                    problems.append(f"{name} is invalid")
            for name, value in (
                ("COIFESP_WORKER_CLIENT_SECRET", self.worker_client_secret),
                ("COIFESP_DIRECTORY_CLIENT_SECRET", self.directory_client_secret),
            ):
                if value is not None and len(value.reveal().encode("utf-8")) < 16:
                    problems.append(f"{name} must contain at least 16 bytes")
            if (
                self.worker_client_id
                and self.directory_client_id
                and self.worker_client_id == self.directory_client_id
            ):
                problems.append("worker and directory OAuth clients must be different")
            if not 5 <= self.worker_lease_seconds <= 3600:
                problems.append("COIFESP_WORKER_LEASE_SECONDS must be between 5 and 3600")
            if not 0 < self.worker_heartbeat_seconds < self.worker_lease_seconds:
                problems.append("COIFESP_WORKER_HEARTBEAT_SECONDS must be shorter than the lease")
            if not 0.05 <= self.worker_idle_poll_seconds <= 60:
                problems.append("COIFESP_WORKER_IDLE_POLL_SECONDS must be between 0.05 and 60")

        if require_tool_worker:
            required = {
                "COIFESP_TOOL_WORKER_TOKEN_ENDPOINT": self.tool_worker_token_endpoint,
                "COIFESP_TOOL_WORKER_CLIENT_ID": self.tool_worker_client_id,
                "COIFESP_TOOL_WORKER_CLIENT_SECRET": self.tool_worker_client_secret,
                "COIFESP_TOOL_WORKER_TENANT_ID": self.tool_worker_tenant_id,
            }
            problems.extend(
                f"{name} is required by the durable Tool Worker"
                for name, value in required.items()
                if value is None
            )
            if self.tool_worker_token_endpoint and not _is_http_url(
                self.tool_worker_token_endpoint
            ):
                problems.append("COIFESP_TOOL_WORKER_TOKEN_ENDPOINT must be an HTTP(S) URL")
            elif (
                self.tool_worker_token_endpoint
                and self.environment is Environment.PRODUCTION
                and not _is_secure_http_url(self.tool_worker_token_endpoint)
            ):
                problems.append("COIFESP_TOOL_WORKER_TOKEN_ENDPOINT must use https in production")
            for name, value in (
                ("COIFESP_TOOL_WORKER_CLIENT_ID", self.tool_worker_client_id),
                ("COIFESP_TOOL_WORKER_TENANT_ID", self.tool_worker_tenant_id),
            ):
                if value and (len(value) > 128 or not _REGION_ID.fullmatch(value)):
                    problems.append(f"{name} is invalid")
            if (
                self.tool_worker_client_secret is not None
                and len(self.tool_worker_client_secret.reveal().encode("utf-8")) < 16
            ):
                problems.append("COIFESP_TOOL_WORKER_CLIENT_SECRET must contain at least 16 bytes")
            client_ids = tuple(
                value
                for value in (
                    self.tool_worker_client_id,
                    self.worker_client_id,
                    self.directory_client_id,
                )
                if value
            )
            if len(set(client_ids)) != len(client_ids):
                problems.append(
                    "Tool Worker, Agent Worker, and directory OAuth clients must be different"
                )
            if not 5 <= self.tool_worker_lease_seconds <= 3600:
                problems.append("COIFESP_TOOL_WORKER_LEASE_SECONDS must be between 5 and 3600")
            if not 0 < self.tool_worker_heartbeat_seconds < self.tool_worker_lease_seconds:
                problems.append(
                    "COIFESP_TOOL_WORKER_HEARTBEAT_SECONDS must be shorter than the lease"
                )
            if not 0.05 <= self.tool_worker_idle_poll_seconds <= 60:
                problems.append("COIFESP_TOOL_WORKER_IDLE_POLL_SECONDS must be between 0.05 and 60")

        if require_sandbox:
            from pathlib import Path

            from .sandbox import load_code_profiles

            if self.sandbox_runtime not in {"docker", "podman"}:
                problems.append("COIFESP_SANDBOX_RUNTIME must be docker or podman")
            if not self.sandbox_workspace_root:
                problems.append("COIFESP_SANDBOX_WORKSPACE_ROOT is required")
            elif not Path(self.sandbox_workspace_root).is_absolute():
                problems.append("COIFESP_SANDBOX_WORKSPACE_ROOT must be absolute")
            if not self.sandbox_profiles_json:
                problems.append("COIFESP_SANDBOX_PROFILES_JSON is required")
            else:
                try:
                    load_code_profiles(self.sandbox_profiles_json)
                except ValueError:
                    problems.append("COIFESP_SANDBOX_PROFILES_JSON is invalid")
        if self.office_data_classification not in {
            "public",
            "internal",
            "confidential",
            "restricted",
        }:
            problems.append("COIFESP_OFFICE_DATA_CLASSIFICATION is invalid")

        if require_memory or self.environment is Environment.PRODUCTION:
            if not self.memory_key_id:
                problems.append("COIFESP_MEMORY_KEY_ID is required for encrypted memory")
            if self.memory_master_key is None and not self.memory_keys:
                problems.append(
                    "COIFESP_MEMORY_MASTER_KEY or COIFESP_MEMORY_KEYS_JSON is required "
                    "for encrypted memory"
                )
            if self.memory_master_key is not None and not _is_base64url_32_bytes(
                self.memory_master_key.reveal()
            ):
                problems.append("COIFESP_MEMORY_MASTER_KEY must be Base64URL-encoded 32 bytes")
            if any(not _is_base64url_32_bytes(item.material.reveal()) for item in self.memory_keys):
                problems.append(
                    "every COIFESP_MEMORY_KEYS_JSON key must be Base64URL-encoded 32 bytes"
                )

        if require_llm:
            if not self.llm_providers:
                llm_required = {
                    "COIFESP_LLM_PROVIDER": self.llm_provider,
                    "COIFESP_LLM_MODEL": self.llm_model,
                    "COIFESP_LLM_API_KEY": self.llm_api_key,
                }
                problems.extend(
                    f"{name} is required for live model execution"
                    for name, value in llm_required.items()
                    if value is None
                )
                supported_providers = {"openai", "openai_compatible", "anthropic"}
                if self.llm_provider and self.llm_provider not in supported_providers:
                    problems.append(
                        "COIFESP_LLM_PROVIDER must be openai, openai_compatible, or anthropic"
                    )
                if self.llm_provider == "openai_compatible" and not self.llm_base_url:
                    problems.append(
                        "COIFESP_LLM_BASE_URL is required for openai_compatible providers"
                    )
                if self.llm_base_url and not _is_secure_http_url(self.llm_base_url):
                    if self.environment is Environment.PRODUCTION:
                        problems.append("COIFESP_LLM_BASE_URL must be an https URL in production")
        if not 0 <= self.llm_max_failover_attempts <= 10:
            problems.append("COIFESP_LLM_MAX_FAILOVER_ATTEMPTS must be between 0 and 10")
        if not 1 <= self.llm_circuit_failure_threshold <= 100:
            problems.append("COIFESP_LLM_CIRCUIT_FAILURE_THRESHOLD must be between 1 and 100")

        if (
            not self.service_name
            or len(self.service_name) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in self.service_name
            )
        ):
            problems.append("COIFESP_SERVICE_NAME is invalid")
        if self.telemetry_enabled:
            if not self.otlp_traces_endpoint:
                problems.append(
                    "COIFESP_OTLP_TRACES_ENDPOINT is required when telemetry is enabled"
                )
            elif not _is_http_url(self.otlp_traces_endpoint):
                problems.append("COIFESP_OTLP_TRACES_ENDPOINT must be an HTTP(S) URL")
            elif self.environment is Environment.PRODUCTION and not _is_secure_http_url(
                self.otlp_traces_endpoint
            ):
                problems.append("COIFESP_OTLP_TRACES_ENDPOINT must be an https URL in production")
        if (
            not self.metrics_path.startswith("/")
            or self.metrics_path.startswith("//")
            or len(self.metrics_path) > 128
            or "?" in self.metrics_path
            or "#" in self.metrics_path
        ):
            problems.append("COIFESP_METRICS_PATH is invalid")
        if self.environment is Environment.PRODUCTION and self.metrics_enabled:
            if (
                self.metrics_bearer_token is None
                or len(self.metrics_bearer_token.reveal().encode("utf-8")) < 32
            ):
                problems.append(
                    "COIFESP_METRICS_BEARER_TOKEN must contain at least 32 bytes "
                    "when production metrics are enabled"
                )
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            problems.append("COIFESP_LOG_LEVEL is invalid")

        if problems:
            raise ConfigurationError("; ".join(problems))


def _is_secure_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _is_base64url_32_bytes(value: str) -> bool:
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 32


_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_REGION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_ENV = re.compile(r"^COIFESP_[A-Z0-9_]{1,112}$")
_API_KEY_ENV = re.compile(r"^COIFESP_[A-Z0-9_]{0,96}API_KEY$")
_PROVIDER_KEYS = frozenset(
    {
        "provider_id",
        "kind",
        "model",
        "base_url",
        "api_key_env",
        "capabilities",
        "max_data_classification",
        "region",
        "external",
        "context_window_tokens",
        "max_output_tokens",
        "input_microusd_per_million_tokens",
        "output_microusd_per_million_tokens",
        "priority",
        "max_concurrency",
        "timeout_seconds",
        "max_retries",
    }
)
_PROVIDER_OPTIONAL_KEYS = frozenset({"tokenizer_encoding"})


def _parse_secret_keyring(
    raw: str | None, *, source: Mapping[str, str]
) -> tuple[SecretKeyConfig, ...]:
    if raw is None:
        return ()
    if len(raw.encode("utf-8")) > 16_384:
        raise ConfigurationError("secret keyring JSON is too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("secret keyring must be valid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ConfigurationError("secret keyring must contain 1 to 16 entries")
    result: list[SecretKeyConfig] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != {"key_id", "key_env"}:
            raise ConfigurationError(
                f"secret keyring entry {index} must contain key_id and key_env"
            )
        key_id = value["key_id"]
        key_env = value["key_env"]
        if (
            not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
            or key_id in seen
            or not isinstance(key_env, str)
            or not _SECRET_ENV.fullmatch(key_env)
        ):
            raise ConfigurationError(f"secret keyring entry {index} is invalid")
        material = source.get(key_env, "")
        if len(material.encode("utf-8")) < 32:
            raise ConfigurationError(f"{key_env} must contain at least 32 bytes")
        seen.add(key_id)
        result.append(SecretKeyConfig(key_id, SecretValue(material)))
    return tuple(result)


def _parse_model_providers(
    raw: str | None,
    *,
    source: Mapping[str, str],
    environment: Environment,
) -> tuple[ModelProviderConfig, ...]:
    if raw is None:
        return ()
    if len(raw.encode("utf-8")) > 65_536:
        raise ConfigurationError("COIFESP_LLM_PROVIDERS_JSON is too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("COIFESP_LLM_PROVIDERS_JSON must be valid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ConfigurationError("COIFESP_LLM_PROVIDERS_JSON must contain 1 to 32 providers")

    providers: list[ModelProviderConfig] = []
    identifiers: set[str] = set()
    for index, value in enumerate(values):
        prefix = f"COIFESP_LLM_PROVIDERS_JSON[{index}]"
        if (not isinstance(value, dict) or not _PROVIDER_KEYS.issubset(value)
                or not set(value).issubset(_PROVIDER_KEYS | _PROVIDER_OPTIONAL_KEYS)):
            raise ConfigurationError(f"{prefix} must contain exactly the documented fields")

        def string(name: str, *, maximum: int = 256) -> str:
            item = value[name]
            if not isinstance(item, str) or not item.strip() or len(item) > maximum:
                raise ConfigurationError(f"{prefix}.{name} is invalid")
            return item.strip()

        def integer(name: str, *, minimum: int, maximum: int) -> int:
            item = value[name]
            if type(item) is not int or not minimum <= item <= maximum:
                raise ConfigurationError(f"{prefix}.{name} is invalid")
            return item

        provider_id = string("provider_id", maximum=64)
        if not _PROVIDER_ID.fullmatch(provider_id) or provider_id in identifiers:
            raise ConfigurationError(f"{prefix}.provider_id is invalid or duplicated")
        identifiers.add(provider_id)
        kind = string("kind", maximum=32)
        if kind not in {"openai", "openai_compatible", "anthropic"}:
            raise ConfigurationError(f"{prefix}.kind is unsupported")
        model = string("model")
        tokenizer_value = value.get("tokenizer_encoding")
        if tokenizer_value is not None and (not isinstance(tokenizer_value, str)
                or not tokenizer_value.strip() or len(tokenizer_value) > 128):
            raise ConfigurationError(f"{prefix}.tokenizer_encoding is invalid")
        base_url_value = value["base_url"]
        if base_url_value is not None and (
            not isinstance(base_url_value, str) or not _is_http_url(base_url_value)
        ):
            raise ConfigurationError(f"{prefix}.base_url must be an HTTP(S) URL or null")
        base_url = base_url_value.strip() if isinstance(base_url_value, str) else None
        if kind == "openai_compatible" and base_url is None:
            raise ConfigurationError(f"{prefix}.base_url is required")
        if environment is Environment.PRODUCTION and base_url is not None:
            if not _is_secure_http_url(base_url):
                raise ConfigurationError(f"{prefix}.base_url must use https in production")
        api_key_env = string("api_key_env", maximum=128)
        if not _API_KEY_ENV.fullmatch(api_key_env):
            raise ConfigurationError(f"{prefix}.api_key_env is invalid")
        api_key_value = source.get(api_key_env, "").strip()
        if not api_key_value:
            raise ConfigurationError(f"{api_key_env} is required by {prefix}")

        capabilities_value = value["capabilities"]
        if (
            not isinstance(capabilities_value, list)
            or len(capabilities_value) > 16
            or any(not isinstance(item, str) for item in capabilities_value)
        ):
            raise ConfigurationError(f"{prefix}.capabilities is invalid")
        capabilities = frozenset(capabilities_value)
        supported_capabilities = {"tool_calling", "streaming", "json_output", "vision"}
        if not capabilities.issubset(supported_capabilities):
            raise ConfigurationError(f"{prefix}.capabilities contains unsupported values")
        classification = string("max_data_classification", maximum=32).lower()
        if classification not in {"public", "internal", "confidential", "restricted"}:
            raise ConfigurationError(f"{prefix}.max_data_classification is invalid")
        region = string("region", maximum=128)
        if not _REGION_ID.fullmatch(region):
            raise ConfigurationError(f"{prefix}.region is invalid")
        external = value["external"]
        if type(external) is not bool:
            raise ConfigurationError(f"{prefix}.external must be a boolean")

        context_window = integer("context_window_tokens", minimum=1, maximum=100_000_000)
        max_output = integer("max_output_tokens", minimum=1, maximum=context_window)
        timeout_value = value["timeout_seconds"]
        if (
            not isinstance(timeout_value, (int, float))
            or isinstance(timeout_value, bool)
            or not 0 < float(timeout_value) <= 600
        ):
            raise ConfigurationError(f"{prefix}.timeout_seconds is invalid")
        providers.append(
            ModelProviderConfig(
                provider_id=provider_id,
                kind=kind,
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
                api_key=SecretValue(api_key_value),
                capabilities=capabilities,
                max_data_classification=classification,
                region=region,
                external=external,
                context_window_tokens=context_window,
                max_output_tokens=max_output,
                input_microusd_per_million_tokens=integer(
                    "input_microusd_per_million_tokens",
                    minimum=0,
                    maximum=10**15,
                ),
                output_microusd_per_million_tokens=integer(
                    "output_microusd_per_million_tokens",
                    minimum=0,
                    maximum=10**15,
                ),
                priority=integer("priority", minimum=0, maximum=10_000),
                max_concurrency=integer("max_concurrency", minimum=1, maximum=100_000),
                timeout_seconds=float(timeout_value),
                max_retries=integer("max_retries", minimum=0, maximum=10),
                tokenizer_encoding=(tokenizer_value.strip()
                    if isinstance(tokenizer_value, str) else None),
            )
        )
    return tuple(providers)
