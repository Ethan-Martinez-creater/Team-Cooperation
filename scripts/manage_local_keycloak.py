import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

KEYCLOAK_HOME = Path(r"E:\keyclock\keycloak-26.7.0")
RUNTIME = Path(r"E:\keyclock\runtime")
ENV_FILE = RUNTIME / "keycloak.env"
PID_FILE = RUNTIME / "keycloak.pid"
STDOUT_LOG = RUNTIME / "keycloak.stdout.log"
STDERR_LOG = RUNTIME / "keycloak.stderr.log"

REQUIRED = {
    "JAVA_HOME",
    "KC_DB",
    "KC_DB_URL",
    "KC_DB_USERNAME",
    "KC_DB_PASSWORD",
    "KC_BOOTSTRAP_ADMIN_USERNAME",
    "KC_BOOTSTRAP_ADMIN_PASSWORD",
    "KC_HTTP_ENABLED",
    "KC_HTTP_HOST",
    "KC_HTTP_PORT",
    "KC_HOSTNAME",
    "KC_HOSTNAME_STRICT",
    "KC_HEALTH_ENABLED",
    "KC_METRICS_ENABLED",
    "KC_CACHE",
    "KC_LOG_LEVEL",
}
SECRET_KEYS = {"KC_DB_PASSWORD", "KC_BOOTSTRAP_ADMIN_PASSWORD"}


def load_environment() -> dict[str, str]:
    if not ENV_FILE.is_file():
        raise RuntimeError("E:\\keyclock\\runtime\\keycloak.env is missing")
    raw = dotenv_values(ENV_FILE)
    values = {key: value for key, value in raw.items() if value is not None}
    missing = sorted(key for key in REQUIRED if not values.get(key, "").strip())
    unknown = sorted(set(values) - REQUIRED)
    if missing:
        raise RuntimeError("keycloak.env has missing values: " + ", ".join(missing))
    if unknown:
        raise RuntimeError("keycloak.env contains unsupported keys: " + ", ".join(unknown))
    for key in SECRET_KEYS:
        if len(values[key]) < 32:
            raise RuntimeError(f"{key} must contain at least 32 characters")
    if values["KC_DB_PASSWORD"] == values["KC_BOOTSTRAP_ADMIN_PASSWORD"]:
        raise RuntimeError("Keycloak database and administrator passwords must differ")
    if values["KC_DB"] != "postgres":
        raise RuntimeError("local Keycloak must use PostgreSQL")
    if values["KC_DB_USERNAME"] != "keycloak_runtime":
        raise RuntimeError("unexpected Keycloak database username")
    if values["KC_HTTP_HOST"] != "127.0.0.1":
        raise RuntimeError("local HTTP listener must bind only to 127.0.0.1")
    java = Path(values["JAVA_HOME"]) / "bin" / "java.exe"
    if not java.is_file():
        raise RuntimeError("configured JAVA_HOME does not contain bin\\java.exe")
    environment = os.environ.copy()
    environment.update(values)
    environment["PATH"] = str(java.parent) + os.pathsep + environment.get("PATH", "")
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    return environment


def parse_jdbc_url(value: str) -> tuple[str, int, str]:
    prefix = "jdbc:postgresql://"
    if not value.startswith(prefix):
        raise RuntimeError("KC_DB_URL must be a PostgreSQL JDBC URL")
    remainder = value[len(prefix) :]
    authority, separator, database = remainder.partition("/")
    host, colon, raw_port = authority.partition(":")
    if not separator or host not in {"127.0.0.1", "localhost"} or not database:
        raise RuntimeError("KC_DB_URL must target a named local database")
    port = int(raw_port) if colon else 5432
    if database != "keycloak" or not 1 <= port <= 65535:
        raise RuntimeError("KC_DB_URL must target the keycloak database")
    return host, port, database


def preflight() -> None:
    environment = load_environment()
    host, port, database = parse_jdbc_url(environment["KC_DB_URL"])
    url = URL.create(
        "postgresql+psycopg",
        username=environment["KC_DB_USERNAME"],
        password=environment["KC_DB_PASSWORD"],
        host=host,
        port=port,
        database=database,
    )
    engine = create_engine(url, hide_parameters=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT current_user, current_database(),
                           r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                           r.rolreplication, r.rolbypassrls
                    FROM pg_catalog.pg_roles AS r
                    WHERE r.rolname = current_user
                    """
                )
            ).one()
    finally:
        engine.dispose()
    if row[0] != "keycloak_runtime" or row[1] != "keycloak" or any(row[2:]):
        raise RuntimeError("Keycloak database identity or least-privilege attributes are unsafe")
    print(
        "KEYCLOAK_PREFLIGHT_OK database=keycloak user=keycloak_runtime "
        "superuser=no createdb=no createrole=no replication=no bypassrls=no "
        "listener=127.0.0.1 secrets=redacted"
    )


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start() -> None:
    environment = load_environment()
    preflight()
    port = int(environment["KC_HTTP_PORT"])
    if not port_available(port):
        raise RuntimeError(f"Keycloak HTTP port {port} is already in use")
    if PID_FILE.exists():
        raise RuntimeError("keycloak.pid already exists; inspect status before starting")
    executable = Path(environment["JAVA_HOME"]) / "bin" / "java.exe"
    runner = KEYCLOAK_HOME / "lib" / "quarkus-run.jar"
    if not executable.is_file() or not runner.is_file():
        raise RuntimeError("Keycloak distribution is missing")
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    with STDOUT_LOG.open("ab") as stdout, STDERR_LOG.open("ab") as stderr:
        process = subprocess.Popen(
            [
                str(executable),
                "-Dprogram.name=kc.bat",
                "-Xms64m",
                "-Xmx512m",
                "-XX:MetaspaceSize=96M",
                "-XX:MaxMetaspaceSize=256m",
                "-XX:+ExitOnOutOfMemoryError",
                "-XX:+UseG1GC",
                "-Dfile.encoding=UTF-8",
                "-Duser.language=en",
                "-Duser.country=US",
                "--add-opens=java.base/java.util=ALL-UNNAMED",
                "--add-opens=java.base/java.util.concurrent=ALL-UNNAMED",
                "--add-opens=java.base/java.security=ALL-UNNAMED",
                "--add-opens=java.base/java.lang=ALL-UNNAMED",
                "--enable-native-access=ALL-UNNAMED",
                "-Djava.util.concurrent.ForkJoinPool.common.threadFactory="
                "io.quarkus.bootstrap.forkjoin.QuarkusForkJoinWorkerThreadFactory",
                "-Djava.util.logging.manager=org.jboss.logmanager.LogManager",
                "-Dquarkus-log-max-startup-records=10000",
                "-Dpicocli.disable.closures=true",
                f"-Dkc.home.dir={KEYCLOAK_HOME.as_posix()}",
                f"-Djboss.server.config.dir={(KEYCLOAK_HOME / 'conf').as_posix()}",
                f"-Dkeycloak.theme.dir={(KEYCLOAK_HOME / 'themes').as_posix()}",
                "-cp",
                str(runner),
                "io.quarkus.bootstrap.runner.QuarkusEntryPoint",
                "start-dev",
                "--import-realm",
            ],
            cwd=KEYCLOAK_HOME,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
            close_fds=True,
        )
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    print(f"KEYCLOAK_START_REQUESTED pid={process.pid} secrets=redacted")


def wait_ready(timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    urls = {
        "discovery": (
            "http://127.0.0.1:8080/realms/coifesp/"
            ".well-known/openid-configuration"
        ),
        "jwks": "http://127.0.0.1:8080/realms/coifesp/protocol/openid-connect/certs",
    }
    last_error = "not_started"
    with httpx.Client(
        timeout=3.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            try:
                discovery = client.get(urls["discovery"])
                jwks = client.get(urls["jwks"])
                if discovery.status_code == 200 and jwks.status_code == 200:
                    document = discovery.json()
                    keys = jwks.json().get("keys")
                    if (
                        document.get("issuer")
                        == "http://127.0.0.1:8080/realms/coifesp"
                        and isinstance(keys, list)
                        and keys
                    ):
                        print(
                            "KEYCLOAK_READY realm=coifesp discovery=yes jwks=yes "
                            f"signing_keys={len(keys)} secrets=redacted"
                        )
                        return
                last_error = f"http_{discovery.status_code}_{jwks.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = type(exc).__name__
            time.sleep(2)
    raise RuntimeError(f"Keycloak readiness timed out ({last_error})")


def status() -> None:
    if not PID_FILE.is_file():
        print("KEYCLOAK_STATUS pid=absent")
        return
    raw = PID_FILE.read_text(encoding="ascii").strip()
    if not raw.isdigit():
        raise RuntimeError("keycloak.pid is invalid")
    pid = int(raw)
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    running = str(pid) in result.stdout
    print(f"KEYCLOAK_STATUS pid={pid} running={'yes' if running else 'no'}")


def logs() -> None:
    for path in (STDOUT_LOG, STDERR_LOG):
        print(f"KEYCLOAK_LOG file={path.name}")
        if not path.exists():
            print("[absent]")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        for line in lines:
            safe = re.sub(
                r"(?i)(password|secret|token)([=: ]+)([^ ,;]+)",
                r"\1\2[REDACTED]",
                line,
            )
            print(safe)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the reviewed local Keycloak instance.")
    parser.add_argument(
        "command", choices=("preflight", "start", "wait", "status", "logs")
    )
    args = parser.parse_args()
    try:
        {
            "preflight": preflight,
            "start": start,
            "wait": wait_ready,
            "status": status,
            "logs": logs,
        }[
            args.command
        ]()
        return 0
    except Exception as exc:
        print(
            "KEYCLOAK_MANAGEMENT_FAILED "
            f"command={args.command} error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
