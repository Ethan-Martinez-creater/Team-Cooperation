from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}$")
_ARG_MAX = 32_768


class SandboxErrorCode(str, Enum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INVALID_REQUEST = "invalid_request"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"
    EXECUTION_FAILED = "execution_failed"
    RUNTIME_FAILED = "runtime_failed"


class WorkspaceAccess(str, Enum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    timeout_seconds: float = 30
    memory_bytes: int = 512 * 1024 * 1024
    cpu_count: float = 1.0
    pids: int = 128
    output_bytes: int = 1_048_576
    tmpfs_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if not 0.1 <= self.timeout_seconds <= 3600:
            raise ValueError("sandbox timeout is invalid")
        if not 16 * 1024 * 1024 <= self.memory_bytes <= 64 * 1024**3:
            raise ValueError("sandbox memory limit is invalid")
        if not 0.1 <= self.cpu_count <= 64:
            raise ValueError("sandbox CPU limit is invalid")
        if not 16 <= self.pids <= 4096:
            raise ValueError("sandbox PID limit is invalid")
        if not 1024 <= self.output_bytes <= 16 * 1024 * 1024:
            raise ValueError("sandbox output limit is invalid")
        if not 1024 * 1024 <= self.tmpfs_bytes <= 4 * 1024**3:
            raise ValueError("sandbox tmpfs limit is invalid")


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    execution_id: str
    image: str
    argv: tuple[str, ...]
    workspace: Path
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_WRITE
    limits: SandboxLimits = SandboxLimits()

    def __post_init__(self) -> None:
        if not self.execution_id or len(self.execution_id) > 128:
            raise ValueError("sandbox execution ID is invalid")
        if not _IMAGE.fullmatch(self.image):
            raise ValueError("sandbox image must be pinned by sha256 digest")
        if not 1 <= len(self.argv) <= 256:
            raise ValueError("sandbox argv is invalid")
        if any(
            not isinstance(arg, str) or "\x00" in arg or len(arg.encode("utf-8")) > _ARG_MAX
            for arg in self.argv
        ):
            raise ValueError("sandbox argv contains an invalid argument")
        if not self.workspace.is_absolute():
            raise ValueError("sandbox workspace must be absolute")


@dataclass(frozen=True, slots=True)
class SandboxResult:
    execution_id: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    error_code: SandboxErrorCode | None
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.error_code is None
