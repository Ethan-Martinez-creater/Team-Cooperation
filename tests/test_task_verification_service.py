import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from test_bootstrap import StubVerifier, production_settings
from test_team_task_result_projection import attach_scheduler, finish, prepare, state

from coifesp_harness.control_plane.app import create_app
from coifesp_harness.control_plane.verification_routes import (
    build_task_verification_router,
)
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    TEAM_TASKS,
)
from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
from coifesp_harness.project_process.scheduler import PROJECT_PROCESS_WAKEUPS
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.verification.repository import (
    TASK_VERIFICATIONS,
    VERIFICATION_METADATA,
)
from coifesp_harness.verification.service import TaskVerificationService


def policy(*criteria):
    return {
        "criteria": list(criteria)
        or [
            {
                "criterion_id": "hash",
                "type": "tool_check",
                "tool": "artifact.sha256",
                "required": True,
            }
        ]
    }


def setup(tmp_path, *, verification_policy=None, completed=True):
    value = prepare(tmp_path, verification_policy=verification_policy or policy())
    VERIFICATION_METADATA.create_all(value.engine)
    value.verifier = TaskVerificationService(
        repository=value.repository, artifact_content=value.content
    )
    if completed:
        finish(value)
        value.projection.project(run_id=value.dispatched.run_id)
    return value


def evidence(value):
    with value.engine.connect() as connection:
        return connection.execute(select(TASK_VERIFICATIONS)).mappings().all()


def verified_events(value):
    with value.engine.connect() as connection:
        return (
            connection.execute(
                select(PROJECT_PROCESS_EVENTS).where(
                    PROJECT_PROCESS_EVENTS.c.event_type == "team_task.verified",
                )
            )
            .mappings()
            .all()
        )


def verify(value):
    return value.verifier.verify_run(run_id=value.dispatched.run_id)


def test_real_worker_submits_pinned_artifacts_then_verifies_and_wakes_once(tmp_path):
    value = setup(tmp_path, completed=False)
    attach_scheduler(value)
    accounting = TeamTaskRunAccounting(
        repository=value.repository,
        run_repository=value.runs,
        capability_repository=value.capabilities,
    )

    def terminal(run):
        accounting.on_run_terminal(run)
        value.projection.on_run_terminal(run)
        value.verifier.on_run_terminal(run)

    finish(value, callback=terminal)
    task, binding, _ = state(value)
    assert task["status"] == "verified" and task["completed_at"] is not None
    assert binding["task_result_json"]["artifact_manifests"][0]["sha256"]
    row = evidence(value)[0]
    assert row["status"] == "PASS" and row["contract_version"] == 1
    assert row["executed_as"] == "service:project-verifier"
    assert row["source_run_id"] == value.dispatched.run_id
    assert len(verified_events(value)) == 1
    assert "private team note" not in str(row)
    assert "team-only detail" not in str(verified_events(value))
    verify(value)
    assert len(evidence(value)) == 1 and len(verified_events(value)) == 1
    assert value.verifier.replay_pending() == 0
    with value.engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(PROJECT_PROCESS_WAKEUPS)
                .where(
                    PROJECT_PROCESS_WAKEUPS.c.source_event_type == "team_task.verified",
                )
            ).scalar_one()
            == 1
        )
        assert value.repository.process(connection, "process-a").status.value != "COMPLETED"


@pytest.mark.parametrize(
    "criterion",
    [
        {"criterion_id": "review", "type": "agent_review", "required": True},
        {"criterion_id": "tests", "type": "tool_check", "tool": "pytest", "required": True},
    ],
)
def test_unavailable_required_verification_stays_pending(tmp_path, criterion):
    value = setup(tmp_path, verification_policy=policy(criterion))
    first = verify(value)
    assert first["status"] == "PENDING" and first["completed_at"] is None
    assert state(value)[0]["status"] == "submitted"
    assert not verified_events(value)
    assert verify(value) == first  # unchanged pending does not churn versions


def test_optional_unavailable_review_does_not_replace_required_integrity(tmp_path):
    value = setup(
        tmp_path,
        verification_policy=policy(
            {"criterion_id": "review", "type": "agent_review", "required": False},
        ),
    )
    result = verify(value)
    assert result["status"] == "PASS"
    assert any(check["status"] == "PENDING" and not check["required"] for check in result["checks"])


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_sha256": "f" * 64},
        {"propagation": "team_private"},
        {"owner_team_id": "team-a"},
        {"artifact_id": "replacement"},
    ],
)
def test_changed_or_revoked_resource_cannot_satisfy_pinned_submission(tmp_path, changes):
    value = setup(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_RESOURCES.update().values(**changes))
    assert verify(value)["status"] == "FAIL"
    assert state(value)[0]["status"] == "changes_requested"
    assert evidence(value)[0]["artifacts_json"][0]["artifact_id"] == "artifact-input"
    assert not verified_events(value)


def test_corrupted_physical_bytes_fail_independent_verification(tmp_path):
    value = setup(tmp_path)

    class CorruptReader:
        def open_policy_authorized(self, **_):
            yield b"wrong bytes"

    value.verifier.artifact_content = CorruptReader()
    assert verify(value)["status"] == "FAIL"
    assert state(value)[0]["status"] == "changes_requested"


def test_missing_reader_can_recover_pending_evidence_same_id(tmp_path):
    value = setup(tmp_path)
    value.verifier.artifact_content = None
    first = verify(value)
    assert first["status"] == "PENDING"
    value.verifier.artifact_content = value.content
    assert value.verifier.replay_pending() == 1
    final = evidence(value)[0]
    assert final["verification_id"] == first["verification_id"]
    assert final["status"] == "PASS" and final["version"] == 2
    assert state(value)[0]["status"] == "verified"


@pytest.mark.parametrize("error", [RuntimeError("store offline"), OSError("temporary IO")])
def test_transient_reader_failure_leaves_no_false_failure_evidence(tmp_path, error):
    value = setup(tmp_path)

    class Offline:
        def open_policy_authorized(self, **_):
            raise error

    value.verifier.artifact_content = Offline()
    with pytest.raises(type(error)):
        verify(value)
    assert not evidence(value)
    assert state(value)[0]["status"] == "submitted"
    value.verifier.artifact_content = value.content
    assert value.verifier.replay_pending() == 1


def test_transaction_failure_rolls_back_evidence_task_event_and_wakeup(tmp_path):
    value = setup(tmp_path)
    attach_scheduler(value)
    enqueue = value.repository.event_listener

    def crash(connection, event):
        enqueue(connection, event)
        raise RuntimeError("crash before commit")

    value.repository.set_event_listener(crash)
    with pytest.raises(RuntimeError, match="crash"):
        verify(value)
    assert not evidence(value) and not verified_events(value)
    assert state(value)[0]["status"] == "submitted"
    with value.engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(PROJECT_PROCESS_WAKEUPS)
            ).scalar_one()
            == 0
        )
    value.repository.set_event_listener(enqueue)
    assert value.verifier.replay_pending() == 1
    assert len(evidence(value)) == len(verified_events(value)) == 1


def test_old_pending_evidence_becomes_stale_when_contract_changes(tmp_path):
    value = setup(tmp_path)
    value.verifier.artifact_content = None
    first = verify(value)
    with value.engine.begin() as connection:
        connection.execute(
            TEAM_TASKS.update().values(
                source_contract_version=2,
                accepted_contract_version=2,
                verification_policy_json=policy(
                    {"criterion_id": "new", "type": "agent_review", "required": True}
                ),
            )
        )
    value.verifier.artifact_content = value.content
    assert value.verifier.replay_pending() == 1
    row = evidence(value)[0]
    assert row["verification_id"] == first["verification_id"]
    assert row["status"] == "STALE" and row["policy_json"] == policy()
    assert not verified_events(value) and state(value)[0]["source_contract_version"] == 2


def test_manual_rejection_invalidates_pending_verification(tmp_path):
    value = setup(tmp_path)
    value.verifier.artifact_content = None
    verify(value)
    TeamCollaborationService(value.engine).review_task(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-a",
        accept=False,
        note="rework",
    )
    assert value.verifier.replay_pending() == 1
    assert evidence(value)[0]["status"] == "STALE"
    assert state(value)[0]["status"] == "changes_requested"


def test_manual_approval_cannot_bypass_structured_contract(tmp_path):
    value = setup(tmp_path)
    with pytest.raises(GovernanceConflictError, match="verification evidence"):
        TeamCollaborationService(value.engine).review_task(
            project_id="project-a",
            task_id="task-a",
            actor_id="lead-a",
            accept=True,
            note="looks good",
        )
    assert state(value)[0]["status"] == "submitted"
    assert not evidence(value)


def test_historical_receipt_without_pinned_snapshot_is_not_inferred(tmp_path):
    value = setup(tmp_path)
    receipt = dict(state(value)[1]["task_result_json"])
    with value.engine.begin() as connection:
        receipt.pop("artifact_manifests")
        receipt.pop("verification_policy")
        connection.execute(PROJECT_AGENT_RUNS.update().values(task_result_json=receipt))
    first = verify(value)
    assert first["status"] == "PENDING"
    assert first["checks"][0]["code"] == "submission_snapshot_unavailable"
    assert verify(value) == first


def test_startup_replays_result_projection_before_verification(tmp_path):
    value = setup(tmp_path, completed=False)
    finish(value)
    assert state(value)[0]["status"] == "in_progress"
    app = create_app(
        settings=production_settings(),
        verifier=StubVerifier(),
        task_verification_service=value.verifier,
    )
    app.state.agent_run_service = value.dispatcher.run_service
    app.state.team_task_result_projection = value.projection

    async def startup():
        async with app.router.lifespan_context(app):
            assert state(value)[0]["status"] == "verified"

    asyncio.run(startup())
    assert len(evidence(value)) == 1


def test_http_runs_real_checks_and_lists_evidence_for_task_parties(tmp_path):
    value = setup(tmp_path)
    app = FastAPI()

    async def auth(request: Request):
        return SimpleNamespace(
            principal=SimpleNamespace(principal_id=request.headers["x-test-actor"])
        )

    app.include_router(build_task_verification_router(authenticator=auth, service=value.verifier))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/v1/projects/project-a/tasks/task-a"
            result = await client.post(path + ":verify", headers={"x-test-actor": "lead-a"})
            assert result.status_code == 200 and result.json()["status"] == "PASS"
            result = await client.get(path + "/verifications", headers={"x-test-actor": "lead-b"})
            assert result.status_code == 200 and len(result.json()) == 1
            assert "private team note" not in result.text

    asyncio.run(scenario())


def test_http_caller_cannot_inject_a_pass_for_unavailable_review(tmp_path):
    value = setup(
        tmp_path,
        verification_policy=policy(
            {"criterion_id": "review", "type": "agent_review", "required": True},
        ),
    )
    app = FastAPI()

    async def auth():
        return SimpleNamespace(principal=SimpleNamespace(principal_id="lead-a"))

    app.include_router(build_task_verification_router(authenticator=auth, service=value.verifier))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/projects/project-a/tasks/task-a:verify", json={"passed": True}
            )
            assert response.status_code == 200 and response.json()["status"] == "PENDING"

    asyncio.run(scenario())
    assert state(value)[0]["status"] == "submitted"


def test_unrelated_project_team_cannot_read_or_trigger_checks(tmp_path, monkeypatch):
    value = setup(tmp_path)
    monkeypatch.setattr(TeamCollaborationService, "_participant", lambda *_: {"team_id": "team-c"})
    for method in (value.verifier.verify_task, value.verifier.results):
        with pytest.raises(PolicyDenied):
            method(project_id="project-a", task_id="task-a", actor_id="other")
    assert not evidence(value)
