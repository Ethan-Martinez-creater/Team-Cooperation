"""Report whether durable legacy Governance data is ready for retirement."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.collaboration.legacy_inventory import (  # noqa: E402
    collect_legacy_governance_inventory,
)
from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-no-active",
        action="store_true",
        help="fail when active legacy plans, assignments, or executions remain",
    )
    parser.add_argument(
        "--require-retired",
        action="store_true",
        help="fail until every legacy row has been migrated or archived elsewhere",
    )
    return parser.parse_args()


def load_database_url() -> str:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = Settings.from_environment()
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required")
    return settings.database_url


def main() -> int:
    args = parse_args()
    engine = create_engine(load_database_url(), pool_pre_ping=True, hide_parameters=True)
    try:
        inventory = collect_legacy_governance_inventory(engine)
    finally:
        engine.dispose()
    print(json.dumps(inventory.as_dict(), sort_keys=True))
    if args.require_retired and not inventory.is_retired:
        return 2
    if args.require_no_active and inventory.has_active_work:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
