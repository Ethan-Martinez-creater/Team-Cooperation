import asyncio
import json
from dataclasses import replace

from test_bootstrap import StubVerifier, production_settings, sqlite_engine

from coifesp_harness.config import SecretValue, Settings
from coifesp_harness.control_plane import build_application
from coifesp_harness.tool_catalog import build_builtin_manifests
from coifesp_harness.verification.tool_checks import (
    DurableVerificationChecks,
    VerificationToolReconciler,
)


def settings(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return replace(
        production_settings(), artifact_store_root=str(artifacts),
        sandbox_workspace_root=str(tmp_path / "workspaces"), sandbox_runtime="docker",
        sandbox_profiles_json=json.dumps([{
            "profile_id": "pytest", "image": "test/python@sha256:" + "a" * 64,
            "executable": "/usr/local/bin/python", "fixed_arguments": ["/opt/verify.py"],
            "workspace_access": "read_only",
            "arguments_schema": {"type": "array", "maxItems": 0},
            "timeout_seconds": 180, "memory_bytes": 536870912,
            "cpu_count": 1.0, "pids": 128, "output_bytes": 1048576,
            "tmpfs_bytes": 67108864,
        }]),
        tool_worker_token_endpoint="https://identity.example.test/token",
        tool_worker_client_id="test-tool-worker",
        tool_worker_client_secret=SecretValue("fixture-secret"), tool_worker_tenant_id="team-b",
    )


def test_control_plane_constructs_queue_bridge_only_when_profiles_configured(tmp_path):
    engine = sqlite_engine()
    app = build_application(settings=settings(tmp_path), engine=engine,
                            verifier=StubVerifier(), readiness_probe=lambda: True)
    bridge = app.state.task_verification_service.tool_checks
    assert isinstance(bridge, DurableVerificationChecks)
    assert bridge.jobs.engine is engine
    assert set(bridge.profiles) == {"pytest"}
    assert app.state.agent_capabilities.manifests["code.run_profile"].timeout_seconds == 190


def test_tool_worker_wires_internal_handler_and_reconciler_not_agent_catalog(tmp_path, monkeypatch):
    from coifesp_harness import tool_worker_main as module

    engine = sqlite_engine()

    class AsyncResource:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(Settings, "validate", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(module, "DatabaseReadinessProbe", lambda _: lambda: True)
    monkeypatch.setattr(module, "OIDCVerifier", AsyncResource)
    monkeypatch.setattr(module, "ClientCredentialsTokenProvider", AsyncResource)
    runtime = asyncio.run(module.build_tool_worker_runtime(settings(tmp_path)))
    try:
        assert isinstance(runtime.runner.reconciler, VerificationToolReconciler)
        assert runtime.runner.reconciler.verifier.repository.engine is engine
        assert runtime.runner.reconciler.verifier.notifier is not None
        names = {definition.name for definition in runtime.runner.registry.definitions()}
        assert "code.run_profile" in names and "verification.run_profile" in names
        assert runtime.runner.registry.get("code.run_profile").timeout_seconds == 190
        assert runtime.runner.registry.get("verification.run_profile").timeout_seconds >= 190
        manifests = build_builtin_manifests(sandbox_profile_ids=("pytest",), sandbox_timeout_seconds=190)
        assert "verification.run_profile" not in {manifest.tool_id for manifest in manifests}
    finally:
        asyncio.run(runtime.aclose())
