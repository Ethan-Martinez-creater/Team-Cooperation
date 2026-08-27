import asyncio
from pathlib import Path

import pytest

from coifesp_harness.sandbox import (
    CodeProfile,
    OCISandbox,
    SandboxErrorCode,
    SandboxLimits,
    SandboxRequest,
    SandboxResult,
    SandboxWorkspaceManager,
    SandboxedCodeTools,
    load_code_profiles,
    WorkspaceAccess,
)
from coifesp_harness.tool_jobs.worker import ToolExecutionContext, _CONTEXT

IMAGE = "registry.example/coifesp/python@sha256:" + "a" * 64


def request(workspace: Path, **overrides):
    values = {
        "execution_id": "exec-1",
        "image": IMAGE,
        "argv": ("python", "-c", "print('safe')"),
        "workspace": workspace,
    }
    values.update(overrides)
    return SandboxRequest(**values)


def test_request_requires_digest_argv_and_bounded_resources(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="digest"):
        request(tmp_path, image="python:latest")
    with pytest.raises(ValueError, match="argv"):
        request(tmp_path, argv=())
    with pytest.raises(ValueError, match="argument"):
        request(tmp_path, argv=("python", "bad\x00arg"))
    with pytest.raises(ValueError, match="memory"):
        SandboxLimits(memory_bytes=1024)


def test_oci_command_enforces_isolation_and_never_uses_shell(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workspace = root / "job"
    workspace.mkdir(parents=True)
    sandbox = OCISandbox(runtime="docker", workspace_root=root, allowed_images=frozenset({IMAGE}))
    value = request(workspace, workspace_access=WorkspaceAccess.READ_ONLY)
    command = sandbox._command(value, workspace.resolve(), "container")
    joined = " ".join(command)
    assert command[0:2] == ("docker", "run")
    assert "--network none" in joined
    assert "--read-only" in command
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges:true" in command
    assert "--user 65532:65532" in joined
    assert "readonly" in command[command.index("--mount") + 1]
    assert command[-3:] == ("python", "-c", "print('safe')")
    assert not any(item.lower() in {"cmd.exe", "powershell", "sh", "bash"} for item in command[:1])


@pytest.mark.asyncio
async def test_workspace_and_image_boundaries_fail_before_process_start(tmp_path: Path) -> None:
    root = tmp_path / "root"
    inside = root / "job"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    calls = 0

    async def forbidden(*_, **__):
        nonlocal calls
        calls += 1
        raise AssertionError("runtime must not start")

    sandbox = OCISandbox(
        runtime="docker",
        workspace_root=root,
        allowed_images=frozenset({IMAGE}),
        process_factory=forbidden,
    )
    result = await sandbox.execute(request(outside))
    assert result.error_code is SandboxErrorCode.INVALID_REQUEST
    other = request(inside, image="registry.example/other@sha256:" + "b" * 64)
    result = await sandbox.execute(other)
    assert result.error_code is SandboxErrorCode.INVALID_REQUEST
    assert calls == 0


@pytest.mark.asyncio
async def test_missing_runtime_is_reported_without_exception(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workspace = root / "job"
    workspace.mkdir(parents=True)

    async def missing(*_, **__):
        raise FileNotFoundError

    sandbox = OCISandbox(
        runtime="podman",
        workspace_root=root,
        allowed_images=frozenset({IMAGE}),
        process_factory=missing,
    )
    result = await sandbox.execute(request(workspace))
    assert result.error_code is SandboxErrorCode.RUNTIME_UNAVAILABLE
    assert not result.succeeded


def test_container_name_is_deterministic_bounded_and_safe() -> None:
    first = OCISandbox._container_name("EXEC/with unsafe spaces and $shell")
    second = OCISandbox._container_name("EXEC/with unsafe spaces and $shell")
    assert first == second
    assert len(first) <= 64
    assert first.startswith("coifesp-")
    assert all(
        character.islower() or character.isdigit() or character in "_.-" for character in first
    )


@pytest.mark.asyncio
async def test_code_tool_uses_admin_profile_and_job_scoped_workspace(tmp_path: Path) -> None:
    class Sandbox:
        def __init__(self):
            self.request = None

        async def execute(self, value):
            self.request = value
            return SandboxResult(value.execution_id, 0, b"ok", b"", None)

    sandbox = Sandbox()
    tools = SandboxedCodeTools(
        sandbox=sandbox,
        profiles=(
            CodeProfile(
                "pytest",
                IMAGE,
                "/usr/local/bin/python",
                ("-m", "pytest"),
                arguments_schema={
                    "type": "array",
                    "items": {"enum": ["-q", "--disable-warnings"]},
                    "maxItems": 2,
                    "uniqueItems": True,
                },
            ),
        ),
        workspace_root=tmp_path,
    )
    definition = tools.definition()
    assert definition.parameters_schema["properties"]["profile_id"]["enum"] == ["pytest"]
    token = _CONTEXT.set(ToolExecutionContext("team-a", "job-1", "run-1", "call-1", "idem-1"))
    try:
        (tmp_path / "team-a" / "job-1").mkdir(parents=True)
        result = await definition.handler({"profile_id": "pytest", "arguments": ["-q"]})
    finally:
        _CONTEXT.reset(token)
    assert result["stdout_utf8"] == "ok"
    assert sandbox.request.image == IMAGE
    assert sandbox.request.argv == (
        "/usr/local/bin/python",
        "-m",
        "pytest",
        "-q",
    )
    assert sandbox.request.workspace == tmp_path / "team-a" / "job-1"


def test_workspace_manager_binds_directory_to_tenant_and_job(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(root=(tmp_path / "sandbox").absolute())
    created = manager.prepare(tenant_id="team-a", job_id="job-1")
    duplicate = manager.prepare(tenant_id="team-a", job_id="job-1")
    assert duplicate == created
    marker = created.path / ".coifesp-workspace.json"
    assert '"tenant_id":"team-a"' in marker.read_text(encoding="utf-8")
    marker.write_text('{"tenant_id":"team-b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="ownership"):
        manager.prepare(tenant_id="team-a", job_id="job-1")


def test_profile_requires_an_array_argument_schema() -> None:
    profile = CodeProfile(
        "lint",
        IMAGE,
        "/usr/local/bin/ruff",
        arguments_schema={
            "type": "array",
            "prefixItems": [{"const": "check"}, {"const": "."}],
            "minItems": 2,
            "maxItems": 2,
        },
    )
    assert profile.profile_id == "lint"
    with pytest.raises(ValueError, match="array"):
        CodeProfile(
            "bad",
            IMAGE,
            "/bin/tool",
            arguments_schema={"type": "object"},
        )


def test_profile_registry_is_strict_and_digest_pinned() -> None:
    import json

    value = {
        "profile_id": "lint",
        "image": IMAGE,
        "executable": "/usr/local/bin/ruff",
        "fixed_arguments": [],
        "arguments_schema": {"type": "array", "maxItems": 0},
        "timeout_seconds": 30,
        "memory_bytes": 536870912,
        "cpu_count": 1,
        "pids": 64,
        "output_bytes": 1048576,
        "tmpfs_bytes": 67108864,
        "workspace_access": "read_write",
    }
    assert load_code_profiles(json.dumps([value]))[0].image == IMAGE
    with pytest.raises(ValueError):
        load_code_profiles(json.dumps([{**value, "unknown": True}]))
    with pytest.raises(ValueError):
        load_code_profiles(json.dumps([{**value, "image": "ruff:latest"}]))
