import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from test_agent_run_worker import Provider
from test_agent_run_worker import stack as worker_stack
from test_task_verification_service import policy, setup, verified_events, verify
from test_team_task_result_projection import state

from coifesp_harness.agent_runs import (
    AgentRunCheckpointCodec,
    DurableAgentWorker,
    DurableAgentWorkerRunner,
)
from coifesp_harness.agent_runs.repository import AGENT_RUNS
from coifesp_harness.control_plane.verification_routes import (
    build_task_verification_router,
)
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product.repository import PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.project_process.repository import PROJECT_EXECUTION_POLICIES
from coifesp_harness.runtime import LLMResponse
from coifesp_harness.security import Principal
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.identity import TeamAgentPrincipalResolver
from coifesp_harness.verification.agent_reviews import AgentReviewChecks
from coifesp_harness.verification.repository import AGENT_REVIEWS
from coifesp_harness.verification.worker_runtime import (
    AgentReviewReconciler,
    configure_worker_reviews,
)


def prepared(tmp_path, *, criteria=None):
    value = setup(tmp_path, verification_policy=policy(*(criteria or [
        {"criterion_id": "semantic", "type": "agent_review", "required": True},
    ])))
    TeamTaskRunAccounting(repository=value.repository, run_repository=value.runs,
                          capability_repository=value.capabilities).settle(
        run_id=value.dispatched.run_id)
    value.reviews = AgentReviewChecks(repository=value.repository, runs=value.runs,
                                     artifact_content=value.content)
    value.verifier.review_checks = value.reviews
    return value


def reviews(value):
    with value.engine.connect() as connection:
        return connection.execute(select(AGENT_REVIEWS).order_by(
            AGENT_REVIEWS.c.attempt)).mappings().all()


def result(passed=True, **changes):
    return json.dumps({"schema": "coifesp.verification-result.v1", "passed": passed,
                       "findings": [] if passed else ["Missing design rationale"],
                       "required_changes": [] if passed else ["Document design rationale"],
                       "evidence_refs": ["resource-input"], **changes})


class NoHuman:
    async def resolve(self, **_):
        raise AssertionError("independent review must not borrow a human identity")


def execute(value, text=None, *, callback=True):
    _, _, loop = worker_stack(provider=Provider(response=LLMResponse(
        text=result() if text is None else text, input_tokens=11, output_tokens=7)))
    value.dispatcher.run_service.terminal_callback = value.verifier.on_run_terminal if callback else None
    worker = DurableAgentWorker(service=value.dispatcher.run_service, loop=loop,
        principal_resolver=TeamAgentPrincipalResolver(engine=value.engine, human_resolver=NoHuman()))
    outcome = asyncio.run(worker.process_once(worker=Principal(
        "worker-review", "team-a", roles=frozenset({"agent_worker"}), is_service=True)))
    assert outcome.status.value == "completed"
    return outcome


def retry(value):
    return value.verifier.verify_task(project_id="project-a", task_id="task-a",
                                      actor_id="lead-a", retry_reviews=True)


def test_independent_run_fixed_shared_input_and_idempotent_budget(tmp_path):
    value = prepared(tmp_path)
    assert verify(value)["status"] == "PENDING"
    verify(value)
    row, = reviews(value)
    assert row["run_id"] != value.dispatched.run_id
    assert row["initiated_by"] == row["executed_as"] == "service:project-orchestrator"
    assert row["owner_team_id"] == "team-a"
    checkpoint = AgentRunCheckpointCodec().decode(value.runs.load_checkpoint(
        tenant_id="team-a", run_id=row["run_id"]))
    contents = str(checkpoint["messages"])
    assert "contracted project input" in contents
    assert "private team note" not in contents and "team-only detail" not in contents
    assert checkpoint["context_items"] == ()
    assert not checkpoint["tool_authorization"].allowed_tools
    with value.engine.connect() as connection:
        usage = value.repository.usage(connection, "process-a")
    assert usage.agent_runs_started == 2 and usage.active_agent_runs == 1


@pytest.mark.parametrize("passed", [True, False])
def test_real_worker_projects_review_verdict_and_accounts_once(tmp_path, passed):
    value = prepared(tmp_path)
    verify(value)
    execute(value, result(passed))
    row, = reviews(value)
    assert row["status"] == ("PASS" if passed else "FAIL")
    assert state(value)[0]["status"] == ("verified" if passed else "changes_requested")
    value.verifier.replay_pending()
    verify(value)
    assert len(verified_events(value)) == int(passed)
    with value.engine.connect() as connection:
        usage = value.repository.usage(connection, "process-a")
    assert usage.active_agent_runs == 0 and usage.agent_runs_completed == 2
    assert usage.total_tokens == 28


@pytest.mark.parametrize("text", ["Looks good", "{}", result(evidence_refs=["private-resource"])])
def test_invalid_review_stays_pending_until_explicit_retry(tmp_path, text):
    value = prepared(tmp_path)
    verify(value)
    execute(value, text)
    assert reviews(value)[0]["status"] == "UNAVAILABLE"
    assert state(value)[0]["status"] == "submitted"
    verify(value)
    assert len(reviews(value)) == 1
    retry(value)
    assert len(reviews(value)) == 2
    execute(value)
    assert state(value)[0]["status"] == "verified"


def test_terminal_callback_loss_and_partial_projection_are_replayed(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    execute(value, callback=False)
    row, = reviews(value)
    assert row["status"] == "QUEUED"
    value.reviews.project_terminal(row["run_id"])
    assert state(value)[0]["status"] == "submitted"
    value.verifier.replay_pending()
    value.verifier.replay_pending()
    assert state(value)[0]["status"] == "verified"
    assert len(verified_events(value)) == 1


def test_opportunistic_projection_uses_current_event_sequence(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    execute(value, callback=False)
    assert verify(value)["status"] == "PASS"
    assert len(verified_events(value)) == 1


def test_review_does_not_approve_changed_contract(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    execute(value, callback=False)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(source_contract_version=2,
                                                      accepted_contract_version=2))
    assert verify(value)["status"] == "STALE"
    value.verifier.replay_pending()
    assert reviews(value)[0]["status"] == "STALE"
    assert not verified_events(value)


def test_unavailable_review_retries_are_bounded_and_require_actor(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    for attempt in range(3):
        execute(value, "invalid")
        if attempt < 2:
            retry(value)
    with pytest.raises(GovernanceConflictError):
        retry(value)
    with pytest.raises((ValueError, PolicyDenied)):
        value.verifier.verify_run(run_id=value.dispatched.run_id, retry_reviews=True)
    assert len(reviews(value)) == 3


def test_no_review_before_required_tool_and_no_optional_run(tmp_path):
    for index, criteria in enumerate([
        [{"criterion_id": "semantic", "type": "agent_review", "required": False}],
        [{"criterion_id": "semantic", "type": "agent_review", "required": True},
         {"criterion_id": "test", "type": "tool_check", "tool": "project.tests", "required": True}],
    ]):
        directory = tmp_path / str(index)
        directory.mkdir()
        value = prepared(directory, criteria=criteria)
        verify(value)
        assert not reviews(value)


def test_budget_rejection_does_not_create_review_or_reservation(tmp_path):
    value = prepared(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_EXECUTION_POLICIES.update().values(max_agent_runs=1))
    outcome = verify(value)
    assert outcome["status"] == "PENDING"
    assert outcome["checks"][-1]["code"] == "review_budget_unavailable"
    assert not reviews(value)


def test_review_identity_binding_cannot_be_reused_across_tenants(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    row, = reviews(value)
    resolver = TeamAgentPrincipalResolver(engine=value.engine, human_resolver=NoHuman())
    with pytest.raises(PolicyDenied):
        asyncio.run(resolver.resolve_for_run(tenant_id="team-b",
            principal_id="service:project-orchestrator", run_id=row["run_id"]))


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "completed"])
def test_terminal_without_valid_reply_releases_budget_and_can_retry(tmp_path, terminal_status):
    value = prepared(tmp_path)
    verify(value)
    row, = reviews(value)
    with value.engine.begin() as connection:
        connection.execute(AGENT_RUNS.update().where(AGENT_RUNS.c.run_id == row["run_id"])
                           .values(status=terminal_status, completed_at=datetime.now(UTC)))
    value.verifier.replay_pending()
    assert reviews(value)[0]["status"] == "UNAVAILABLE"
    assert state(value)[0]["status"] == "submitted"
    with value.engine.connect() as connection:
        assert value.repository.usage(connection, "process-a").active_agent_runs == 0
    retry(value)
    assert reviews(value)[-1]["status"] == "QUEUED"


def test_dispatch_failure_rolls_back_budget_run_and_review(tmp_path, monkeypatch):
    from coifesp_harness.verification.agent_reviews import AgentRunService

    value = prepared(tmp_path)

    def fail(self, **_):
        raise RuntimeError("injected storage interruption")

    monkeypatch.setattr(AgentRunService, "create", fail)
    with pytest.raises(RuntimeError, match="storage interruption"):
        verify(value)
    assert not reviews(value)
    with value.engine.connect() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.active_agent_runs == 0 and usage.agent_runs_started == 1
        assert len(connection.execute(select(AGENT_RUNS)).all()) == 1


def test_oversized_input_is_not_silently_truncated_or_reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr("coifesp_harness.verification.agent_reviews.MAX_INPUT_BYTES", 1)
    value = prepared(tmp_path)
    outcome = verify(value)
    assert outcome["status"] == "PENDING"
    assert outcome["checks"][-1]["code"] == "review_input_unavailable"
    assert not reviews(value)


def test_http_retry_queues_run_but_never_accepts_caller_verdict(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    execute(value, "invalid")
    app = FastAPI()

    async def auth():
        return SimpleNamespace(principal=SimpleNamespace(principal_id="lead-a"))

    app.include_router(build_task_verification_router(authenticator=auth, service=value.verifier))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/projects/project-a/tasks/task-a:retry-agent-review",
                                         json={"passed": True, "status": "PASS"})
            assert response.status_code == 200 and response.json()["status"] == "PENDING"

    asyncio.run(scenario())
    assert reviews(value)[-1]["status"] == "QUEUED"
    assert state(value)[0]["status"] == "submitted"


def test_worker_runtime_wiring_and_tenant_scoped_recovery(tmp_path):
    value = prepared(tmp_path)
    from test_team_task_result_projection import attach_scheduler

    attach_scheduler(value)
    verify(value)
    execute(value, callback=False)
    settings = SimpleNamespace(artifact_store_root=str(tmp_path), artifact_max_upload_bytes=100000,
                               sandbox_profiles_json=None)
    reconciler = configure_worker_reviews(settings=settings, engine=value.engine,
        service=value.dispatcher.run_service, jobs=None, audit=value.capabilities.audit_log)
    assert reconciler is not None
    assert reconciler.reconcile(tenant_id="team-b") == 0
    assert reviews(value)[0]["status"] == "QUEUED"
    assert reconciler.reconcile(tenant_id="team-a") == 1
    assert state(value)[0]["status"] == "verified"
    assert reconciler.reconcile(tenant_id="team-a") == 0
    # The installed independent-process callback remains idempotent.
    row, = reviews(value)
    value.dispatcher.run_service.terminal_callback(value.runs.get(
        tenant_id="team-a", run_id=row["run_id"]))
    assert len(verified_events(value)) == 1


@pytest.mark.parametrize("transient_failure", [False, True])
def test_agent_runner_reconciles_even_when_terminal_callback_was_lost(tmp_path, transient_failure):
    value = prepared(tmp_path)
    verify(value)
    _, _, loop = worker_stack(provider=Provider(response=LLMResponse(
        text=result(), input_tokens=11, output_tokens=7)))
    value.dispatcher.run_service.terminal_callback = None
    worker = DurableAgentWorker(service=value.dispatcher.run_service, loop=loop,
        principal_resolver=TeamAgentPrincipalResolver(engine=value.engine, human_resolver=NoHuman()))

    async def scenario():
        stop = asyncio.Event()

        class Identity:
            async def resolve(self):
                return Principal("worker-review", "team-a", roles=frozenset({"agent_worker"}), is_service=True)

        class Once:
            async def process_once(self, **kwargs):
                outcome = await worker.process_once(**kwargs)
                stop.set()
                return outcome

        class Reconciler(AgentReviewReconciler):
            calls = 0

            def reconcile(self, **kwargs):
                self.calls += 1
                if transient_failure and self.calls == 1:
                    raise RuntimeError("temporary recovery outage")
                return super().reconcile(**kwargs)

        await DurableAgentWorkerRunner(worker=Once(), identity_provider=Identity(),
            terminal_reconciler=Reconciler(value.verifier)).run(stop=stop)

    asyncio.run(scenario())
    assert state(value)[0]["status"] == "verified"


def test_recovery_rotates_past_unavailable_reviews(tmp_path):
    value = prepared(tmp_path, criteria=[
        {"criterion_id": "one", "type": "agent_review", "required": True},
        {"criterion_id": "two", "type": "agent_review", "required": True},
    ])
    verify(value)
    rows = sorted(reviews(value), key=lambda row: row["run_id"])
    visited = []

    class Reviews:
        def project_terminal(self, run_id):
            visited.append(run_id)
            raise RuntimeError("temporarily unavailable")

    value.verifier.review_checks = Reviews()
    reconciler = AgentReviewReconciler(value.verifier, batch_size=1)
    for _ in range(3):
        assert reconciler.reconcile(tenant_id="team-a") == 1
    assert visited == [rows[0]["run_id"], rows[1]["run_id"], rows[0]["run_id"]]


def test_terminal_review_checks_current_contract_before_parent_projection(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    execute(value, callback=False)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(source_contract_version=2,
                                                      accepted_contract_version=2))
    row, = reviews(value)
    value.reviews.project_terminal(row["run_id"])
    assert reviews(value)[0]["status"] == "STALE"
    assert verify(value)["status"] == "STALE"
    assert not verified_events(value)


@pytest.mark.parametrize("withdrawal", ["task", "resource"])
def test_withdrawal_cancels_unstarted_review_and_releases_budget(tmp_path, withdrawal):
    value = prepared(tmp_path)
    verify(value)
    row, = reviews(value)
    with value.engine.begin() as connection:
        if withdrawal == "task":
            connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
        else:
            connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
    assert verify(value)["status"] in {"STALE", "FAIL"}
    assert reviews(value)[0]["status"] == "STALE"
    assert value.runs.get(tenant_id="team-a", run_id=row["run_id"]).status.value == "cancelled"
    value.verifier.replay_pending()
    with value.engine.connect() as connection:
        usage = value.repository.usage(connection, "process-a")
        assert usage.active_agent_runs == 0
        assert usage.total_tokens == 10


def test_inflight_review_is_not_cancelled_before_actual_usage_is_known(tmp_path):
    value = prepared(tmp_path)
    verify(value)
    row, = reviews(value)
    worker = Principal("worker-review", "team-a", roles=frozenset({"agent_worker"}), is_service=True)
    lease = value.dispatcher.run_service.claim(worker=worker)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
    assert verify(value)["status"] == "STALE"
    assert reviews(value)[0]["status"] == "QUEUED"
    assert value.runs.get(tenant_id="team-a", run_id=row["run_id"]).status.value == "leased"
    with value.engine.connect() as connection:
        assert value.repository.usage(connection, "process-a").active_agent_runs == 1
    value.runs.abort(tenant_id="team-a", run_id=row["run_id"], worker_id=worker.principal_id,
                     lease_token=lease.lease_token, error_code="submission_changed")
    value.verifier.replay_pending()
    assert reviews(value)[0]["status"] == "STALE"
    with value.engine.connect() as connection:
        assert value.repository.usage(connection, "process-a").active_agent_runs == 0


def test_review_reference_limit_blocks_dispatch_without_consuming_budget(tmp_path):
    value = prepared(tmp_path)
    from coifesp_harness.verification.agent_reviews import ReviewInputUnavailable

    with pytest.raises(ReviewInputUnavailable):
        value.reviews._input({}, [{}] * 33, "review")
