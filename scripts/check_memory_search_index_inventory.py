from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from coifesp_harness.config import Settings


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    settings = Settings.from_environment()
    engine = create_engine(settings.database_url, hide_parameters=True)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT tenant_id, count(*) AS missing
            FROM memory_records r
            WHERE NOT EXISTS (
                SELECT 1 FROM memory_search_terms s
                WHERE s.tenant_id=r.tenant_id AND s.memory_id=r.memory_id
            )
            GROUP BY tenant_id ORDER BY tenant_id
        """)).mappings().all()
    engine.dispose()
    print("MEMORY_INDEX_INVENTORY " + ("complete=yes" if not rows else
        "complete=no tenants=" + ",".join(f"{row['tenant_id']}:{row['missing']}" for row in rows)))


if __name__ == "__main__":
    main()
