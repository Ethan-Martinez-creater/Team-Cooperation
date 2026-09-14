"""Start one shared local Agent Worker and one shared Tool Worker for all demo teams."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import psutil
from dotenv import dotenv_values
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RUNTIME = ROOT / "runtime-data" / "local-workers"
TENANTS = ("team-product", "team-engineering", "team-quality")
LOCAL_MODEL_AUTHORIZATION = ROOT / ".env.local-model-authorization"

from coifesp_harness.config import Settings
from coifesp_harness.connectors import (
    GITHUB_ADAPTER_PATHS,
    SQLAlchemyConnectorRegistry,
    load_connector_endpoints_for_tenants,
)
from coifesp_harness.connectors.runtime_client import (
    EMBEDDED_GITHUB_ORIGIN,
)
from coifesp_harness.errors import ResourceNotFound
from coifesp_harness.postgres_audit import (
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.security import Principal


def expected_worker_running(kind: str, pid: object) -> bool:
    if not isinstance(pid, int) or not psutil.pid_exists(pid):
        return False
    try:
        command = " ".join(psutil.Process(pid).cmdline()).lower()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False
    module = (
        "coifesp_harness.worker_main"
        if kind == "agent"
        else "coifesp_harness.tool_worker_main"
    )
    return module in command


def configure_local_github(environment: dict[str, str]) -> None:
    github_file = ROOT / ".env.github-adapter.local"
    github = (
        {
            key: value
            for key, value in dotenv_values(github_file).items()
            if isinstance(value, str)
        }
        if github_file.is_file()
        else {}
    )
    token = github.get("COIFESP_GITHUB_TOKEN")
    if not token:
        return
    secret_file = RUNTIME / "github-internal.json"
    if secret_file.is_file():
        internal_secret = json.loads(secret_file.read_text(encoding="utf-8"))["secret"]
    else:
        internal_secret = secrets.token_urlsafe(48)
        with secret_file.open("x", encoding="utf-8") as stream:
            json.dump({"secret": internal_secret}, stream)
    environment.update(
        {
            "COIFESP_GITHUB_TOKEN": token,
            "COIFESP_GITHUB_ADAPTER_MODE": "embedded",
            "COIFESP_GITHUB_ADAPTER_CLIENT_ID": "local-engineering-github",
            "COIFESP_GITHUB_ADAPTER_CLIENT_SECRET": internal_secret,
            "COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET": internal_secret,
            "COIFESP_GITHUB_REPOSITORIES": json.dumps(
                ["Ethan-Martinez-creater/Team-Cooperation-Test"]
            ),
            "COIFESP_GITHUB_ADAPTER_LEDGER": str(
                (RUNTIME / "github-engineering-ledger.sqlite3").resolve()
            ),
        }
    )
    document = [
        {
            "connector_id": "github-main",
            "tenant_id": "team-engineering",
            "base_url": EMBEDDED_GITHUB_ORIGIN,
            "token_endpoint": EMBEDDED_GITHUB_ORIGIN + "/oauth/token",
            "client_id": "local-engineering-github",
            "client_secret_env": "COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET",
            "scopes": ["github.adapter"],
            "allowed_paths": sorted(GITHUB_ADAPTER_PATHS),
            "max_classification": "internal",
            "timeout_seconds": 15,
            "max_response_bytes": 1048576,
            "max_attempts": 3,
            "circuit_failure_threshold": 5,
            "circuit_cooldown_seconds": 30,
        }
    ]
    environment["COIFESP_CONNECTORS_JSON"] = json.dumps(document)
    endpoint = load_connector_endpoints_for_tenants(
        environment["COIFESP_CONNECTORS_JSON"],
        tenant_ids=TENANTS,
        environment=environment,
    )[0]
    settings = Settings.from_environment(environment)
    engine = create_engine(
        settings.database_url, hide_parameters=True, pool_pre_ping=True
    )
    try:
        audit = SQLAlchemyAuditLog(
            engine=engine, keyring=AuditSigningKeyring.from_settings(settings)
        )
        registry = SQLAlchemyConnectorRegistry(engine=engine, audit_log=audit)
        registry.create_schema()
        try:
            registry.active_endpoint_for_worker(
                tenant_id="team-engineering",
                connector_id="github-main",
                environment=environment,
            )
        except ResourceNotFound:
            admin = Principal(
                "local-connector-admin",
                "team-engineering",
                frozenset({"connector_administrator"}),
            )
            reviewer = Principal(
                "local-connector-reviewer",
                "team-engineering",
                frozenset({"connector_reviewer"}),
            )
            _, _, version = registry.propose(
                principal=admin,
                endpoint=endpoint,
                client_secret_env="COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET",
            )
            registry.review(
                principal=reviewer,
                connector_id="github-main",
                expected_version=version,
                approve=True,
                reason="Local dedicated GitHub test repository",
            )
    finally:
        engine.dispose()


def configure_local_internal_model(environment: dict[str, str]) -> None:
    environment.pop("COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS", None)
    if not LOCAL_MODEL_AUTHORIZATION.is_file():
        return
    authorization = dotenv_values(LOCAL_MODEL_AUTHORIZATION)
    raw_ids = authorization.get("COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS")
    if not isinstance(raw_ids, str) or not raw_ids.strip():
        return
    provider_ids = tuple(item.strip() for item in raw_ids.split(","))
    raw_registry = environment.get("COIFESP_LLM_PROVIDERS_JSON")
    if not raw_registry:
        raise RuntimeError(
            "local INTERNAL model authorization requires COIFESP_LLM_PROVIDERS_JSON"
        )
    registry = json.loads(raw_registry)
    if not isinstance(registry, list):
        raise TypeError("local model provider registry must be a JSON array")
    matched: set[str] = set()
    updated = []
    for item in registry:
        if not isinstance(item, dict):
            raise TypeError("local model provider registry contains a non-object")
        candidate = dict(item)
        provider_id = candidate.get("provider_id")
        if provider_id in provider_ids:
            if candidate.get("max_data_classification") == "public":
                candidate["max_data_classification"] = "internal"
            matched.add(provider_id)
        updated.append(candidate)
    if matched != set(provider_ids):
        raise RuntimeError("local INTERNAL model authorization names an unknown provider")
    environment["COIFESP_LLM_PROVIDERS_JSON"] = json.dumps(updated)
    environment["COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS"] = ",".join(provider_ids)


def configure_local_artifact_store(environment: dict[str, str]) -> None:
    """Keep local-demo artifacts inside the project-owned runtime directory."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    artifact_root = RUNTIME / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    environment["COIFESP_ARTIFACT_STORE_ROOT"] = str(artifact_root.resolve())


def build_local_environment() -> dict[str, str]:
    configured = {
        key: value
        for key, value in dotenv_values(ROOT / ".env").items()
        if isinstance(value, str)
    }
    environment = os.environ.copy()
    environment.update(configured)
    environment["COIFESP_AUTH_MODE"] = "local"
    environment["COIFESP_LOCAL_WORKER_TENANTS"] = ",".join(TENANTS)
    configure_local_artifact_store(environment)
    configure_local_internal_model(environment)
    for legacy in (
        "COIFESP_WORKER_TENANT_ID",
        "COIFESP_TOOL_WORKER_TENANT_ID",
        "COIFESP_WORKER_TENANTS",
        "COIFESP_TOOL_WORKER_TENANTS",
    ):
        # An explicit empty value prevents load_dotenv(override=False) from
        # restoring a legacy single-tenant value from .env in the child.
        environment[legacy] = ""
    environment["PYTHONPATH"] = str(ROOT / "src")
    configure_local_github(environment)
    return environment


def main() -> int:
    environment = build_local_environment()
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    manifest_path = RUNTIME / "workers.json"
    if manifest_path.is_file():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(expected_worker_running(kind, pid) for kind, pid in prior.items()):
            print("LOCAL_WORKERS_ALREADY_RUNNING")
            return 1
    processes: list[tuple[str, subprocess.Popen]] = []
    streams = []
    try:
        for kind, module in (
            ("agent", "coifesp_harness.worker_main"),
            ("tool", "coifesp_harness.tool_worker_main"),
        ):
            prefix = RUNTIME / kind
            stdout = (prefix.with_suffix(".stdout.log")).open("ab")
            stderr = (prefix.with_suffix(".stderr.log")).open("ab")
            streams.extend((stdout, stderr))
            process = subprocess.Popen(
                [sys.executable, "-B", "-m", module],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=creation_flags,
            )
            processes.append((kind, process))
        time.sleep(3)
        failed = [
            (kind, process.returncode)
            for kind, process in processes
            if process.poll() is not None
        ]
        if failed:
            for _, process in processes:
                if process.poll() is None:
                    process.terminate()
            print(f"LOCAL_WORKERS_START_FAILED processes={failed}")
            return 1
        manifest = {kind: process.pid for kind, process in processes}
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("LOCAL_WORKERS_STARTED tenants=3 agent_workers=1 tool_workers=1")
        return 0
    finally:
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
