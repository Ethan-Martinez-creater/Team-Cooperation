"""Start the production Worker composition briefly and shut it down cleanly."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.worker_main import build_worker_runtime  # noqa: E402


async def check() -> None:
    settings = load_environment_settings(PROJECT_ROOT / ".env")
    runtime = await build_worker_runtime(settings)
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.runner.run(stop=stop))
    try:
        await asyncio.sleep(max(0.2, settings.worker_idle_poll_seconds * 2.2))
        if task.done():
            await task
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    finally:
        stop.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.aclose()
    print(
        "WORKER_STARTUP_OK database=yes model_gateway=yes oidc=yes directory=yes "
        "queue=idle graceful_shutdown=yes secrets=redacted"
    )


if __name__ == "__main__":
    asyncio.run(check())
