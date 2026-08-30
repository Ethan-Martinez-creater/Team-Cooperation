import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_task_verification_service import state, verified_events, verify
from test_verification_tool_checks import Coordinator, jobs, tool_setup

from coifesp_harness.sandbox import (
    SandboxErrorCode,
    SandboxResult,
    SandboxWorkspaceManager,
    WorkspaceAccess,
)
from coifesp_harness.tool_jobs import TOOL_JOBS, DurableToolWorker, ToolJobStatus
from coifesp_harness.tools import ToolRegistry
from coifesp_harness.verification.sandbox_tool import SandboxedVerificationTool
from coifesp_harness.verification.tool_checks import VerificationToolReconciler


class Sandbox:
    """Fake OCI adapter only; the queue, worker, content and projections are real."""

    def __init__(self, *, exit_code=0, unavailable=False):
        self.exit_code = exit_code
        self.unavailable = unavailable
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        assert request.workspace_access is WorkspaceAccess.READ_ONLY
        assert request.argv == ("/usr/local/bin/python", "/opt/verify.py")
        manifest = json.loads((request.workspace / "verification-inputs.json").read_text())
        assert request.workspace.name.startswith(".verification-attempt-")
        assert not (request.workspace / ".coifesp-workspace.json").exists()
        assert not list((request.workspace / "artifacts").glob(".verification-artifact-*"))
        assert manifest["schema"] == "coifesp.verification-inputs.v1"
        for artifact in manifest["artifacts"]:
            blob = (request.workspace / artifact["relative_path"]).read_bytes()
            assert hashlib.sha256(blob).hexdigest() == artifact["sha256"]
            assert len(blob) == artifact["size_bytes"]
        if self.unavailable:
            return SandboxResult(request.execution_id, None, b"", b"",
                                 SandboxErrorCode.RUNTIME_UNAVAILABLE)
        return SandboxResult(
            request.execution_id, self.exit_code, b"private tool output", b"private errors",
            SandboxErrorCode.EXECUTION_FAILED if self.exit_code else None,
        )


def worker(value, tmp_path, sandbox):
    root = tmp_path / "workspaces"
    registry = ToolRegistry()
    registry.register(SandboxedVerificationTool(
        engine=value.engine, sandbox=sandbox, profiles=(value.profile,),
        workspace_root=root, artifact_content=value.content,
    ).definition())
    return DurableToolWorker(
        repository=value.jobs, registry=registry, tenant_id="team-b", worker_id="tools-b",
        workspace_manager=SandboxWorkspaceManager(root=root),
        reconciler=VerificationToolReconciler(coordinator=Coordinator(), verifier=value.verifier),
    )


@pytest.mark.parametrize("exit_code, expected", [(0, "verified"), (1, "changes_requested")])
def test_submission_queue_real_tool_worker_and_verification_projection(tmp_path, exit_code, expected):
    value = tool_setup(tmp_path)
    verify(value)
    sandbox = Sandbox(exit_code=exit_code)
    executor = worker(value, tmp_path, sandbox)
    assert asyncio.run(executor.run_once()) is True
    assert jobs(value)[0].status is ToolJobStatus.SUCCEEDED
    assert state(value)[0]["status"] == expected
    assert "private tool" not in str(jobs(value)[0].result)
    assert asyncio.run(executor.run_once()) is False
    assert len(sandbox.requests) == 1
    assert len(verified_events(value)) == (1 if exit_code == 0 else 0)


def test_runtime_unavailable_retries_same_job_and_recovers(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    sandbox = Sandbox(unavailable=True)
    executor = worker(value, tmp_path, sandbox)
    assert asyncio.run(executor.run_once()) is True
    original = jobs(value)[0]
    assert original.status is ToolJobStatus.RETRY_WAIT
    assert state(value)[0]["status"] == "submitted"
    with value.engine.begin() as connection:
        connection.execute(TOOL_JOBS.update().values(
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        ))
    sandbox.unavailable = False
    assert asyncio.run(executor.run_once()) is True
    assert len(jobs(value)) == 1 and jobs(value)[0].job_id == original.job_id
    assert jobs(value)[0].attempt_count == 2
    assert state(value)[0]["status"] == "verified"
    assert len(verified_events(value)) == 1


def test_worker_completion_before_projection_crash_is_replayed(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    sandbox = Sandbox()
    executor = worker(value, tmp_path, sandbox)
    executor.reconciler = None  # Simulate stopping before the post-job recovery hook.
    assert asyncio.run(executor.run_once()) is True
    assert state(value)[0]["status"] == "submitted"
    assert jobs(value)[0].status is ToolJobStatus.SUCCEEDED
    assert value.verifier.replay_pending() == 1
    assert state(value)[0]["status"] == "verified"
    assert len(verified_events(value)) == 1 and len(sandbox.requests) == 1


def test_slow_staging_keeps_async_loop_available(tmp_path, monkeypatch):
    value = tool_setup(tmp_path)
    verify(value)
    executor = worker(value, tmp_path, Sandbox())
    tool = executor.registry.get("verification.run_profile").handler.__self__
    original = tool._prepare_execution_workspace
    entered, release = threading.Event(), threading.Event()

    def slow(**kwargs):
        entered.set()
        assert release.wait(5), "input staging blocked the event loop"
        return original(**kwargs)

    monkeypatch.setattr(tool, "_prepare_execution_workspace", slow)

    async def scenario():
        async def tick():
            for _ in range(500):
                if entered.is_set():
                    release.set()
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("worker never began staging")

        try:
            outcome, _ = await asyncio.gather(executor.run_once(), tick())
            assert outcome is True
        finally:
            release.set()

    asyncio.run(scenario())
    assert state(value)[0]["status"] == "verified"
