from __future__ import annotations

from typing import Any

from ...config import ConfigurationError, ModelProviderConfig, Settings
from ...security import Classification
from ..models import ModelCapability, ModelRoutePolicy
from .anthropic import AnthropicProvider
from .gateway import ModelGateway
from .models import (
    ModelGatewayObserver,
    ProviderDescriptor,
    ProviderRegistration,
)
from .openai_compatible import OpenAICompatibleProvider
from .tokenizers import TiktokenCounter


def build_model_gateway(
    settings: Settings,
    *,
    default_route_policy: ModelRoutePolicy | None = None,
    observer: ModelGatewayObserver | None = None,
) -> ModelGateway:
    """Build the production gateway from an explicit, secret-safe provider registry."""

    settings.validate(require_llm=True)
    if not settings.llm_providers:
        raise ConfigurationError(
            "COIFESP_LLM_PROVIDERS_JSON is required for the multi-provider gateway"
        )
    registrations = tuple(_registration(value) for value in settings.llm_providers)
    return ModelGateway(
        registrations,
        default_route_policy=default_route_policy,
        max_failover_attempts=settings.llm_max_failover_attempts,
        concurrency_wait_seconds=settings.llm_concurrency_wait_seconds,
        circuit_failure_threshold=settings.llm_circuit_failure_threshold,
        circuit_cooldown_seconds=settings.llm_circuit_cooldown_seconds,
        observer=observer,
    )


def _registration(config: ModelProviderConfig) -> ProviderRegistration:
    provider = _provider(config)
    descriptor = ProviderDescriptor(
        provider_id=config.provider_id,
        model=config.model,
        capabilities=frozenset(ModelCapability(item) for item in config.capabilities),
        max_data_classification=Classification[config.max_data_classification.upper()],
        region=config.region,
        external=config.external,
        context_window_tokens=config.context_window_tokens,
        max_output_tokens=config.max_output_tokens,
        input_microusd_per_million_tokens=(
            config.input_microusd_per_million_tokens
        ),
        output_microusd_per_million_tokens=(
            config.output_microusd_per_million_tokens
        ),
        priority=config.priority,
        max_concurrency=config.max_concurrency,
    )
    counter = (TiktokenCounter(config.tokenizer_encoding)
        if config.tokenizer_encoding is not None else None)
    return ProviderRegistration(descriptor=descriptor, provider=provider,
        token_counter=counter)


def _provider(config: ModelProviderConfig):
    if config.kind in {"openai", "openai_compatible"}:
        from openai import AsyncOpenAI

        options: dict[str, Any] = {
            "api_key": config.api_key.reveal(),
            "timeout": config.timeout_seconds,
            "max_retries": config.max_retries,
        }
        if config.base_url is not None:
            options["base_url"] = config.base_url
        return OpenAICompatibleProvider(
            client=AsyncOpenAI(**options),
            model=config.model,
            provider_id=config.provider_id,
            max_output_tokens=config.max_output_tokens,
        )
    if config.kind == "anthropic":
        from anthropic import AsyncAnthropic

        options = {
            "api_key": config.api_key.reveal(),
            "timeout": config.timeout_seconds,
            "max_retries": config.max_retries,
        }
        if config.base_url is not None:
            options["base_url"] = config.base_url
        return AnthropicProvider(
            client=AsyncAnthropic(**options),
            model=config.model,
            provider_id=config.provider_id,
            max_output_tokens=config.max_output_tokens,
        )
    raise AssertionError("validated provider kind is unsupported")
