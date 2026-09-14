"""Start the local control plane with the same shared-pool connector environment."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psutil
from start_local_workers import ROOT, RUNTIME, build_local_environment


def expected_control_plane_running(pid: object) -> bool:
    if not isinstance(pid, int) or not psutil.pid_exists(pid):
        return False
    try:
        command = " ".join(psutil.Process(pid).cmdline()).lower()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False
    return "uvicorn" in command and "coifesp_harness.control_plane:create_application" in command


def ready() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8000/health/ready", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main() -> int:
    environment = build_local_environment()
    readiness_timeout = float(
        environment.get("COIFESP_LOCAL_STARTUP_TIMEOUT_SECONDS", "180")
    )
    manifest_path = RUNTIME / "control-plane.json"
    if manifest_path.is_file():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if expected_control_plane_running(prior.get("pid")) and ready():
            print("LOCAL_CONTROL_PLANE_ALREADY_RUNNING")
            return 0
    stdout = (RUNTIME / "control-plane.stdout.log").open("ab")
    stderr = (RUNTIME / "control-plane.stderr.log").open("ab")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "uvicorn",
                "coifesp_harness.control_plane:create_application",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + readiness_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                print(f"LOCAL_CONTROL_PLANE_START_FAILED exit_code={process.returncode}")
                return 1
            if ready():
                manifest_path.write_text(
                    json.dumps({"pid": process.pid}, indent=2), encoding="utf-8"
                )
                print("LOCAL_CONTROL_PLANE_STARTED url=http://127.0.0.1:8000/app/")
                return 0
            time.sleep(0.5)
        process.terminate()
        print(
            "LOCAL_CONTROL_PLANE_START_FAILED "
            f"readiness_timeout={readiness_timeout:g}s"
        )
        return 1
    finally:
        stdout.close()
        stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
