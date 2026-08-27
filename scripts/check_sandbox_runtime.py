import asyncio
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.sandbox import load_code_profiles  # noqa: E402


async def check_runtime(runtime: str) -> None:
    executable = shutil.which(runtime)
    if executable is None:
        raise RuntimeError(f"{runtime} executable is unavailable")
    process = await asyncio.create_subprocess_exec(
        executable,
        "version",
        "--format",
        "{{.Server.Version}}" if runtime == "docker" else "{{.Version}}",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    if process.returncode != 0 or not stdout.strip() or len(stdout) > 1024:
        raise RuntimeError(f"{runtime} daemon is unavailable")


def main() -> int:
    try:
        settings = load_environment_settings(PROJECT_ROOT / ".env")
        settings.validate(require_sandbox=True)
        assert settings.sandbox_runtime is not None
        assert settings.sandbox_workspace_root is not None
        assert settings.sandbox_profiles_json is not None
        profiles = load_code_profiles(settings.sandbox_profiles_json)
        asyncio.run(check_runtime(settings.sandbox_runtime))
        root = Path(settings.sandbox_workspace_root)
        print(
            "SANDBOX_RUNTIME_OK "
            f"runtime={settings.sandbox_runtime} profiles={len(profiles)} "
            f"workspace_absolute={'yes' if root.is_absolute() else 'no'} "
            "images=digest_pinned network_default=none secrets=redacted"
        )
        return 0
    except Exception as exc:
        print(
            f"SANDBOX_RUNTIME_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
