"""Exercise real Docker isolation and cleanup invariants without secrets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.config import Settings  # noqa: E402
from coifesp_harness.sandbox import (  # noqa: E402
    OCISandbox,
    SandboxErrorCode,
    SandboxLimits,
    SandboxRequest,
    SandboxWorkspaceManager,
    load_code_profiles,
)


async def docker_json(*args: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    if process.returncode != 0:
        raise RuntimeError(f"docker inspection failed: {stderr.decode(errors='replace')[:256]}")
    return json.loads(stdout)


def container_name(execution_id: str) -> str:
    digest = hashlib.sha256(execution_id.encode()).hexdigest()[:16]
    return f"coifesp-{execution_id}-{digest}"


async def require_absent(name: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "container",
        "inspect",
        name,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(process.wait(), timeout=10)
    if process.returncode == 0:
        raise RuntimeError(f"sandbox container was not cleaned: {name}")


async def main_async() -> None:
    load_dotenv(ROOT / ".env", override=True)
    settings = Settings.from_environment()
    settings.validate(require_sandbox=True)
    assert settings.sandbox_runtime == "docker"
    assert settings.sandbox_workspace_root is not None
    assert settings.sandbox_profiles_json is not None
    profile = load_code_profiles(settings.sandbox_profiles_json)[0]
    root = Path(settings.sandbox_workspace_root)
    manager = SandboxWorkspaceManager(root=root)
    workspace = manager.prepare(
        tenant_id="sandbox-acceptance", job_id="isolation"
    ).path
    sandbox = OCISandbox(
        runtime="docker",
        workspace_root=root,
        allowed_images=frozenset({profile.image}),
    )
    limits = SandboxLimits(
        timeout_seconds=30,
        memory_bytes=128 * 1024 * 1024,
        cpu_count=0.5,
        pids=32,
        output_bytes=64 * 1024,
        tmpfs_bytes=8 * 1024 * 1024,
    )
    probe = """
import os, pathlib, socket, time
root_readonly = False
try:
    pathlib.Path('/coifesp-root-write').write_text('denied')
except OSError:
    root_readonly = True
network_denied = False
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(('1.1.1.1', 53))
except OSError:
    network_denied = True
finally:
    s.close()
pathlib.Path('/workspace/result.txt').write_text('workspace-ok')
status = pathlib.Path('/proc/self/status').read_text()
no_new_privs = next(x.split()[1] for x in status.splitlines() if x.startswith('NoNewPrivs:'))
cap_eff = next(x.split()[1] for x in status.splitlines() if x.startswith('CapEff:'))
print(f'uid={os.getuid()} gid={os.getgid()} root_readonly={root_readonly} network_denied={network_denied} no_new_privs={no_new_privs} cap_eff={cap_eff}', flush=True)
time.sleep(3)
""".strip()
    execution_id = "isolation"
    task = asyncio.create_task(
        sandbox.execute(
            SandboxRequest(
                execution_id=execution_id,
                image=profile.image,
                argv=("/usr/local/bin/python", "-I", "-B", "-c", probe),
                workspace=workspace,
                limits=limits,
            )
        )
    )
    name = container_name(execution_id)
    inspected = None
    for _ in range(30):
        await asyncio.sleep(0.1)
        try:
            inspected = await docker_json("container", "inspect", name)
            break
        except RuntimeError:
            continue
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise RuntimeError("running sandbox container could not be inspected")
    value = inspected[0]
    host = value["HostConfig"]
    config = value["Config"]
    security = set(host.get("SecurityOpt") or [])
    checks = {
        "network_none": host.get("NetworkMode") == "none",
        "root_readonly": host.get("ReadonlyRootfs") is True,
        "caps_dropped": "ALL" in set(host.get("CapDrop") or []),
        "no_new_privileges": any("no-new-privileges" in item for item in security),
        "non_root": config.get("User") == "65532:65532",
        "pids": host.get("PidsLimit") == 32,
        "memory": host.get("Memory") == 128 * 1024 * 1024,
        "swap": host.get("MemorySwap") == 128 * 1024 * 1024,
        "cpu": host.get("NanoCpus") == 500_000_000,
        "image_digest": config.get("Image") == profile.image,
        "logging_disabled": host.get("LogConfig", {}).get("Type") == "none",
    }
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"Docker HostConfig isolation checks failed: {failed}")
    result = await task
    output = result.stdout.decode("utf-8", errors="replace")
    expected = (
        "uid=65532 gid=65532 root_readonly=True network_denied=True "
        "no_new_privs=1 cap_eff=0000000000000000"
    )
    if not result.succeeded or expected not in output:
        safe_stderr = result.stderr.decode("utf-8", errors="replace")[:512]
        raise RuntimeError(
            "in-container isolation assertions failed: "
            f"exit={result.exit_code} error={result.error_code} "
            f"stdout={output[:512]!r} stderr={safe_stderr!r}"
        )
    if (workspace / "result.txt").read_text(encoding="utf-8") != "workspace-ok":
        raise RuntimeError("job workspace was not writable")
    await require_absent(name)

    timeout_id = "timeout"
    timed = await sandbox.execute(
        SandboxRequest(
            execution_id=timeout_id,
            image=profile.image,
            argv=("/usr/local/bin/python", "-I", "-B", "-c", "import time; time.sleep(5)"),
            workspace=workspace,
            limits=SandboxLimits(
                timeout_seconds=0.5,
                memory_bytes=64 * 1024 * 1024,
                cpu_count=0.5,
                pids=32,
                output_bytes=4096,
                tmpfs_bytes=4 * 1024 * 1024,
            ),
        )
    )
    if timed.error_code is not SandboxErrorCode.TIMED_OUT or not timed.timed_out:
        raise RuntimeError("sandbox timeout did not fail closed")
    await require_absent(container_name(timeout_id))

    output_id = "output-limit"
    limited = await sandbox.execute(
        SandboxRequest(
            execution_id=output_id,
            image=profile.image,
            argv=("/usr/local/bin/python", "-I", "-B", "-c", "print('x' * 100000)"),
            workspace=workspace,
            limits=SandboxLimits(
                timeout_seconds=5,
                memory_bytes=64 * 1024 * 1024,
                cpu_count=0.5,
                pids=32,
                output_bytes=1024,
                tmpfs_bytes=4 * 1024 * 1024,
            ),
        )
    )
    if limited.error_code is not SandboxErrorCode.OUTPUT_LIMIT or not limited.output_truncated:
        raise RuntimeError("sandbox output budget did not fail closed")
    if len(limited.stdout) + len(limited.stderr) != 1024:
        raise RuntimeError("sandbox output exceeded its combined budget")
    await require_absent(container_name(output_id))
    print(
        "SANDBOX_ISOLATION_OK non_root=yes read_only_root=yes network=none "
        "caps=none no_new_privileges=yes memory_cpu_pids=bounded "
        "digest_pinned=yes workspace_scoped=yes timeout_cleanup=yes "
        "output_cleanup=yes secrets=none"
    )


def main() -> int:
    try:
        asyncio.run(main_async())
        return 0
    except Exception as exc:
        print(
            f"SANDBOX_ISOLATION_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=none"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
