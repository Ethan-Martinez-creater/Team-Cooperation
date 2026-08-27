from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

KEYCLOAK_HOME = Path(r"E:\keyclock\keycloak-26.7.0")
KEYCLOAK_ENV = Path(r"E:\keyclock\runtime\keycloak.env")
PID_FILE = Path(r"E:\keyclock\runtime\keycloak.pid")
STDOUT_LOG = Path(r"E:\keyclock\runtime\keycloak.stdout.log")
STDERR_LOG = Path(r"E:\keyclock\runtime\keycloak.stderr.log")
DISCOVERY_URL = "http://127.0.0.1:8080/realms/coifesp/.well-known/openid-configuration"


def find_java_home() -> Path:
    root = Path(r"E:\openjdk")
    candidates = [root, *sorted((path for path in root.iterdir() if path.is_dir()), reverse=True)]
    for candidate in candidates:
        if (candidate / "bin" / "java.exe").is_file():
            return candidate
    raise RuntimeError("OpenJDK java.exe was not found under E:\\openjdk")


def ready() -> bool:
    try:
        response = httpx.get(DISCOVERY_URL, timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def main() -> int:
    if ready():
        print("KEYCLOAK_LOCAL_READY already_running=yes")
        return 0
    if not (KEYCLOAK_HOME / "bin" / "kc.bat").is_file() or not KEYCLOAK_ENV.is_file():
        raise RuntimeError("local Keycloak installation or environment file is absent")
    configured = {
        key: value for key, value in dotenv_values(KEYCLOAK_ENV).items() if isinstance(value, str)
    }
    environment = os.environ.copy()
    environment.update(configured)
    java_home = find_java_home()
    environment["JAVA_HOME"] = str(java_home)
    environment["PATH"] = f"{java_home / 'bin'}{os.pathsep}{environment.get('PATH', '')}"
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with STDOUT_LOG.open("ab") as stdout, STDERR_LOG.open("ab") as stderr:
        process = subprocess.Popen(
            [str(KEYCLOAK_HOME / "bin" / "kc.bat"), "start-dev"],
            cwd=KEYCLOAK_HOME,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
        )
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Keycloak exited before readiness with code {process.returncode}")
        if ready():
            print(f"KEYCLOAK_LOCAL_READY already_running=no pid={process.pid}")
            return 0
        time.sleep(1)
    raise RuntimeError("Keycloak did not become ready within 90 seconds")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"KEYCLOAK_LOCAL_START_FAILED reason={exc}")
        raise SystemExit(1) from None
