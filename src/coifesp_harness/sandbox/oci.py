from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Awaitable, Callable

from .models import SandboxErrorCode, SandboxRequest, SandboxResult, WorkspaceAccess

_RUNTIME = frozenset({"docker", "podman"})
_NAME = re.compile(r"[^a-z0-9_.-]+")
ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


class OCISandbox:
    """Fail-closed OCI executor; the CLI is invoked without a shell."""

    def __init__(
        self,
        *,
        runtime: str,
        workspace_root: Path,
        allowed_images: frozenset[str],
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        if runtime not in _RUNTIME:
            raise ValueError("sandbox runtime is unsupported")
        if not workspace_root.is_absolute():
            raise ValueError("sandbox workspace root must be absolute")
        if not allowed_images:
            raise ValueError("sandbox image allowlist cannot be empty")
        self.runtime = runtime
        self.workspace_root = workspace_root.resolve(strict=False)
        self.allowed_images = allowed_images
        self._process_factory = process_factory

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        try:
            workspace = request.workspace.resolve(strict=True)
            if not workspace.is_dir() or not workspace.is_relative_to(self.workspace_root):
                raise ValueError("sandbox workspace is outside its configured root")
            if request.image not in self.allowed_images:
                raise ValueError("sandbox image is not allowlisted")
        except (OSError, ValueError):
            return self._result(request, SandboxErrorCode.INVALID_REQUEST)

        name = self._container_name(request.execution_id)
        command = self._command(request, workspace, name)
        try:
            process = await self._process_factory(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=(
                    getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
        except (FileNotFoundError, OSError):
            return self._result(request, SandboxErrorCode.RUNTIME_UNAVAILABLE)

        stdout = bytearray()
        stderr = bytearray()
        exceeded = asyncio.Event()
        budget = {"remaining": request.limits.output_bytes}
        budget_lock = asyncio.Lock()
        readers = (
            asyncio.create_task(self._read(process.stdout, stdout, budget, budget_lock, exceeded)),
            asyncio.create_task(self._read(process.stderr, stderr, budget, budget_lock, exceeded)),
        )
        wait = asyncio.create_task(process.wait())
        limit = asyncio.create_task(exceeded.wait())
        timed_out = False
        output_truncated = False
        try:
            done, _ = await asyncio.wait(
                {wait, limit},
                timeout=request.limits.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait in done:
                limit.cancel()
            elif limit in done:
                output_truncated = True
                await self._terminate(name, process)
            else:
                timed_out = True
                await self._terminate(name, process)
            await wait
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(name, process))
            await asyncio.shield(wait)
            raise
        finally:
            limit.cancel()
            await asyncio.gather(*readers, limit, return_exceptions=True)

        error = None
        if timed_out:
            error = SandboxErrorCode.TIMED_OUT
        elif output_truncated:
            error = SandboxErrorCode.OUTPUT_LIMIT
        elif process.returncode != 0:
            error = SandboxErrorCode.EXECUTION_FAILED
        return SandboxResult(
            request.execution_id,
            process.returncode,
            bytes(stdout),
            bytes(stderr),
            error,
            timed_out,
            output_truncated,
        )

    def _command(self, request: SandboxRequest, workspace: Path, name: str) -> tuple[str, ...]:
        limits = request.limits
        mount = f"type=bind,src={workspace},dst=/workspace"
        if request.workspace_access is WorkspaceAccess.READ_ONLY:
            mount += ",readonly"
        return (
            self.runtime,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(limits.pids),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            f"{limits.cpu_count:g}",
            "--user",
            "65532:65532",
            "--workdir",
            "/workspace",
            "--mount",
            mount,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}",
            "--env",
            "HOME=/tmp",
            "--log-driver",
            "none",
            request.image,
            *request.argv,
        )

    async def _terminate(self, name: str, process: asyncio.subprocess.Process) -> None:
        # Stop the creator first. Otherwise a slow `docker run` may create the
        # container after an early name-based cleanup has already returned 404.
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                pass
        # Address the deterministic container name through the same runtime.
        # Retry force-removal to cover delayed daemon-side create visibility.
        actions = (("stop", "--time", "2", name),) + tuple(
            ("rm", "--force", name) for _ in range(3)
        )
        for action in actions:
            try:
                cleanup = await self._process_factory(
                    self.runtime,
                    *action,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(cleanup.wait(), timeout=5)
            except (OSError, TimeoutError):
                continue
            await asyncio.sleep(0.05)

    @staticmethod
    async def _read(
        stream,
        destination: bytearray,
        budget: dict[str, int],
        budget_lock: asyncio.Lock,
        exceeded: asyncio.Event,
    ):
        if stream is None:
            return
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return
            async with budget_lock:
                remaining = budget["remaining"]
                if remaining <= 0:
                    exceeded.set()
                    return
                accepted = min(len(chunk), remaining)
                destination.extend(chunk[:accepted])
                budget["remaining"] -= accepted
                if accepted < len(chunk):
                    exceeded.set()
                    return

    @staticmethod
    def _container_name(execution_id: str) -> str:
        digest = hashlib.sha256(execution_id.encode()).hexdigest()[:16]
        prefix = _NAME.sub("-", execution_id.lower()).strip("-._")[:32] or "job"
        return f"coifesp-{prefix}-{digest}"

    @staticmethod
    def _result(request: SandboxRequest, error: SandboxErrorCode) -> SandboxResult:
        return SandboxResult(request.execution_id, None, b"", b"", error)
