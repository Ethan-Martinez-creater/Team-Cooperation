from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402

QUERIES = {
    "memory": "SELECT key_id, count(*) FROM memory_records GROUP BY key_id ORDER BY key_id",
    "checkpoint": "SELECT checkpoint_key_id, count(*) FROM agent_runs GROUP BY checkpoint_key_id ORDER BY checkpoint_key_id",
    "control": "SELECT content_key_id, count(*) FROM agent_run_commands GROUP BY content_key_id ORDER BY content_key_id",
    "tool_arguments": "SELECT arguments_key_id, count(*) FROM tool_jobs GROUP BY arguments_key_id ORDER BY arguments_key_id",
    "tool_results": "SELECT result_key_id, count(*) FROM tool_jobs WHERE result_key_id IS NOT NULL GROUP BY result_key_id ORDER BY result_key_id",
}


def main() -> int:
    settings = load_environment_settings(ROOT / ".env")
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT set_config('coifesp.tenant_id', 'team-a', true)"))
            values = []
            for domain, query in QUERIES.items():
                rows = connection.execute(text(query)).all()
                rendered = ",".join(f"{row[0]}:{row[1]}" for row in rows) or "none"
                values.append(f"{domain}={rendered}")
        print(
            "ENCRYPTION_KEY_ID_DIAGNOSTIC tenant=team-a "
            + " ".join(values)
            + " data=none secrets=none"
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
