from __future__ import annotations

import os
import sys
import time

import psutil

WORKER_MARKERS = {
    "agent-worker": (
        "coifesp-agent-worker",
        "coifesp_harness.worker_main",
        "coifesp_harness\\worker_main.py",
        "coifesp_harness/worker_main.py",
    ),
    "tool-worker": (
        "coifesp-tool-worker",
        "coifesp_harness.tool_worker_main",
        "coifesp_harness\\tool_worker_main.py",
        "coifesp_harness/tool_worker_main.py",
    ),
}


def protected_pids() -> set[int]:
    result = {os.getpid()}
    process = psutil.Process(os.getpid())
    for parent in process.parents():
        result.add(parent.pid)
    return result


def discover() -> list[tuple[str, psutil.Process]]:
    protected = protected_pids()
    found: list[tuple[str, psutil.Process]] = []
    for process in psutil.process_iter(("pid", "cmdline")):
        if process.pid in protected:
            continue
        try:
            command = " ".join(process.info.get("cmdline") or ()).lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        for worker_type, markers in WORKER_MARKERS.items():
            if any(marker in command for marker in markers):
                found.append((worker_type, process))
                break
    return found


def main() -> int:
    found = discover()
    if not found:
        print("COIFESP_WORKERS_STOPPED agent_worker=absent tool_worker=absent")
        return 0

    for _, process in found:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs([process for _, process in found], timeout=10)
    forced: set[int] = set()
    for process in alive:
        try:
            forced.add(process.pid)
            process.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)

    remaining = discover()
    if remaining:
        print(
            "COIFESP_WORKERS_STOP_FAILED "
            f"remaining={len(remaining)} pids={','.join(str(item.pid) for _, item in remaining)}"
        )
        return 1
    counts = {
        worker_type: sum(1 for value, _ in found if value == worker_type)
        for worker_type in WORKER_MARKERS
    }
    print(
        "COIFESP_WORKERS_STOPPED "
        f"agent_worker={counts['agent-worker']} tool_worker={counts['tool-worker']} "
        f"forced={len(forced)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
