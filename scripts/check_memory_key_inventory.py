from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

from sqlalchemy import create_engine, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.agent_runs.control_crypto import AgentControlKeyring  # noqa: E402
from coifesp_harness.agent_runs.crypto import AgentCheckpointKeyring  # noqa: E402
from coifesp_harness.agent_runs.repository import (  # noqa: E402
    AGENT_RUN_COMMANDS,
    AGENT_RUNS,
)
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.memory import TenantMemoryKeyring  # noqa: E402
from coifesp_harness.memory.repository import (  # noqa: E402
    MEMORY_RECORDS,
    SQLAlchemyMemoryRepository,
)
from coifesp_harness.tool_jobs.crypto import ToolJobKeyring  # noqa: E402
from coifesp_harness.tool_jobs.repository import TOOL_JOBS  # noqa: E402


def rendered(counter: collections.Counter[str]) -> str:
    return ",".join(f"{key}:{counter[key]}" for key in sorted(counter)) or "none"


def check(tenant_id: str, expected_active: str) -> None:
    settings = load_environment_settings(ROOT / ".env")
    memory = TenantMemoryKeyring.from_settings(settings)
    checkpoints = AgentCheckpointKeyring.from_settings(settings)
    controls = AgentControlKeyring.from_settings(settings)
    tools = ToolJobKeyring.from_settings(settings)
    if any(ring.key_id != expected_active for ring in (memory, checkpoints, controls, tools)):
        raise RuntimeError("configured active Memory key does not match the expected stage")
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    counts: dict[str, collections.Counter[str]] = {
        "memory": collections.Counter(),
        "checkpoint": collections.Counter(),
        "control": collections.Counter(),
        "tool_arguments": collections.Counter(),
        "tool_results": collections.Counter(),
    }
    decrypted = collections.Counter()
    try:
        with engine.connect() as connection:
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            for row in connection.execute(select(MEMORY_RECORDS)).mappings():
                record = SQLAlchemyMemoryRepository._row_to_record(row)
                counts["memory"][record.key_id] += 1
                memory.decrypt(record)
                decrypted["memory"] += 1
            for row in connection.execute(select(AGENT_RUNS)).mappings():
                counts["checkpoint"][row["checkpoint_key_id"]] += 1
                checkpoints.decrypt(
                    tenant_id=row["tenant_id"],
                    run_id=row["run_id"],
                    version=int(row["version"]),
                    ciphertext=bytes(row["checkpoint_ciphertext"]),
                    nonce=bytes(row["checkpoint_nonce"]),
                    fingerprint=row["checkpoint_fingerprint"],
                    key_id=row["checkpoint_key_id"],
                )
                decrypted["checkpoint"] += 1
            for row in connection.execute(select(AGENT_RUN_COMMANDS)).mappings():
                counts["control"][row["content_key_id"]] += 1
                controls.decrypt(
                    tenant_id=row["tenant_id"],
                    run_id=row["run_id"],
                    sequence=int(row["sequence"]),
                    command_id=row["command_id"],
                    command_type=row["command_type"],
                    ciphertext=bytes(row["content_ciphertext"]),
                    nonce=bytes(row["content_nonce"]),
                    fingerprint=row["content_fingerprint"],
                    key_id=row["content_key_id"],
                )
                decrypted["control"] += 1
            for row in connection.execute(select(TOOL_JOBS)).mappings():
                counts["tool_arguments"][row["arguments_key_id"]] += 1
                tools.decrypt(
                    tenant_id=row["tenant_id"],
                    job_id=row["job_id"],
                    purpose="arguments",
                    ciphertext=bytes(row["arguments_ciphertext"]),
                    nonce=bytes(row["arguments_nonce"]),
                    fingerprint=row["arguments_fingerprint"],
                    key_id=row["arguments_key_id"],
                )
                decrypted["tool_arguments"] += 1
                if row["result_key_id"] is not None:
                    counts["tool_results"][row["result_key_id"]] += 1
                    tools.decrypt(
                        tenant_id=row["tenant_id"],
                        job_id=row["job_id"],
                        purpose="result",
                        ciphertext=bytes(row["result_ciphertext"]),
                        nonce=bytes(row["result_nonce"]),
                        fingerprint=row["result_fingerprint"],
                        key_id=row["result_key_id"],
                    )
                    decrypted["tool_results"] += 1
    finally:
        engine.dispose()
    print(
        "MEMORY_KEY_INVENTORY_OK "
        f"tenant={tenant_id} active={expected_active} "
        + " ".join(
            f"{domain}={rendered(counts[domain])} decrypted={decrypted[domain]}"
            for domain in counts
        )
        + " plaintext=none ciphertext=none secrets=redacted"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory encrypted records by key ID.")
    parser.add_argument("--tenant-id", default="team-a")
    parser.add_argument("--expected-active", required=True)
    arguments = parser.parse_args()
    try:
        check(arguments.tenant_id, arguments.expected_active)
        return 0
    except Exception as exc:
        print(
            f"MEMORY_KEY_INVENTORY_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
