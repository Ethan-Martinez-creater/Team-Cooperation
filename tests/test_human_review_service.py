import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from test_agent_review_service import execute, prepared
from test_task_verification_service import policy, setup, verified_events, verify
from test_team_task_result_projection import attach_scheduler, state

from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.control_plane.verification_routes import (
    build_task_verification_router,
)
from coifesp_harness.errors import (
    GovernanceConflictError,
    PolicyDenied,
    ResourceNotFound,
)
from coifesp_harness.product.repository import ACCOUNTS, PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
from coifesp_harness.project_process.scheduler import PROJECT_PROCESS_WAKEUPS
from coifesp_harness.project_process.service import ProjectProcessService
from coifesp_harness.verification.repository import HUMAN_REVIEWS
from coifesp_harness.verification.service import TaskVerificationService

HUMAN = {"criterion_id": "business-acceptance", "type": "human_review", "required": True}
AGENT = {"criterion_id": "semantic", "type": "agent_review", "required": True}
HASH = {"criterion_id": "sha", "type": "tool_check", "tool": "artifact.sha256", "required": True}


def stack(tmp_path, criteria=None):
    value = setup(tmp_path, verification_policy=policy(*(criteria or [HUMAN])))
    verify(value)
    return value


def records(value):
    with value.engine.connect() as connection:
        return connection.execute(select(HUMAN_REVIEWS).order_by(
            HUMAN_REVIEWS.c.criterion_id)).mappings().all()


def decide(value, *, decision="ACCEPT", actor_id="lead-a", review_id=None, **overrides):
    row = records(value)[0]
    return value.verifier.decide_human_review(project_id="project-a", task_id="task-a",
        review_id=review_id or row["review_id"], actor_id=actor_id,
        decision={"decision": decision, "reason": "Checked the supplied deliverable",
                  "expected_version": 1, "idempotency_key": "human-decision-1", **overrides})


def test_human_review_is_fixed_durable_and_does_not_launch_an_agent(tmp_path):
    value = stack(tmp_path)
    original, = records(value)
    assert original["status"] == "OPEN" and original["reviewer_team_id"] == "team-a"
    assert original["source_run_id"] == value.dispatched.run_id
    value.verifier = TaskVerificationService(repository=value.repository, artifact_content=value.content)
    value.verifier.replay_pending()
    verify(value)
    row, = records(value)
    assert row["review_id"] == original["review_id"] and row["version"] == 1
    assert state(value)[0]["status"] == "submitted"
    with value.engine.connect() as connection:
        assert len(connection.execute(select(AGENT_RUNS)).all()) == 1


@pytest.mark.parametrize("decision,status", [("ACCEPT", "verified"), ("REJECT", "changes_requested")])
def test_human_decision_materializes_verification_and_task_once(tmp_path, decision, status):
    value = stack(tmp_path)
    result = decide(value, decision=decision)
    assert result["verification"]["status"] == ("PASS" if decision == "ACCEPT" else "FAIL")
    assert state(value)[0]["status"] == status
    replay = decide(value, decision=decision)
    assert replay == result
    value.verifier.replay_pending()
    assert records(value)[0]["version"] == 2
    assert len(verified_events(value)) == int(decision == "ACCEPT")
    assert "Checked the supplied" not in str(verified_events(value))


def test_composite_requires_tools_agent_and_human_not_just_one_pass(tmp_path):
    value = prepared(tmp_path, criteria=[HUMAN, HASH, AGENT])
    assert verify(value)["status"] == "PENDING"
    assert not records(value)
    execute(value)
    row, = records(value)
    assert row["status"] == "OPEN" and state(value)[0]["status"] == "submitted"
    assert decide(value)["verification"]["status"] == "PASS"
    assert state(value)[0]["status"] == "verified"


def test_agent_failure_never_offers_human_override(tmp_path):
    from test_agent_review_service import result

    value = prepared(tmp_path, criteria=[HUMAN, AGENT])
    verify(value)
    execute(value, result(False))
    assert not records(value)
    assert state(value)[0]["status"] == "changes_requested"


def test_unavailable_tool_never_offers_human_override(tmp_path):
    value = stack(tmp_path, [HUMAN, {**HASH, "tool": "unsupported.check"}])
    assert not records(value)
    assert verify(value)["status"] == "PENDING"


def test_optional_human_is_not_requested_and_does_not_block(tmp_path):
    value = stack(tmp_path, [{**HUMAN, "required": False}, HASH])
    assert not records(value)
    assert state(value)[0]["status"] == "verified"


def test_every_required_human_criterion_must_be_accepted(tmp_path):
    value = stack(tmp_path, [HUMAN, {**HUMAN, "criterion_id": "usability"}])
    assert len(records(value)) == 2
    assert decide(value)["verification"]["status"] == "PENDING"
    assert state(value)[0]["status"] == "submitted"
    last = records(value)[1]
    assert decide(value, review_id=last["review_id"])["verification"]["status"] == "PASS"


def test_rejection_closes_other_open_review_without_fabricating_decisions(tmp_path):
    value = stack(tmp_path, [HUMAN, {**HUMAN, "criterion_id": "usability"}])
    decide(value, decision="REJECT")
    first, other = records(value)
    assert first["status"] == "REJECTED" and other["status"] == "STALE"
    assert other["decision"] is None and other["decided_by"] is None


@pytest.mark.parametrize("mutation", ["version", "task", "resource"])
def test_stale_subject_is_rejected_and_replay_closes_review(tmp_path, mutation):
    value = stack(tmp_path)
    with value.engine.begin() as connection:
        if mutation == "version":
            connection.execute(TEAM_TASKS.update().values(source_contract_version=2, accepted_contract_version=2))
        elif mutation == "task":
            connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
        else:
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
    with pytest.raises(GovernanceConflictError):
        decide(value)
    value.verifier.replay_pending()
    row, = records(value)
    assert row["status"] == "STALE" and row["decision"] is None
    assert not verified_events(value)


def test_disabled_and_executing_team_cannot_accept_their_own_work(tmp_path):
    value = stack(tmp_path)
    with pytest.raises(PolicyDenied):
        decide(value, actor_id="lead-b")
    with value.engine.begin() as connection:
        connection.execute(ACCOUNTS.update().where(ACCOUNTS.c.account_id == "lead-a").values(enabled=False))
    with pytest.raises(PolicyDenied):
        decide(value)
    assert records(value)[0]["status"] == "OPEN"


def test_wrong_project_or_unknown_human_identity_cannot_decide(tmp_path):
    value = stack(tmp_path)
    with pytest.raises(ResourceNotFound):
        decide(value, actor_id="service:project-orchestrator")
    with pytest.raises(ResourceNotFound):
        value.verifier.decide_human_review(project_id="other", task_id="task-a", actor_id="lead-a",
            review_id=records(value)[0]["review_id"], decision={"decision": "ACCEPT", "reason": "checked",
                "idempotency_key": "key", "expected_version": 1})


def test_idempotency_conflicts_and_optimistic_versions_are_enforced(tmp_path):
    value = stack(tmp_path)
    with pytest.raises(GovernanceConflictError):
        decide(value, expected_version=2)
    decide(value)
    with pytest.raises(GovernanceConflictError):
        decide(value, reason="different reason")
    with pytest.raises(GovernanceConflictError):
        decide(value, idempotency_key="different-key")
    assert len(verified_events(value)) == 1


def test_projection_failure_rolls_back_decision_and_task_state(tmp_path, monkeypatch):
    value = stack(tmp_path)
    original = ProjectProcessService.append_fact

    def fail(self, **kwargs):
        if kwargs["event_type"] == "team_task.verified":
            raise RuntimeError("injected outbox failure")
        return original(self, **kwargs)

    monkeypatch.setattr(ProjectProcessService, "append_fact", fail)
    with pytest.raises(RuntimeError, match="outbox failure"):
        decide(value)
    assert records(value)[0]["status"] == "OPEN"
    assert state(value)[0]["status"] == "submitted"
    assert not verified_events(value)
    monkeypatch.setattr(ProjectProcessService, "append_fact", original)
    assert decide(value)["verification"]["status"] == "PASS"


def test_list_visible_to_task_parties_but_not_unrelated_team(tmp_path):
    value = stack(tmp_path)
    listed = value.verifier.human_reviews(project_id="project-a", task_id="task-a", actor_id="lead-b")
    assert listed[0]["artifacts"][0]["resource_id"] == "resource-input"
    assert "private team note" not in str(listed)
    with pytest.raises(ResourceNotFound):
        value.verifier.human_reviews(project_id="project-a", task_id="task-a", actor_id="lead-c")


def test_http_human_review_rejects_machine_decisions_and_forged_fields(tmp_path):
    value = stack(tmp_path)
    app = FastAPI()

    @app.exception_handler(PolicyDenied)
    async def denied(_request, _exc):
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    async def auth(request: Request):
        return SimpleNamespace(principal=SimpleNamespace(principal_id=request.headers.get("x-actor", "lead-a"),
            is_service=request.headers.get("x-service") == "true"))

    app.include_router(build_task_verification_router(authenticator=auth, service=value.verifier))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            root = "/v1/projects/project-a/tasks/task-a/human-reviews"
            response = await client.get(root)
            assert response.status_code == 200
            path = root + "/" + response.json()[0]["review_id"] + ":decide"
            body = {"decision": "ACCEPT", "reason": "Meets requirements", "idempotency_key": "http-1", "expected_version": 1}
            assert (await client.post(path, json={**body, "passed": True})).status_code == 422
            assert (await client.post(path, json=body, headers={"x-service": "true"})).status_code == 403
            assert (await client.post(path, json=body, headers={"x-actor": "lead-b"})).status_code == 403
            accepted = await client.post(path, json=body)
            assert accepted.status_code == 200 and accepted.json()["verification"]["status"] == "PASS"

    asyncio.run(scenario())


def test_human_facts_and_wakeup_are_atomic_and_do_not_change_process_triple(tmp_path):
    value = setup(tmp_path, verification_policy=policy(HUMAN))
    attach_scheduler(value)
    with value.engine.connect() as connection:
        before = value.repository.process(connection, "process-a")
    verify(value)
    decide(value)
    decide(value)
    with value.engine.connect() as connection:
        after = value.repository.process(connection, "process-a")
        facts = connection.execute(select(PROJECT_PROCESS_EVENTS).where(
            PROJECT_PROCESS_EVENTS.c.event_type.like("task_verification.human_review.%")
        )).mappings().all()
        wakeups = connection.execute(select(PROJECT_PROCESS_WAKEUPS).where(
            PROJECT_PROCESS_WAKEUPS.c.source_event_type.like("task_verification.human_review.%")
        )).mappings().all()
    assert {f["event_type"] for f in facts} == {
        "task_verification.human_review.opened", "task_verification.human_review.decided"}
    assert len(facts) == len(wakeups) == 2
    assert before.version == after.version
    assert (before.phase, before.status, before.wait_reason) == (after.phase, after.status, after.wait_reason)
    assert after.last_event_sequence == before.last_event_sequence + 3
    assert "Checked the supplied" not in str(facts)


def test_human_request_event_failure_rolls_back_request_and_verification(tmp_path, monkeypatch):
    value = setup(tmp_path, verification_policy=policy(HUMAN))
    original = ProjectProcessService.append_fact

    def fail(self, **kwargs):
        if kwargs["event_type"] == "task_verification.human_review.opened":
            raise RuntimeError("injected request outbox failure")
        return original(self, **kwargs)

    monkeypatch.setattr(ProjectProcessService, "append_fact", fail)
    with pytest.raises(RuntimeError, match="outbox failure"):
        verify(value)
    assert not records(value)
    assert state(value)[0]["status"] == "submitted"
    monkeypatch.setattr(ProjectProcessService, "append_fact", original)
    verify(value)
    assert len(records(value)) == 1
