from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..tool_jobs import PermanentToolError, RetryableToolError, current_tool_execution_context
from ..tools import ToolDefinition
from ..security import RiskLevel
from .models import SandboxErrorCode, SandboxLimits, SandboxRequest, WorkspaceAccess
from .oci import OCISandbox

_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}$")
_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "image",
        "executable",
        "fixed_arguments",
        "arguments_schema",
        "timeout_seconds",
        "memory_bytes",
        "cpu_count",
        "pids",
        "output_bytes",
        "tmpfs_bytes",
        "workspace_access",
    }
)


@dataclass(frozen=True, slots=True)
class CodeProfile:
    profile_id: str
    image: str
    executable: str
    fixed_arguments: tuple[str, ...] = ()
    arguments_schema: dict[str, Any] | None = None
    limits: SandboxLimits = SandboxLimits()
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_WRITE

    def __post_init__(self) -> None:
        if not _PROFILE.fullmatch(self.profile_id):
            raise ValueError("sandbox profile ID is invalid")
        if not _IMAGE.fullmatch(self.image):
            raise ValueError("sandbox profile image must be pinned by sha256 digest")
        if not self.executable.startswith("/") or "\x00" in self.executable:
            raise ValueError("sandbox executable must be an absolute container path")
        if len(self.fixed_arguments) > 128 or any(
            not isinstance(item, str) or "\x00" in item or len(item.encode("utf-8")) > 32_768
            for item in self.fixed_arguments
        ):
            raise ValueError("sandbox fixed arguments are invalid")
        schema = self.arguments_schema or {
            "type": "array",
            "maxItems": 0,
        }
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "array":
            raise ValueError("sandbox profile arguments schema must describe an array")


class SandboxedCodeTools:
    """Maps model-visible profiles to administrator-owned OCI policies."""

    def __init__(
        self,
        *,
        sandbox: OCISandbox,
        profiles: tuple[CodeProfile, ...],
        workspace_root: Path,
    ) -> None:
        if not profiles or len({item.profile_id for item in profiles}) != len(profiles):
            raise ValueError("sandbox profiles are absent or duplicated")
        self.sandbox = sandbox
        self.profiles = {item.profile_id: item for item in profiles}
        self.workspace_root = workspace_root.resolve(strict=False)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="code.run_profile",
            description=(
                "Run an administrator-approved program profile in an isolated, "
                "network-disabled workspace."
            ),
            handler=self.run_profile,
            parameters_schema={
                "type": "object",
                "properties": {
                    "profile_id": {"type": "string", "enum": sorted(self.profiles)},
                    "arguments": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 32768},
                        "maxItems": 128,
                    },
                },
                "required": ["profile_id", "arguments"],
                "additionalProperties": False,
            },
            required_roles=frozenset({"contributor"}),
            risk=RiskLevel.MEDIUM,
            timeout_seconds=max(item.limits.timeout_seconds for item in self.profiles.values())
            + 10,
            max_output_chars=1_000_000,
        )

    async def run_profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile = self.profiles.get(arguments["profile_id"])
        if profile is None:
            raise PermanentToolError("sandbox_profile_denied")
        try:
            Draft202012Validator(
                profile.arguments_schema or {"type": "array", "maxItems": 0}
            ).validate(arguments["arguments"])
        except ValidationError as exc:
            raise PermanentToolError("sandbox_arguments_denied") from exc
        context = current_tool_execution_context()
        workspace = self.workspace_root / context.tenant_id / context.job_id
        request = SandboxRequest(
            execution_id=context.job_id,
            image=profile.image,
            argv=(profile.executable, *profile.fixed_arguments, *arguments["arguments"]),
            workspace=workspace,
            workspace_access=profile.workspace_access,
            limits=profile.limits,
        )
        result = await self.sandbox.execute(request)
        if result.error_code is SandboxErrorCode.RUNTIME_UNAVAILABLE:
            raise RetryableToolError("sandbox_runtime_unavailable")
        if result.error_code is not None:
            raise PermanentToolError(f"sandbox_{result.error_code.value}")
        return {
            "exit_code": result.exit_code,
            "stdout_utf8": result.stdout.decode("utf-8", errors="replace"),
            "stderr_utf8": result.stderr.decode("utf-8", errors="replace"),
        }


def load_code_profiles(raw: str) -> tuple[CodeProfile, ...]:
    if len(raw.encode("utf-8")) > 262_144:
        raise ValueError("sandbox profile registry is too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("sandbox profile registry is invalid JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("sandbox profile registry must contain 1 to 32 profiles")
    profiles: list[CodeProfile] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != _PROFILE_FIELDS:
            raise ValueError("sandbox profile fields are invalid")
        fixed = value["fixed_arguments"]
        if not isinstance(fixed, list) or any(not isinstance(item, str) for item in fixed):
            raise ValueError("sandbox fixed arguments are invalid")
        try:
            profiles.append(
                CodeProfile(
                    profile_id=value["profile_id"],
                    image=value["image"],
                    executable=value["executable"],
                    fixed_arguments=tuple(fixed),
                    arguments_schema=value["arguments_schema"],
                    limits=SandboxLimits(
                        timeout_seconds=value["timeout_seconds"],
                        memory_bytes=value["memory_bytes"],
                        cpu_count=value["cpu_count"],
                        pids=value["pids"],
                        output_bytes=value["output_bytes"],
                        tmpfs_bytes=value["tmpfs_bytes"],
                    ),
                    workspace_access=WorkspaceAccess(value["workspace_access"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("sandbox profile value is invalid") from exc
    if len({item.profile_id for item in profiles}) != len(profiles):
        raise ValueError("sandbox profile IDs are duplicated")
    return tuple(profiles)
