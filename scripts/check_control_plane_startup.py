from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.control_plane import create_application  # noqa: E402


async def check_startup() -> None:
    app = create_application()
    async with app.router.lifespan_context(app):
        if not app.state.database_readiness():
            raise RuntimeError("control-plane database readiness returned false")
        if app.state.memory_service is None:
            raise RuntimeError("control-plane Memory service is missing")
        if app.state.audit_log is None:
            raise RuntimeError("control-plane audit log is missing")
        print(
            "CONTROL_PLANE_BOOTSTRAP_OK startup=yes database=yes "
            "memory=yes audit=yes secrets=redacted"
        )


def main() -> int:
    try:
        asyncio.run(check_startup())
        print("CONTROL_PLANE_SHUTDOWN_OK resources=closed secrets=redacted")
        return 0
    except Exception as exc:
        print(
            "CONTROL_PLANE_BOOTSTRAP_FAILED "
            f"error_type={type(exc).__name__} reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
