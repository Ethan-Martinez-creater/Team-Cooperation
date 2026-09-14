import asyncio

import pytest
from sqlalchemy import select
from test_task_verification_service import setup, state, verified_events, verify

from coifesp_harness.artifacts import SQLAlchemyArtifactRepository
from coifesp_harness.connectors import ConnectorResponse, GitHubTools
from coifesp_harness.product.repository import PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.security import Classification
from coifesp_harness.team_agents.task_contract_models import _verification_policy
from coifesp_harness.tool_jobs import (
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolJobKeyring,
)
from coifesp_harness.tools import ToolRegistry
from coifesp_harness.verification.repository import TASK_VERIFICATIONS
from coifesp_harness.verification.service import TaskVerificationService
from coifesp_harness.verification.tool_checks import DurableVerificationChecks


def policy():
    return {"criteria": [{"criterion_id": "ci", "type": "tool_check", "required": True,
                         "tool": "github.get_commit_checks", "github": {
                             "connector_id": "github-main", "repository": "owner/repo",
                             "commit_sha": "a" * 40, "required_checks": ["pytest"],
                         }}]}


def prepare(tmp_path):
    value = setup(tmp_path, verification_policy=policy())
    value.content.repository = SQLAlchemyArtifactRepository(
        engine=value.engine, audit_log=value.capabilities.audit_log,
    )
    value.content.repository.create_schema()
    value.jobs = SQLAlchemyToolJobRepository(
        engine=value.engine, keyring=ToolJobKeyring(master_key=b"t" * 32, key_id="test"),
    )
    value.jobs.create_schema()
    value.verifier.tool_checks = DurableVerificationChecks(jobs=value.jobs, profiles=())
    return value


def execute(value, *, conclusion="success", complete=True, wrong_sha=False):
    class Client:
        async def execute(self, request):
            return ConnectorResponse(200, {
                "repository": "owner/repo", "commit_sha": ("b" if wrong_sha else "a") * 40,
                "complete": complete, "checks": [{"id": 1, "name": "pytest",
                    "status": "completed", "conclusion": conclusion}],
            })

    registry = ToolRegistry()
    for definition in GitHubTools(client=Client(), tenant_id="team-b",
                                  classification=Classification.INTERNAL).definitions():
        registry.register(definition)
    worker = DurableToolWorker(repository=value.jobs, registry=registry, tenant_id="team-b",
                               worker_id="worker", lease_seconds=30, heartbeat_seconds=5)
    assert asyncio.run(worker.run_once())


def test_github_evidence_verifies_submission_and_replays_once(tmp_path):
    value = prepare(tmp_path)
    first = verify(value)
    assert first["status"] == "PENDING"
    assert verify(value) == first
    execute(value)
    # A new verifier reads durable evidence after worker completion / process restart.
    value.verifier = TaskVerificationService(
        repository=value.repository, artifact_content=value.content,
        tool_checks=DurableVerificationChecks(jobs=value.jobs, profiles=()),
    )
    value.verifier.replay_pending()
    result = verify(value)
    assert result["status"] == "PASS"
    assert state(value)[0]["status"] == "verified"
    assert len(result["checks"][1]["receipt_digest"]) == 64
    assert len(verified_events(value)) == 1
    verify(value)
    assert len(verified_events(value)) == 1
    with value.engine.connect() as connection:
        resources = connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.resource_id.like("github-evidence:%"),
        )).mappings().all()
    assert len(resources) == 1
    resource = resources[0]
    assert resource["propagation"] == "team_private"
    assert resource["owner_team_id"] == "team-b"
    assert resource["source_run_id"] == value.dispatched.run_id
    payload = b"".join(value.content.open_policy_authorized(
        owner_tenant_id="team-b", sha256=resource["artifact_sha256"],
    ))
    assert b'"status":"PASS"' in payload
    assert b"owner/repo" not in payload
    assert resource["resource_id"] not in str(result)


@pytest.mark.parametrize("options,status,task_status", [
    ({"conclusion": "failure"}, "FAIL", "changes_requested"),
    ({"complete": False}, "PENDING", "submitted"),
    ({"wrong_sha": True}, "PENDING", "submitted"),
])
def test_github_failure_and_incomplete_evidence_are_not_success(tmp_path, options, status, task_status):
    value = prepare(tmp_path)
    verify(value)
    execute(value, **options)
    assert verify(value)["status"] == status
    assert state(value)[0]["status"] == task_status
    assert not verified_events(value)


def test_new_query_requires_explicit_retry_and_retains_old_evidence(tmp_path):
    value = prepare(tmp_path)
    first = verify(value)
    execute(value, complete=False)
    verify(value)
    assert len(value.jobs.list_for_run(tenant_id="team-b", run_id=value.dispatched.run_id)) == 1
    retry = value.verifier.verify_task(
        project_id="project-a", task_id="task-a", actor_id="lead-a", retry_tools=True,
    )
    assert retry["checks"][1]["tool_attempt"] == 2
    assert "receipt_digest" not in retry["checks"][1]
    assert retry["checks"][1]["superseded_tool_job_ids"] == [first["checks"][1]["tool_job_id"]]
    execute(value)
    assert verify(value)["status"] == "PASS"


def test_changed_task_cannot_use_old_github_pass(tmp_path):
    value = prepare(tmp_path)
    verify(value)
    execute(value)
    with value.engine.begin() as connection:
        connection.execute(TEAM_TASKS.update().values(status="changes_requested"))
    value.verifier.replay_pending()
    with value.engine.connect() as connection:
        assert connection.execute(select(TASK_VERIFICATIONS.c.status)).scalar_one() == "STALE"
    assert not verified_events(value)


def test_evidence_publication_failure_keeps_verification_retryable(tmp_path, monkeypatch):
    value = prepare(tmp_path)
    verify(value)
    execute(value)
    persist = value.content.repository._persist_publication

    def crash(*args, **kwargs):
        persist(*args, **kwargs)
        raise RuntimeError("simulated publication crash")

    monkeypatch.setattr(value.content.repository, "_persist_publication", crash)
    with pytest.raises(RuntimeError, match="publication crash"):
        verify(value)
    with value.engine.connect() as connection:
        assert connection.execute(select(TASK_VERIFICATIONS.c.status)).scalar_one() == "PENDING"
        assert not connection.execute(select(PROJECT_RESOURCES.c.resource_id).where(
            PROJECT_RESOURCES.c.resource_id.like("github-evidence:%"),
        )).all()
    assert state(value)[0]["status"] == "submitted"
    monkeypatch.setattr(value.content.repository, "_persist_publication", persist)
    assert verify(value)["status"] == "PASS"
    assert len(verified_events(value)) == 1


@pytest.mark.parametrize("overrides", [
    {"commit_sha": "main"}, {"required_checks": []}, {"required_checks": ["pytest", "pytest"]},
    {"repository": "https://github.com/owner/repo"},
])
def test_github_contract_rejects_unbound_subject(overrides):
    value = policy()
    value["criteria"][0]["github"].update(overrides)
    with pytest.raises(ValueError):
        _verification_policy(value)
