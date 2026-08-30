import asyncio
import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_task_verification_service import setup as build_task

from coifesp_harness.product.repository import PROJECT_AGENT_RUNS
from coifesp_harness.sandbox.models import (
    SandboxErrorCode,
    SandboxLimits,
    SandboxResult,
    WorkspaceAccess,
)
from coifesp_harness.sandbox.tools import CodeProfile
from coifesp_harness.sandbox.workspace import SandboxWorkspaceManager
from coifesp_harness.tool_jobs import PermanentToolError, RetryableToolError
from coifesp_harness.verification.repository import TASK_VERIFICATIONS
from coifesp_harness.verification.sandbox_tool import (
    SandboxedVerificationTool,
    profile_digest,
)

_IMAGE = "python@sha256:" + "a" * 64
_CRITERION_ID = "sandbox-check"
_JOB_ID = "tool-job-1"


class RecordingContent:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def open_policy_authorized(self, **kwargs):
        self.calls.append(kwargs)
        yield self.payload


class FakeOCI:
    """In-memory OCI boundary; no Docker or Podman process is started."""

    def __init__(self, error_code: SandboxErrorCode | None = None, exit_code: int = 0):
        self.requests = []
        self.error_code = error_code
        self.exit_code = exit_code

    async def execute(self, request):
        self.requests.append(request)
        return SandboxResult(
            execution_id=request.execution_id,
            exit_code=None if self.error_code is SandboxErrorCode.RUNTIME_UNAVAILABLE else self.exit_code,
            stdout=b"sensitive stdout",
            stderr=b"sensitive stderr",
            error_code=self.error_code,
        )


@pytest.fixture
def case(tmp_path):
    value = build_task(tmp_path)
    with value.engine.connect() as connection:
        binding = (
            connection.execute(
                select(PROJECT_AGENT_RUNS).where(
                    PROJECT_AGENT_RUNS.c.run_id == value.dispatched.run_id
                )
            )
            .mappings()
            .one()
        )

    artifacts = binding["task_result_json"]["artifact_manifests"]
    raw = b"contracted project input"
    assert len(artifacts) == 1
    assert artifacts[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifacts[0]["size_bytes"] == len(raw)

    profile = CodeProfile(
        profile_id="python-check",
        image=_IMAGE,
        executable="/usr/local/bin/check",
        arguments_schema={"type": "array", "maxItems": 0},
        limits=SandboxLimits(timeout_seconds=12),
        workspace_access=WorkspaceAccess.READ_ONLY,
    )
    job_id = _JOB_ID
    subject_digest = "b" * 64
    verification_id = "verification-sandbox"
    check = {
        "criterion_id": _CRITERION_ID,
        "type": "tool_check",
        "tool": f"sandbox.profile:{profile.profile_id}",
        "required": True,
        "status": "PENDING",
        "code": "queued",
        "evidence_refs": [],
        "tool_job_id": job_id,
        "profile_digest": profile_digest(profile),
    }
    policy = {
        "criteria": [
            {
                "criterion_id": _CRITERION_ID,
                "type": "tool_check",
                "tool": f"sandbox.profile:{profile.profile_id}",
                "required": True,
            }
        ]
    }
    now = datetime.now(UTC)
    with value.engine.begin() as connection:
        connection.execute(
            TASK_VERIFICATIONS.insert().values(
                verification_id=verification_id,
                project_id="project-a",
                process_id="process-a",
                task_id="task-a",
                source_run_id=value.dispatched.run_id,
                contract_version=binding["task_contract_version"],
                subject_digest=subject_digest,
                policy_json=policy,
                artifacts_json=artifacts,
                checks_json=[check],
                status="PENDING",
                initiated_by="test",
                executed_as="service:project-verifier",
                version=1,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
    )

    workspace_root = tmp_path / "verification-workspaces"
    # Use the production manager so Windows tests exercise the same extended
    # local path returned to the verifier, even when pytest's basetemp is
    # already close to MAX_PATH.
    job_workspace = SandboxWorkspaceManager(root=workspace_root).prepare(
        tenant_id="team-b", job_id=job_id
    ).path
    # These files represent an earlier worker attempt and a private job marker.
    # A verification attempt must never mount this dirty outer directory.
    (job_workspace / "old-job-secret.txt").write_text("tenant-private", encoding="utf-8")
    old_attempt = job_workspace / ".verification-attempt-old"
    old_attempt.mkdir(mode=0o700)
    (old_attempt / "old-attempt-secret.txt").write_text("stale", encoding="utf-8")

    content = RecordingContent(raw)
    sandbox = FakeOCI()
    tool = SandboxedVerificationTool(
        engine=value.engine,
        sandbox=sandbox,
        profiles=(profile,),
        workspace_root=workspace_root,
        artifact_content=content,
    )
    arguments = {
        "verification_id": verification_id,
        "subject_digest": subject_digest,
        "criterion_id": _CRITERION_ID,
        "profile_id": profile.profile_id,
        "profile_digest": profile_digest(profile),
    }
    return SimpleNamespace(
        value=value,
        profile=profile,
        tool=tool,
        sandbox=sandbox,
        content=content,
        arguments=arguments,
        artifacts=artifacts,
        raw=raw,
        workspace_root=workspace_root,
        job_workspace=job_workspace,
        run_id=value.dispatched.run_id,
        verification_id=verification_id,
        job_id=job_id,
        subject_digest=subject_digest,
    )


def _set_context(monkeypatch, item, *, tenant_id="team-b", job_id=None, run_id=None):
    monkeypatch.setattr(
        "coifesp_harness.verification.sandbox_tool.current_tool_execution_context",
        lambda: SimpleNamespace(
            tenant_id=tenant_id,
            job_id=job_id or item.job_id,
            run_id=run_id or item.run_id,
            call_id="tool-call-1",
            idempotency_key="tool-idempotency-1",
        ),
    )


def _run(tool, arguments):
    return asyncio.run(tool.run_profile(arguments))


def test_definition_uses_profile_timeout_and_exact_frozen_arguments(case, monkeypatch):
    definition = case.tool.definition()

    assert definition.name == "verification.run_profile"
    assert definition.timeout_seconds == case.profile.limits.timeout_seconds + 60
    assert set(definition.parameters_schema["required"]) == {
        "verification_id",
        "subject_digest",
        "criterion_id",
        "profile_id",
        "profile_digest",
    }
    assert definition.parameters_schema["additionalProperties"] is False

    # The worker normally installs this context before invoking a handler;
    # patch it here because this test calls the handler directly.
    _set_context(monkeypatch, case)
    with pytest.raises(PermanentToolError) as exc:
        _run(case.tool, {**case.arguments, "arguments": []})
    assert exc.value.error_code == "verification_request_denied"


def test_criterion_id_is_an_opaque_nonempty_contract_string(case):
    criterion = "检查/criterion/" + ("é" * 700)
    request = {**case.arguments, "criterion_id": criterion}

    assert SandboxedVerificationTool._request(request)["criterion_id"] == criterion


@pytest.mark.parametrize("denial", ["tenant", "run", "job", "status"])
def test_persisted_binding_denials_happen_before_content_reads(case, monkeypatch, denial):
    if denial == "tenant":
        _set_context(monkeypatch, case, tenant_id="team-a")
    elif denial == "run":
        _set_context(monkeypatch, case, run_id="other-run")
    elif denial == "job":
        _set_context(monkeypatch, case, job_id="other-job")
    else:
        _set_context(monkeypatch, case)
        with case.value.engine.begin() as connection:
            connection.execute(
                TASK_VERIFICATIONS.update()
                .where(TASK_VERIFICATIONS.c.verification_id == case.verification_id)
                .values(status="PASS", completed_at=datetime.now(UTC))
            )

    with pytest.raises(PermanentToolError) as exc:
        _run(case.tool, case.arguments)

    assert exc.value.error_code == "verification_request_denied"
    assert case.content.calls == []
    assert case.sandbox.requests == []


def test_empty_artifact_snapshot_cannot_bypass_cross_tenant_binding(case, monkeypatch):
    with case.value.engine.begin() as connection:
        connection.execute(
            TASK_VERIFICATIONS.update()
            .where(TASK_VERIFICATIONS.c.verification_id == case.verification_id)
            .values(artifacts_json=[])
        )
    _set_context(monkeypatch, case, tenant_id="team-a")

    with pytest.raises(PermanentToolError) as exc:
        _run(case.tool, case.arguments)

    assert exc.value.error_code == "verification_request_denied"
    assert case.content.calls == []
    assert case.sandbox.requests == []


def test_unknown_or_digest_mismatched_profile_is_denied_before_content(case, monkeypatch):
    _set_context(monkeypatch, case)
    arguments = {
        **case.arguments,
        "profile_id": "other-profile",
        "profile_digest": "c" * 64,
    }

    with pytest.raises(PermanentToolError) as exc:
        _run(case.tool, arguments)

    assert exc.value.error_code == "verification_profile_denied"
    assert case.content.calls == []
    assert case.sandbox.requests == []


def test_read_write_profile_cannot_be_used_by_verification_tool(case, monkeypatch):
    write_profile = replace(
        case.profile,
        profile_id="write-check",
        workspace_access=WorkspaceAccess.READ_WRITE,
    )
    tool = SandboxedVerificationTool(
        engine=case.value.engine,
        sandbox=case.sandbox,
        profiles=(write_profile,),
        workspace_root=case.workspace_root,
        artifact_content=case.content,
    )
    arguments = {
        **case.arguments,
        "profile_id": write_profile.profile_id,
        "profile_digest": profile_digest(write_profile),
    }
    _set_context(monkeypatch, case)

    with pytest.raises(PermanentToolError) as exc:
        _run(tool, arguments)

    assert exc.value.error_code == "verification_profile_denied"
    assert case.content.calls == []
    assert case.sandbox.requests == []


def test_corrupted_artifact_bytes_fail_without_invoking_oci(case, monkeypatch):
    _set_context(monkeypatch, case)
    case.content.payload = b"tampered artifact"

    with pytest.raises(PermanentToolError) as exc:
        _run(case.tool, case.arguments)

    assert exc.value.error_code == "verification_artifact_corrupt"
    assert len(case.content.calls) == 1
    assert case.sandbox.requests == []


def test_unsafe_direct_child_is_rejected_before_content_reads(case, monkeypatch):
    unsafe = case.job_workspace / "unsafe-link"
    unsafe.mkdir()
    original = SandboxedVerificationTool._is_link_or_reparse

    def simulated_reparse(path: Path) -> bool:
        return path.name == unsafe.name or original(path)

    monkeypatch.setattr(
        SandboxedVerificationTool,
        "_is_link_or_reparse",
        staticmethod(simulated_reparse),
    )
    _set_context(monkeypatch, case)

    with pytest.raises(PermanentToolError) as exc:
        _run(case.tool, case.arguments)

    assert exc.value.error_code == "verification_workspace_denied"
    assert case.content.calls == []
    assert case.sandbox.requests == []


def test_staging_is_fresh_readable_and_does_not_mount_dirty_job_directory(case, monkeypatch):
    _set_context(monkeypatch, case)
    result = _run(case.tool, case.arguments)

    assert result == {
        "schema": "coifesp.verification-tool-result.v1",
        "verification_id": case.verification_id,
        "subject_digest": case.subject_digest,
        "criterion_id": _CRITERION_ID,
        "profile_digest": profile_digest(case.profile),
        "exit_code": 0,
        "timed_out": False,
        "output_truncated": False,
    }
    request = case.sandbox.requests[0]
    staged = request.workspace
    assert staged != case.job_workspace
    assert staged.parent == case.job_workspace
    assert request.workspace_access is WorkspaceAccess.READ_ONLY
    assert not (staged / "old-job-secret.txt").exists()
    assert not (staged / ".verification-attempt-old").exists()
    assert (staged / "artifacts" / case.artifacts[0]["sha256"]).read_bytes() == case.raw

    if os.name != "nt":
        assert stat.S_IMODE(staged.stat().st_mode) == 0o755
        assert stat.S_IMODE((staged / "artifacts").stat().st_mode) == 0o755
        assert stat.S_IMODE(
            (staged / "artifacts" / case.artifacts[0]["sha256"]).stat().st_mode
        ) == 0o444
        assert stat.S_IMODE((staged / "verification-inputs.json").stat().st_mode) == 0o444


def test_repeated_attempts_receive_distinct_fresh_staging_directories(case, monkeypatch):
    _set_context(monkeypatch, case)
    _run(case.tool, case.arguments)
    first = case.sandbox.requests[-1].workspace
    (first / "attempt-secret.txt").write_text("must not carry forward", encoding="utf-8")

    _run(case.tool, case.arguments)
    second = case.sandbox.requests[-1].workspace

    assert second != first
    assert first.exists()
    assert not (second / "attempt-secret.txt").exists()
    assert len(case.content.calls) == 2
    assert len(case.sandbox.requests) == 2


def test_nonzero_execution_is_a_structured_result_without_output_leaks(case, monkeypatch):
    case.sandbox.exit_code = 7
    case.sandbox.error_code = SandboxErrorCode.EXECUTION_FAILED
    _set_context(monkeypatch, case)

    result = _run(case.tool, case.arguments)

    assert result["exit_code"] == 7
    assert set(result) == {
        "schema",
        "verification_id",
        "subject_digest",
        "criterion_id",
        "profile_digest",
        "exit_code",
        "timed_out",
        "output_truncated",
    }
    assert "stdout_utf8" not in result and "stderr_utf8" not in result
    assert "sensitive stdout" not in json.dumps(result)
    assert "sensitive stderr" not in json.dumps(result)


def test_runtime_unavailable_is_retryable(case, monkeypatch):
    case.sandbox.error_code = SandboxErrorCode.RUNTIME_UNAVAILABLE
    _set_context(monkeypatch, case)

    with pytest.raises(RetryableToolError) as exc:
        _run(case.tool, case.arguments)

    assert exc.value.error_code == "verification_runtime_unavailable"
    assert len(case.content.calls) == 1
    assert len(case.sandbox.requests) == 1
