from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.agent_runs.control_crypto import AgentControlKeyring  # noqa: E402
from coifesp_harness.agent_runs.crypto import AgentCheckpointKeyring  # noqa: E402
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.memory import MemoryKind, MemoryScope, TenantMemoryKeyring  # noqa: E402
from coifesp_harness.security import Classification, ResourceLabel  # noqa: E402
from coifesp_harness.tool_jobs.crypto import ToolJobKeyring  # noqa: E402


@dataclass(frozen=True, slots=True)
class MemoryRecordProbe:
    memory_id: str
    tenant_id: str
    scope: MemoryScope
    kind: MemoryKind
    label: ResourceLabel
    ciphertext: bytes
    nonce: bytes
    content_fingerprint: str
    key_id: str


def check(expected_active: str) -> None:
    settings = load_environment_settings(ROOT / ".env")
    memory = TenantMemoryKeyring.from_settings(settings)
    checkpoint = AgentCheckpointKeyring.from_settings(settings)
    control = AgentControlKeyring.from_settings(settings)
    tool = ToolJobKeyring.from_settings(settings)
    label = ResourceLabel(
        owner_tenant_id="team-a",
        classification=Classification.INTERNAL,
        compartments=frozenset({"project-x"}),
        resource_id="memory:key-rotation-probe",
    )
    encrypted_memory = memory.encrypt(
        memory_id="key-rotation-probe",
        tenant_id="team-a",
        scope=MemoryScope.TEAM_PROJECT,
        kind=MemoryKind.DECISION,
        label=label,
        plaintext="fixed non-sensitive memory key probe",
    )
    memory_record = MemoryRecordProbe(
        memory_id="key-rotation-probe",
        tenant_id="team-a",
        scope=MemoryScope.TEAM_PROJECT,
        kind=MemoryKind.DECISION,
        label=label,
        ciphertext=encrypted_memory.ciphertext,
        nonce=encrypted_memory.nonce,
        content_fingerprint=encrypted_memory.content_fingerprint,
        key_id=encrypted_memory.key_id,
    )
    encrypted_checkpoint = checkpoint.encrypt(
        tenant_id="team-a",
        run_id="key-rotation-run",
        version=1,
        checkpoint={"schema": "probe", "content": "fixed non-sensitive checkpoint"},
    )
    encrypted_control = control.encrypt(
        tenant_id="team-a",
        run_id="key-rotation-run",
        sequence=1,
        command_id="key-rotation-command",
        command_type="steer",
        content="fixed non-sensitive control probe",
    )
    encrypted_arguments = tool.encrypt(
        tenant_id="team-a",
        job_id="key-rotation-job",
        purpose="arguments",
        value={"value": "fixed non-sensitive argument"},
    )
    encrypted_result = tool.encrypt(
        tenant_id="team-a",
        job_id="key-rotation-job",
        purpose="result",
        value={"value": "fixed non-sensitive result"},
    )
    payloads = (
        encrypted_memory,
        encrypted_checkpoint,
        encrypted_control,
        encrypted_arguments,
        encrypted_result,
    )
    if any(payload.key_id != expected_active for payload in payloads):
        raise RuntimeError("one or more encryption domains used a non-active key")
    memory.decrypt(memory_record)
    checkpoint.decrypt(
        tenant_id="team-a",
        run_id="key-rotation-run",
        version=1,
        ciphertext=encrypted_checkpoint.ciphertext,
        nonce=encrypted_checkpoint.nonce,
        fingerprint=encrypted_checkpoint.fingerprint,
        key_id=encrypted_checkpoint.key_id,
    )
    control.decrypt(
        tenant_id="team-a",
        run_id="key-rotation-run",
        sequence=1,
        command_id="key-rotation-command",
        command_type="steer",
        ciphertext=encrypted_control.ciphertext,
        nonce=encrypted_control.nonce,
        fingerprint=encrypted_control.fingerprint,
        key_id=encrypted_control.key_id,
    )
    tool.decrypt(
        tenant_id="team-a",
        job_id="key-rotation-job",
        purpose="arguments",
        ciphertext=encrypted_arguments.ciphertext,
        nonce=encrypted_arguments.nonce,
        fingerprint=encrypted_arguments.fingerprint,
        key_id=encrypted_arguments.key_id,
    )
    tool.decrypt(
        tenant_id="team-a",
        job_id="key-rotation-job",
        purpose="result",
        ciphertext=encrypted_result.ciphertext,
        nonce=encrypted_result.nonce,
        fingerprint=encrypted_result.fingerprint,
        key_id=encrypted_result.key_id,
    )
    print(
        "MEMORY_ACTIVE_KEY_WRITES_OK "
        f"active={expected_active} memory=yes checkpoint=yes control=yes "
        "tool_arguments=yes tool_results=yes round_trip=yes database_writes=none "
        "plaintext=none ciphertext=none secrets=redacted"
    )


if __name__ == "__main__":
    try:
        check("memory-v2")
    except Exception as exc:
        print(
            f"MEMORY_ACTIVE_KEY_WRITES_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        raise SystemExit(1) from None
