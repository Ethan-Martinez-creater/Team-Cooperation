import base64
import json

import pytest

from coifesp_harness.config import ConfigurationError, SecretValue, Settings
from coifesp_harness.runtime.providers import build_model_gateway


def test_secret_value_does_not_leak_through_stringification() -> None:
    value = SecretValue("very-secret-value")
    assert "very-secret-value" not in repr(value)
    assert "very-secret-value" not in str(value)


def test_production_configuration_fails_closed() -> None:
    settings = Settings.from_environment({"COIFESP_ENV": "production"})
    with pytest.raises(ConfigurationError) as captured:
        settings.validate()
    message = str(captured.value)
    assert "COIFESP_DATABASE_URL" in message
    assert "COIFESP_OIDC_ISSUER" in message
    assert "COIFESP_AUDIT_SIGNING_KEY" in message


def test_live_model_configuration_is_explicit() -> None:
    settings = Settings.from_environment({"COIFESP_ENV": "development"})
    with pytest.raises(ConfigurationError, match="COIFESP_LLM_PROVIDER"):
        settings.validate(require_llm=True)


def test_openai_compatible_provider_requires_base_url() -> None:
    settings = Settings.from_environment(
        {
            "COIFESP_ENV": "development",
            "COIFESP_LLM_PROVIDER": "openai_compatible",
            "COIFESP_LLM_MODEL": "model-id",
            "COIFESP_LLM_API_KEY": "secret",
        }
    )
    with pytest.raises(ConfigurationError, match="COIFESP_LLM_BASE_URL"):
        settings.validate(require_llm=True)


def test_durable_worker_requires_separate_bounded_oauth_clients() -> None:
    settings = Settings.from_environment({"COIFESP_ENV": "development"})
    with pytest.raises(ConfigurationError, match="COIFESP_WORKER_TOKEN_ENDPOINT"):
        settings.validate(require_worker=True)


def test_local_workers_require_tenant_but_not_oidc_clients() -> None:
    agent = Settings.from_environment({
        "COIFESP_ENV": "development", "COIFESP_AUTH_MODE": "local",
        "COIFESP_LOCAL_WORKER_TENANTS": "team-product,team-engineering,team-quality",
    })
    agent.validate(require_worker=True)
    agent.validate(require_tool_worker=True)
    assert agent.worker_tenant_ids == ("team-product", "team-engineering", "team-quality")
    assert agent.tool_worker_tenant_ids == agent.worker_tenant_ids


def test_local_worker_still_requires_explicit_tenant() -> None:
    settings = Settings.from_environment({
        "COIFESP_ENV": "development", "COIFESP_AUTH_MODE": "local",
    })
    with pytest.raises(ConfigurationError, match="COIFESP_WORKER_TENANTS"):
        settings.validate(require_worker=True)


@pytest.mark.parametrize(
    "value", ["", "team-a,team-a", "team-a,bad tenant"]
)
def test_local_worker_tenant_set_rejects_empty_duplicate_or_invalid(value) -> None:
    environment = {
        "COIFESP_ENV": "development", "COIFESP_AUTH_MODE": "local",
        "COIFESP_LOCAL_WORKER_TENANTS": value,
    }
    if value == "":
        settings = Settings.from_environment(environment)
        with pytest.raises(ConfigurationError, match="COIFESP_WORKER_TENANTS"):
            settings.validate(require_worker=True)
    else:
        with pytest.raises(ConfigurationError, match="unique valid tenant"):
            Settings.from_environment(environment)


def test_legacy_single_tenant_maps_to_singleton_pool_and_conflicts_fail() -> None:
    settings = Settings.from_environment({
        "COIFESP_ENV": "development", "COIFESP_AUTH_MODE": "oidc",
        "COIFESP_WORKER_TENANT_ID": "team-a",
        "COIFESP_TOOL_WORKER_TENANT_ID": "team-a",
    })
    assert settings.worker_tenant_ids == ("team-a",)
    assert settings.tool_worker_tenant_ids == ("team-a",)
    with pytest.raises(ConfigurationError, match="conflicts"):
        Settings.from_environment({
            "COIFESP_ENV": "development", "COIFESP_WORKER_TENANT_ID": "team-a",
            "COIFESP_WORKER_TENANTS": "team-a,team-b",
        })


def test_production_rejects_local_worker_tenant_configuration() -> None:
    settings = Settings.from_environment({
        "COIFESP_ENV": "production", "COIFESP_AUTH_MODE": "oidc",
        "COIFESP_LOCAL_WORKER_TENANTS": "team-a",
    })
    with pytest.raises(ConfigurationError, match="forbidden in production"):
        settings.validate()


def test_production_tls_ca_bundle_must_be_absolute() -> None:
    settings = Settings.from_environment({
        "COIFESP_ENV": "production",
        "COIFESP_TLS_CA_BUNDLE": "relative-ca.pem",
    })
    with pytest.raises(ConfigurationError, match="COIFESP_TLS_CA_BUNDLE"):
        settings.validate()


def test_tool_worker_requires_a_third_distinct_oauth_client() -> None:
    values = {
        "COIFESP_ENV": "development",
        "COIFESP_OIDC_ISSUER": "http://127.0.0.1:8080/realms/coifesp",
        "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
        "COIFESP_OIDC_AUTHORIZED_PARTIES": "coifesp-tool-worker",
        "COIFESP_WORKER_CLIENT_ID": "coifesp-agent-worker",
        "COIFESP_DIRECTORY_CLIENT_ID": "coifesp-directory-reader",
        "COIFESP_TOOL_WORKER_TOKEN_ENDPOINT": "http://127.0.0.1:8080/token",
        "COIFESP_TOOL_WORKER_CLIENT_ID": "coifesp-agent-worker",
        "COIFESP_TOOL_WORKER_CLIENT_SECRET": "x" * 32,
        "COIFESP_TOOL_WORKER_TENANT_ID": "team-a",
    }
    with pytest.raises(ConfigurationError, match="must be different"):
        Settings.from_environment(values).validate(require_tool_worker=True)


def test_sandbox_requires_runtime_absolute_root_and_strict_profiles() -> None:
    values = {
        "COIFESP_ENV": "development",
        "COIFESP_SANDBOX_RUNTIME": "host",
        "COIFESP_SANDBOX_WORKSPACE_ROOT": "relative",
        "COIFESP_SANDBOX_PROFILES_JSON": "[]",
    }
    with pytest.raises(ConfigurationError) as captured:
        Settings.from_environment(values).validate(require_sandbox=True)
    reason = str(captured.value)
    assert "COIFESP_SANDBOX_RUNTIME" in reason
    assert "COIFESP_SANDBOX_WORKSPACE_ROOT" in reason
    assert "COIFESP_SANDBOX_PROFILES_JSON" in reason

    shared = {
        "COIFESP_ENV": "development",
        "COIFESP_OIDC_ISSUER": "http://127.0.0.1:8080/realms/coifesp",
        "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
        "COIFESP_OIDC_AUTHORIZED_PARTIES": "coifesp-agent-worker",
        "COIFESP_WORKER_TOKEN_ENDPOINT": "http://127.0.0.1:8080/token",
        "COIFESP_WORKER_CLIENT_ID": "same-client",
        "COIFESP_WORKER_CLIENT_SECRET": "x" * 32,
        "COIFESP_WORKER_TENANT_ID": "team-a",
        "COIFESP_DIRECTORY_API_BASE_URL": "http://127.0.0.1:8080",
        "COIFESP_DIRECTORY_REALM": "coifesp",
        "COIFESP_DIRECTORY_TOKEN_ENDPOINT": "http://127.0.0.1:8080/token",
        "COIFESP_DIRECTORY_CLIENT_ID": "same-client",
        "COIFESP_DIRECTORY_CLIENT_SECRET": "y" * 32,
    }
    value = Settings.from_environment(shared)
    with pytest.raises(ConfigurationError, match="must be different"):
        value.validate(require_auth=True, require_worker=True)


def test_encrypted_memory_requires_exact_base64url_key() -> None:
    settings = Settings.from_environment(
        {
            "COIFESP_ENV": "development",
            "COIFESP_MEMORY_KEY_ID": "local-dev-v1",
            "COIFESP_MEMORY_MASTER_KEY": "not-a-valid-32-byte-key",
        }
    )
    with pytest.raises(ConfigurationError, match="Base64URL-encoded 32 bytes"):
        settings.validate(require_memory=True)


def test_versioned_keyrings_use_secret_references_and_validate_active_versions() -> None:
    memory_v1 = base64.urlsafe_b64encode(b"m" * 32).decode("ascii")
    memory_v2 = base64.urlsafe_b64encode(b"n" * 32).decode("ascii")
    values = {
        "COIFESP_ENV": "development",
        "COIFESP_MEMORY_KEY_ID": "memory-v2",
        "COIFESP_MEMORY_KEYS_JSON": json.dumps(
            [
                {"key_id": "memory-v1", "key_env": "COIFESP_MEMORY_KEY_V1"},
                {"key_id": "memory-v2", "key_env": "COIFESP_MEMORY_KEY_V2"},
            ]
        ),
        "COIFESP_MEMORY_KEY_V1": memory_v1,
        "COIFESP_MEMORY_KEY_V2": memory_v2,
    }
    settings = Settings.from_environment(values)
    settings.validate(require_memory=True)
    assert {item.key_id for item in settings.memory_keys} == {"memory-v1", "memory-v2"}
    assert memory_v1 not in repr(settings.memory_keys)
    assert memory_v2 not in repr(settings.memory_keys)

    values["COIFESP_MEMORY_KEY_ID"] = "memory-v3"
    with pytest.raises(ConfigurationError, match="must identify an available key"):
        Settings.from_environment(values).validate(require_memory=True)


def test_versioned_keyring_rejects_inline_material_and_missing_secret_reference() -> None:
    with pytest.raises(ConfigurationError, match="key_id and key_env"):
        Settings.from_environment(
            {"COIFESP_MEMORY_KEYS_JSON": json.dumps([{"key_id": "memory-v1", "key": "x" * 44}])}
        )
    with pytest.raises(ConfigurationError, match="COIFESP_MEMORY_KEY_V1"):
        Settings.from_environment(
            {
                "COIFESP_MEMORY_KEYS_JSON": json.dumps(
                    [
                        {
                            "key_id": "memory-v1",
                            "key_env": "COIFESP_MEMORY_KEY_V1",
                        }
                    ]
                )
            }
        )


def provider_registry(**overrides):
    value = {
        "provider_id": "deepseek",
        "kind": "openai_compatible",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "COIFESP_LLM_API_KEY",
        "capabilities": ["tool_calling", "streaming", "json_output"],
        "max_data_classification": "public",
        "region": "external",
        "external": True,
        "context_window_tokens": 64000,
        "max_output_tokens": 8192,
        "input_microusd_per_million_tokens": 100000,
        "output_microusd_per_million_tokens": 200000,
        "priority": 10,
        "max_concurrency": 8,
        "timeout_seconds": 60,
        "max_retries": 2,
    }
    value.update(overrides)
    return json.dumps([value])


def test_multi_provider_registry_resolves_secret_by_environment_reference() -> None:
    value = Settings.from_environment(
        {
            "COIFESP_ENV": "development",
            "COIFESP_LLM_API_KEY": "provider-secret",
            "COIFESP_LLM_PROVIDERS_JSON": provider_registry(),
        }
    )
    value.validate(require_llm=True)
    assert len(value.llm_providers) == 1
    assert value.llm_providers[0].provider_id == "deepseek"
    assert value.llm_providers[0].tokenizer_encoding is None
    assert "provider-secret" not in repr(value.llm_providers)
    gateway = build_model_gateway(value)
    assert gateway.max_failover_attempts == 2


def test_local_external_internal_provider_authorization_is_explicit() -> None:
    value = Settings.from_environment(
        {
            "COIFESP_ENV": "development",
            "COIFESP_AUTH_MODE": "local",
            "COIFESP_LLM_API_KEY": "provider-secret",
            "COIFESP_LLM_PROVIDERS_JSON": provider_registry(
                max_data_classification="internal"
            ),
            "COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS": "deepseek",
        }
    )
    value.validate(require_llm=True)
    assert value.local_external_internal_provider_ids == ("deepseek",)


@pytest.mark.parametrize(
    ("environment", "auth_mode"),
    (("production", "oidc"), ("development", "builtin")),
)
def test_local_external_internal_provider_authorization_fails_outside_local(
    environment, auth_mode
) -> None:
    value = Settings.from_environment(
        {
            "COIFESP_ENV": environment,
            "COIFESP_AUTH_MODE": auth_mode,
            "COIFESP_LLM_API_KEY": "provider-secret",
            "COIFESP_LLM_PROVIDERS_JSON": provider_registry(
                max_data_classification="internal"
            ),
            "COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS": "deepseek",
        }
    )
    with pytest.raises(ConfigurationError, match="allowed only"):
        value.validate(require_llm=True)


def test_local_external_internal_provider_must_be_registered_and_internal() -> None:
    base = {
        "COIFESP_ENV": "development",
        "COIFESP_AUTH_MODE": "local",
        "COIFESP_LLM_API_KEY": "provider-secret",
        "COIFESP_LLM_PROVIDERS_JSON": provider_registry(),
    }
    public = Settings.from_environment(
        {**base, "COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS": "deepseek"}
    )
    with pytest.raises(ConfigurationError, match="max_data_classification"):
        public.validate(require_llm=True)
    unknown = Settings.from_environment(
        {**base, "COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS": "unknown"}
    )
    with pytest.raises(ConfigurationError, match="unregistered"):
        unknown.validate(require_llm=True)


def test_provider_registry_accepts_explicit_local_tokenizer_encoding() -> None:
    value = Settings.from_environment({"COIFESP_ENV": "development",
        "COIFESP_LLM_API_KEY": "provider-secret",
        "COIFESP_LLM_PROVIDERS_JSON": provider_registry(tokenizer_encoding="cl100k_base")})
    assert value.llm_providers[0].tokenizer_encoding == "cl100k_base"
    gateway = build_model_gateway(value)
    assert gateway._states["deepseek"].registration.token_counter.exact is True


def test_multi_provider_registry_fails_closed_on_missing_secret_or_unknown_field() -> None:
    with pytest.raises(ConfigurationError, match="COIFESP_LLM_API_KEY"):
        Settings.from_environment(
            {
                "COIFESP_ENV": "development",
                "COIFESP_LLM_PROVIDERS_JSON": provider_registry(),
            }
        )
    raw = json.loads(provider_registry())
    raw[0]["unreviewed_option"] = True
    with pytest.raises(ConfigurationError, match="exactly the documented fields"):
        Settings.from_environment(
            {
                "COIFESP_ENV": "development",
                "COIFESP_LLM_API_KEY": "provider-secret",
                "COIFESP_LLM_PROVIDERS_JSON": json.dumps(raw),
            }
        )
