from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.agent_runs import (  # noqa: E402
    AgentCheckpointKeyring,
    AgentCheckpointKeyRotationService,
)
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate one tenant checkpoint key batch.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--source-key-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--after-run-id")
    parser.add_argument("--limit", type=int, default=100)
    arguments = parser.parse_args()
    engine = None
    try:
        settings = load_environment_settings(ROOT / ".env")
        engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
        result = AgentCheckpointKeyRotationService(
            engine=engine,
            keyring=AgentCheckpointKeyring.from_settings(settings),
            audit=SQLAlchemyAuditLog(
                engine=engine, keyring=AuditSigningKeyring.from_settings(settings)
            ),
        ).rotate_batch(
            tenant_id=arguments.tenant_id,
            actor_id=arguments.actor_id,
            source_key_id=arguments.source_key_id,
            after_run_id=arguments.after_run_id,
            limit=arguments.limit,
        )
        print(
            "AGENT_CHECKPOINT_KEY_ROTATION_OK "
            f"tenant={result.tenant_id} source={result.source_key_id} "
            f"target={result.target_key_id} rotated={result.rotated} "
            f"complete={'yes' if result.complete else 'no'} "
            f"last_run={'set' if result.last_run_id else 'none'} secrets=redacted"
        )
        return 0
    except Exception as exc:
        print(
            f"AGENT_CHECKPOINT_KEY_ROTATION_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
