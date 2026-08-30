from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_task_verification_service import setup
from test_team_task_result_projection import attach_scheduler, finish, state

from coifesp_harness.capabilities.repository import CAPACITY_RESERVATIONS
from coifesp_harness.project_process.scheduler import PROJECT_PROCESS_WAKEUPS
from coifesp_harness.verification.worker_runtime import configure_worker_reviews


def configured(tmp_path):
    value = setup(tmp_path, completed=False)
    attach_scheduler(value)  # schema only; configured Worker uses its own listener
    reconciler = configure_worker_reviews(
        settings=SimpleNamespace(artifact_store_root=str(tmp_path), artifact_max_upload_bytes=100000,
                                 sandbox_profiles_json=None),
        engine=value.engine, service=value.dispatcher.run_service, jobs=None,
        audit=value.capabilities.audit_log)
    return value, reconciler


@pytest.mark.parametrize("lost_callback", [False, True])
def test_standalone_worker_projects_settles_verifies_and_wakes_without_api(tmp_path, lost_callback):
    value, reconciler = configured(tmp_path)
    callback = value.dispatcher.run_service.terminal_callback
    finish(value, callback=None if lost_callback else callback)
    if lost_callback:
        assert state(value)[0]["status"] == "in_progress"
        assert reconciler.reconcile(tenant_id="team-a") == 0
        assert state(value)[0]["status"] == "in_progress"
        assert reconciler.reconcile(tenant_id="team-b") == 1
    assert state(value)[0]["status"] == "verified"
    assert reconciler.reconcile(tenant_id="team-b") == 0
    with value.repository.transaction() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.active_agent_runs == 0 and usage.total_tokens == 10
        assert connection.execute(select(CAPACITY_RESERVATIONS.c.status)).scalar_one() == "released"
        kinds = connection.execute(select(PROJECT_PROCESS_WAKEUPS.c.source_event_type)).scalars().all()
        assert kinds.count("agent_run.completed") == 1
        assert kinds.count("team_task.submitted") == 1
        assert kinds.count("team_task.verified") == 1
    callback(value.runs.get(tenant_id="team-b", run_id=value.dispatched.run_id))
    assert reconciler.reconcile(tenant_id="team-b") == 0


def test_projection_crash_is_recovered_without_duplicate_accounting(tmp_path):
    value, reconciler = configured(tmp_path)
    finish(value, callback=None)
    projection = reconciler.task_reconciler.projection
    original = projection.project

    def unavailable(**kwargs):
        raise RuntimeError("temporary projection outage")

    projection.project = unavailable
    assert reconciler.reconcile(tenant_id="team-b") == 1
    assert state(value)[0]["status"] == "in_progress"
    with value.repository.transaction() as connection:
        assert value.repository.usage(connection, "process-a").total_tokens == 10
    projection.project = original
    assert reconciler.reconcile(tenant_id="team-b") == 1
    assert state(value)[0]["status"] == "verified"
    with value.repository.transaction() as connection:
        assert value.repository.usage(connection, "process-a").total_tokens == 10


def test_previous_callback_failure_does_not_skip_task_steps(tmp_path):
    value = setup(tmp_path, completed=False)
    attach_scheduler(value)

    def broken_previous(run):
        raise RuntimeError("other projection failed")

    value.dispatcher.run_service.terminal_callback = broken_previous
    configure_worker_reviews(settings=SimpleNamespace(artifact_store_root=str(tmp_path),
        artifact_max_upload_bytes=100000, sandbox_profiles_json=None), engine=value.engine,
        service=value.dispatcher.run_service, jobs=None, audit=value.capabilities.audit_log)
    callback = value.dispatcher.run_service.terminal_callback
    finish(value, callback=None)
    with pytest.raises(RuntimeError, match="other projection failed"):
        callback(value.runs.get(tenant_id="team-b", run_id=value.dispatched.run_id))
    assert state(value)[0]["status"] == "verified"


def test_recovery_does_not_select_active_runs(tmp_path):
    value, reconciler = configured(tmp_path)
    assert reconciler.reconcile(tenant_id="team-b") == 0
    assert state(value)[0]["status"] == "in_progress"


def test_unconfigured_artifact_reader_still_settles_usage_without_false_result(tmp_path):
    value = setup(tmp_path, completed=False)
    attach_scheduler(value)
    reconciler = configure_worker_reviews(settings=SimpleNamespace(artifact_store_root=None,
        sandbox_profiles_json=None), engine=value.engine, service=value.dispatcher.run_service,
        jobs=None, audit=value.capabilities.audit_log)
    finish(value, callback=None)
    assert reconciler.reconcile(tenant_id="team-b") == 1
    task, binding, _ = state(value)
    assert task["status"] == "in_progress" and binding["task_result_status"] is None
    with value.repository.transaction() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.active_agent_runs == 0 and usage.total_tokens == 10
