"""Report bounded Agent Run status counts without exposing checkpoint contents."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import Settings  # noqa: E402


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = Settings.from_environment()
    if not settings.database_url:
        raise RuntimeError("COIFESP_DATABASE_URL is absent")
    engine = create_engine(settings.database_url, hide_parameters=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                {"tenant_id": settings.worker_tenant_id or "team-a"},
            )
            rows = connection.execute(
                text("SELECT status, count(*) FROM agent_runs GROUP BY status ORDER BY status")
            ).all()
    finally:
        engine.dispose()
    summary = " ".join(f"{status}={count}" for status, count in rows) or "empty=yes"
    print(f"AGENT_RUN_QUEUE_STATUS {summary} secrets=redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
