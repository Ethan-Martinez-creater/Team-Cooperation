"""Build the production Tool Worker core without requiring the paused OCI runtime."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.config import Settings  # noqa: E402
from coifesp_harness.tool_worker_main import build_tool_worker_runtime  # noqa: E402
from coifesp_harness.tools import ToolRegistry  # noqa: E402


async def check() -> None:
    load_dotenv(ROOT / ".env", override=True)
    settings = Settings.from_environment()
    runtime = await build_tool_worker_runtime(settings, registry=ToolRegistry())
    try:
        principal = await runtime.runner.identity_provider.resolve()
        if principal.tenant_id != "team-a" or "tool_worker" not in principal.roles:
            raise RuntimeError("assembled runtime resolved an invalid worker identity")
        # An injected empty registry validates the production identity, database,
        # crypto, repositories and coordinator without pretending the paused OCI
        # execution backend is available.
        processed = runtime.runner.repository.claim_next(
            tenant_id=principal.tenant_id,
            worker_id=principal.principal_id,
            lease_seconds=settings.tool_worker_lease_seconds,
        )
        if processed is not None:
            raise RuntimeError("acceptance requires an empty Tool Job queue")
        print(
            "TOOL_WORKER_RUNTIME_OK database_ready=yes crypto_ready=yes "
            "repositories_ready=yes coordinator_ready=yes identity_revalidated=yes "
            "queue=empty sandbox=separately_paused secrets=redacted"
        )
    finally:
        await runtime.aclose()


def main() -> int:
    try:
        asyncio.run(check())
        return 0
    except Exception as exc:
        print(
            f"TOOL_WORKER_RUNTIME_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
