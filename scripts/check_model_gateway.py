import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402
from coifesp_harness.runtime import (  # noqa: E402
    Message,
    ModelCapability,
    ModelRoutePolicy,
    ModelStreamEventType,
)
from coifesp_harness.runtime.providers import build_model_gateway  # noqa: E402
from coifesp_harness.security import Classification  # noqa: E402


def load_settings() -> Settings:
    from dotenv import load_dotenv

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        raise ConfigurationError("configuration file is missing: .env")
    load_dotenv(env_path, override=True)
    value = Settings.from_environment()
    value.validate(require_llm=True)
    if not value.llm_providers:
        raise ConfigurationError(
            "COIFESP_LLM_PROVIDERS_JSON is required for the gateway check"
        )
    return value


async def check(*, provider_id: str | None, streaming: bool) -> None:
    settings = load_settings()
    selected = provider_id or settings.llm_providers[0].provider_id
    if selected not in {item.provider_id for item in settings.llm_providers}:
        raise ConfigurationError("requested provider_id is not registered")
    gateway = build_model_gateway(settings)
    capabilities = (
        frozenset({ModelCapability.STREAMING}) if streaming else frozenset()
    )
    policy = ModelRoutePolicy(
        data_classification=Classification.PUBLIC,
        required_capabilities=capabilities,
        allowed_provider_ids=frozenset({selected}),
        max_call_cost_microusd=1_000_000,
        max_output_tokens=256,
        max_call_total_tokens=2_048,
    )
    messages = (Message("user", "Reply briefly with COIFESP_MODEL_OK."),)
    if streaming:
        deltas = 0
        final = None
        async for event in gateway.stream_routed(
            messages=messages,
            tools=(),
            correlation_id="coifesp-gateway-connectivity-check",
            route_policy=policy,
        ):
            if event.event_type is ModelStreamEventType.TEXT_DELTA:
                deltas += 1
            else:
                final = event.response
        if final is None or not final.text or deltas == 0:
            raise RuntimeError("provider stream did not return text and a final response")
        response = final
    else:
        response = await gateway.complete_routed(
            messages=messages,
            tools=(),
            correlation_id="coifesp-gateway-connectivity-check",
            route_policy=policy,
        )
        if not response.text:
            raise RuntimeError("provider did not return text")
    print(
        "MODEL_GATEWAY_OK "
        f"provider_id={response.provider_id} model={response.model} "
        f"finish_reason={response.finish_reason or 'unknown'} "
        f"input_tokens={response.input_tokens} output_tokens={response.output_tokens} "
        f"cost_microusd={response.cost_microusd} "
        f"streaming={'yes' if streaming else 'no'} secrets=redacted"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the configured model gateway safely.")
    parser.add_argument("--provider-id")
    parser.add_argument("--non-streaming", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(
            check(
                provider_id=arguments.provider_id,
                streaming=not arguments.non_streaming,
            )
        )
        return 0
    except Exception as exc:
        print(
            "MODEL_GATEWAY_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
