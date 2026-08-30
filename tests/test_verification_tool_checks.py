from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from test_task_verification_service import setup, state, verified_events, verify

from coifesp_harness.errors import IntegrityError, PolicyDenied, ResourceNotFound
from coifesp_harness.product.repository import TEAM_TASKS
from coifesp_harness.sandbox import CodeProfile, WorkspaceAccess
from coifesp_harness.tool_jobs import (
    TOOL_JOBS,
    SQLAlchemyToolJobRepository,
    ToolJobKeyring,
)
from coifesp_harness.verification.repository import TASK_VERIFICATIONS
from coifesp_harness.verification.service import TaskVerificationService
from coifesp_harness.verification.tool_checks import (
    RESULT_SCHEMA,
    DurableVerificationChecks,
    VerificationToolReconciler,
)


def tool_setup(tmp_path, *, required=True, configured=True):
    value = setup(tmp_path, verification_policy={"criteria": [{
        "criterion_id": "tests", "type": "tool_check",
        "tool": "sandbox.profile:pytest", "required": required,
    }]})
    value.jobs = SQLAlchemyToolJobRepository(
        engine=value.engine, keyring=ToolJobKeyring(master_key=b"t" * 32, key_id="test"),
    )
    value.jobs.create_schema()
    value.profile = CodeProfile(
        profile_id="pytest", image="test/python@sha256:" + "a" * 64,
        executable="/usr/local/bin/python", fixed_arguments=("/opt/verify.py",),
        workspace_access=WorkspaceAccess.READ_ONLY,
    )
    value.verifier.tool_checks = DurableVerificationChecks(
        jobs=value.jobs, profiles=(value.profile,) if configured else (),
    )
    return value


def jobs(value):
    return value.jobs.list_for_run(tenant_id="team-b", run_id=value.dispatched.run_id,
                                  include_payloads=True)


def complete(value, *, exit_code=0, overrides=None, failed=False):
    lease = value.jobs.claim_next(tenant_id="team-b", worker_id="worker", lease_seconds=30)
    assert lease is not None
    args = lease.job.arguments
    lease_args = {"tenant_id": "team-b", "job_id": lease.job.job_id,
                  "worker_id": "worker", "lease_token": lease.lease_token}
    value.jobs.start(**lease_args)
    result = {key: args[key] for key in (
        "verification_id", "subject_digest", "criterion_id", "profile_digest"
    )}
    result.update(schema=RESULT_SCHEMA, exit_code=exit_code,
                  timed_out=False, output_truncated=False)
    result.update(overrides or {})
    if failed:
        value.jobs.fail(**lease_args, error_code="runtime_unavailable", retryable=False)
    else:
        value.jobs.succeed(**lease_args, result=result)


def test_tool_enqueue_and_verification_are_idempotent_then_terminal_once(tmp_path):
    value = tool_setup(tmp_path)
    first = verify(value)
    assert first["status"] == "PENDING"
    assert state(value)[0]["status"] == "submitted"
    assert verify(value) == first
    assert len(jobs(value)) == 1
    job = jobs(value)[0]
    assert job.created_by == "service:project-verifier"
    assert job.run_id == value.dispatched.run_id
    assert "artifact" not in str(job.arguments)
    assert first["checks"][1]["tool_job_id"] == job.job_id
    complete(value)
    assert verify(value)["status"] == "PASS"
    assert state(value)[0]["status"] == "verified"
    assert len(verified_events(value)) == 1
    verify(value)
    assert len(verified_events(value)) == 1 and len(jobs(value)) == 1


def test_nonzero_tool_exit_requests_changes_without_exposing_output(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value, exit_code=1)
    result = verify(value)
    assert result["status"] == "FAIL"
    assert result["checks"][1]["code"] == "tool_check_failed"
    assert state(value)[0]["status"] == "changes_requested"
    assert not verified_events(value)


@pytest.mark.parametrize("overrides", [
    {"subject_digest": "b" * 64}, {"verification_id": "another"},
    {"criterion_id": "another"}, {"profile_digest": "b" * 64},
    {"schema": "unknown"}, {"exit_code": False}, {"timed_out": "false"},
    {"stdout": "private output"}, {"output_truncated": 0},
])
def test_unbound_or_malformed_result_is_not_evidence(tmp_path, overrides):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value, overrides=overrides)
    result = verify(value)
    assert result["status"] == "PENDING"
    assert result["checks"][1]["code"] == "tool_result_invalid"
    assert state(value)[0]["status"] == "submitted"
    assert "private output" not in str(result)


@pytest.mark.parametrize("flag", ["timed_out", "output_truncated"])
def test_incomplete_execution_cannot_pass(tmp_path, flag):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value, overrides={flag: True})
    assert verify(value)["checks"][1]["code"] == "tool_execution_incomplete"
    assert state(value)[0]["status"] == "submitted"


def test_infrastructure_failure_does_not_fail_submission(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value, failed=True)
    assert verify(value)["checks"][1]["code"] == "tool_execution_unavailable"
    assert state(value)[0]["status"] == "submitted"


def test_explicit_retry_keeps_old_job_and_completes_new_attempt_once(tmp_path):
    value = tool_setup(tmp_path)
    original = verify(value)
    complete(value, failed=True)
    verify(value)
    retried = value.verifier.verify_task(
        project_id="project-a", task_id="task-a", actor_id="lead-a", retry_tools=True,
    )
    check = retried["checks"][1]
    assert check["tool_attempt"] == 2
    assert check["superseded_tool_job_ids"] == [original["checks"][1]["tool_job_id"]]
    value.verifier.verify_task(
        project_id="project-a", task_id="task-a", actor_id="lead-a", retry_tools=True,
    )
    assert len(jobs(value)) == 2
    complete(value)
    assert verify(value)["status"] == "PASS"
    assert len(verified_events(value)) == 1


def test_automatic_recovery_cannot_request_unbounded_retries(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value, failed=True)
    for _ in range(3):
        verify(value)
    assert len(jobs(value)) == 1
    with pytest.raises(PolicyDenied, match="authorized"):
        value.verifier.verify_run(run_id=value.dispatched.run_id, retry_tools=True)


def test_retry_requires_current_task_party(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value, failed=True)
    with pytest.raises(ResourceNotFound):
        value.verifier.verify_task(
            project_id="project-a", task_id="task-a", actor_id="other", retry_tools=True,
        )
    assert len(jobs(value)) == 1


def test_corrupt_artifact_baseline_never_dispatches_tool(tmp_path, monkeypatch):
    value = tool_setup(tmp_path)

    def corrupt(**kwargs):
        raise IntegrityError("corrupt fixture")

    monkeypatch.setattr(value.content, "open_policy_authorized", corrupt)
    assert verify(value)["status"] == "FAIL"
    assert not jobs(value)


def test_changed_submission_discards_completed_tool_evidence(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
    assert verify(value)["status"] == "STALE"
    assert state(value)[0]["status"] == "changes_requested"
    assert not verified_events(value)


def test_profile_configuration_change_does_not_reinterpret_pinned_job(tmp_path):
    value = tool_setup(tmp_path)
    first = verify(value)
    value.verifier.tool_checks = DurableVerificationChecks(
        jobs=value.jobs, profiles=(replace(value.profile, fixed_arguments=("changed",)),),
    )
    assert verify(value) == first
    complete(value)
    assert verify(value)["status"] == "PASS"
    assert len(jobs(value)) == 1


def test_unconfigured_profile_can_be_configured_then_replayed(tmp_path):
    value = tool_setup(tmp_path, configured=False)
    assert verify(value)["checks"][1]["code"] == "verification_profile_unavailable"
    assert not jobs(value)
    value.verifier.tool_checks = DurableVerificationChecks(jobs=value.jobs, profiles=(value.profile,))
    verify(value)
    assert len(jobs(value)) == 1


def test_optional_profile_does_not_enqueue_or_block_completion(tmp_path):
    value = tool_setup(tmp_path, required=False)
    result = verify(value)
    assert result["status"] == "PASS"
    assert result["checks"][1]["code"] == "optional_tool_not_scheduled"
    assert not jobs(value)


def test_verification_write_failure_rolls_back_tool_enqueue(tmp_path):
    value = tool_setup(tmp_path)

    def fail_write(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO task_verifications"):
            raise RuntimeError("injected verification write failure")

    event.listen(value.engine, "before_cursor_execute", fail_write)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            verify(value)
    finally:
        event.remove(value.engine, "before_cursor_execute", fail_write)
    assert not jobs(value)
    with value.engine.connect() as connection:
        assert connection.execute(select(TASK_VERIFICATIONS)).first() is None
        assert connection.execute(select(TOOL_JOBS)).first() is None
    verify(value)
    assert len(jobs(value)) == 1


class Coordinator:
    def __init__(self):
        self.calls = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)
        return 0


def test_fresh_reconciler_recovers_tool_completion_for_own_team_only(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    complete(value)
    verifier = TaskVerificationService(
        repository=value.repository, artifact_content=value.content,
        tool_checks=DurableVerificationChecks(jobs=value.jobs, profiles=(value.profile,)),
    )
    coordinator = Coordinator()
    reconciler = VerificationToolReconciler(coordinator=coordinator, verifier=verifier)
    assert reconciler.reconcile(tenant_id="team-a", actor_id="worker") == 0
    assert state(value)[0]["status"] == "submitted"
    assert reconciler.reconcile(tenant_id="team-b", actor_id="worker") == 1
    assert len(verified_events(value)) == 1
    assert reconciler.reconcile(tenant_id="team-b", actor_id="worker") == 0
    assert len(coordinator.calls) == 3


def test_bounded_replay_rotates_past_unavailable_records(tmp_path):
    value = tool_setup(tmp_path)
    verify(value)
    with value.engine.begin() as connection:
        original = dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one())
        connection.execute(TASK_VERIFICATIONS.insert().values(
            **{**original, "verification_id": "verification:zz", "subject_digest": "f" * 64}
        ))
    pending = SimpleNamespace(repository=value.repository,
                              verify_run=lambda **_: {"status": "PENDING"})
    reconciler = VerificationToolReconciler(coordinator=Coordinator(), verifier=pending)
    reconciler.reconcile(tenant_id="team-b", actor_id="worker", limit=1)
    assert reconciler._after["team-b"] == original["verification_id"]
    reconciler.reconcile(tenant_id="team-b", actor_id="worker", limit=1)
    assert reconciler._after["team-b"] == "verification:zz"
    reconciler.reconcile(tenant_id="team-b", actor_id="worker", limit=1)
    assert reconciler._after["team-b"] == original["verification_id"]
