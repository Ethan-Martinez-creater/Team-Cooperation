from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("required dependency is unavailable: python-dotenv") from exc

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        raise ConfigurationError(f"configuration file is missing: {env_path.name}")
    load_dotenv(dotenv_path=env_path, override=True)
    settings = Settings.from_environment()
    settings.validate(require_llm=True)

    problems: list[str] = []
    if not settings.database_url:
        problems.append("COIFESP_DATABASE_URL is required for the database check")
    for name, secret in (
        ("COIFESP_AUDIT_SIGNING_KEY", settings.audit_signing_key),
        ("COIFESP_ENVELOPE_SIGNING_KEY", settings.envelope_signing_key),
    ):
        if secret is None:
            problems.append(f"{name} is required")
        elif len(secret.reveal().encode("utf-8")) < 32:
            problems.append(f"{name} must contain at least 32 bytes")
    if problems:
        raise ConfigurationError("; ".join(problems))
    return settings


def check_database(settings: Settings) -> bool:
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        assert settings.database_url is not None
        parsed = make_url(settings.database_url)
        target = (
            f"{parsed.get_backend_name()}://{parsed.host or 'local'}:"
            f"{parsed.port or 'default'}/{parsed.database or ''}"
        )
        engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as connection:
            value = connection.execute(text("SELECT 1")).scalar_one()
        engine.dispose()
        if value != 1:
            print(f"DATABASE_FAILED target={target} reason=unexpected_probe_result")
            return False
        print(f"DATABASE_OK target={target} probe=SELECT_1")
        return True
    except Exception as exc:
        print(f"DATABASE_FAILED error_type={type(exc).__name__}")
        return False


def check_llm(settings: Settings) -> bool:
    try:
        from coifesp_harness.runtime import Message
        from coifesp_harness.runtime.providers import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider.from_settings(
            settings,
            timeout_seconds=30.0,
            max_retries=0,
            max_output_tokens=32,
        )
        response = asyncio.run(
            provider.complete(
                messages=(
                    Message(
                        role="system",
                        content="This is a connectivity probe. Return a concise response.",
                    ),
                    Message(role="user", content="Reply with OK."),
                ),
                tools=(),
                correlation_id="connectivity-probe",
            )
        )
        if not response.text and not response.tool_calls:
            print(
                f"LLM_FAILED provider={settings.llm_provider} "
                f"model={settings.llm_model} reason=empty_response"
            )
            return False
        print(
            f"LLM_OK provider={settings.llm_provider} "
            f"model={settings.llm_model} response_received=yes"
        )
        return True
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        status = f" status_code={status_code}" if status_code is not None else ""
        print(f"LLM_FAILED error_type={type(exc).__name__}{status}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely probe configured database and LLM without printing secrets."
    )
    parser.add_argument("--database-only", action="store_true")
    parser.add_argument("--llm-only", action="store_true")
    args = parser.parse_args()
    if args.database_only and args.llm_only:
        parser.error("--database-only and --llm-only are mutually exclusive")

    try:
        settings = load_settings()
    except Exception as exc:
        print(f"CONFIG_FAILED error_type={type(exc).__name__} reason={exc}")
        return 2

    print("CONFIG_OK secrets=redacted")
    checks: list[bool] = []
    if not args.llm_only:
        checks.append(check_database(settings))
    if not args.database_only:
        checks.append(check_llm(settings))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
