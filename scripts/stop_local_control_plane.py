"""Stop only the local control-plane process recorded by the local launcher."""

from __future__ import annotations

import json

import psutil
from start_local_control_plane import RUNTIME, expected_control_plane_running


def main() -> int:
    manifest_path = RUNTIME / "control-plane.json"
    if not manifest_path.is_file():
        print("LOCAL_CONTROL_PLANE_NOT_RUNNING")
        return 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pid = manifest.get("pid")
    if not expected_control_plane_running(pid):
        print("LOCAL_CONTROL_PLANE_NOT_RUNNING")
        return 0
    process = psutil.Process(pid)
    process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    print("LOCAL_CONTROL_PLANE_STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
