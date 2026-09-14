import asyncio

import pytest
from sqlalchemy import create_engine

from coifesp_harness.connectors import ConnectorResponse, GitHubTools
from coifesp_harness.connectors.github_receipts import GitHubReceiptReader
from coifesp_harness.errors import ResourceNotFound
from coifesp_harness.security import Classification
from coifesp_harness.tool_jobs import (
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolJobKeyring,
    ToolJobStatus,
)
from coifesp_harness.tools import ToolRegistry


def run_job(tmp_path, body, *, operation="github.get_commit_checks"):
    engine = create_engine("sqlite:///" + (tmp_path / "jobs.db").as_posix())
    keyring = ToolJobKeyring(master_key=b"k" * 32, key_id="test-v1")
    jobs = SQLAlchemyToolJobRepository(engine=engine, keyring=keyring)
    jobs.create_schema()
    calls = []

    class Client:
        async def execute(self, request):
            calls.append(request)
            return ConnectorResponse(200, body)

    registry = ToolRegistry()
    for definition in GitHubTools(client=Client(), tenant_id="team-a",
                                  classification=Classification.INTERNAL).definitions():
        registry.register(definition)
    arguments = {"connector_id": "github-main", "repository": "owner/repo"}
    if operation == "github.create_issue":
        arguments.update(title="Review", body="Private input")
    elif operation == "github.dispatch_workflow":
        arguments.update(workflow="verify.yml", ref="main", inputs={})
    else:
        arguments["commit_sha"] = "a" * 40
    jobs.enqueue(tenant_id="team-a", actor_id="lead", job_id="job-1", run_id="run-1",
                 call_id="call-1", tool_name=operation, idempotency_key="idem-1",
                 arguments=arguments)
    worker = DurableToolWorker(repository=jobs, registry=registry, tenant_id="team-a",
                               worker_id="worker", lease_seconds=5, heartbeat_seconds=1)
    asyncio.run(worker.run_once())
    return engine, keyring, jobs, worker, calls


def checks(*, conclusion="success", complete=True, sha=None):
    return {"repository": "owner/repo", "commit_sha": sha or "a" * 40,
            "complete": complete, "checks": [
                {"id": 1, "name": "pytest", "status": "completed", "conclusion": conclusion},
            ]}


@pytest.mark.parametrize("operation,body", [
    ("github.create_issue", {"repository": "owner/repo", "issue_number": 42}),
    ("github.dispatch_workflow", {"repository": "owner/repo", "dispatch_id": "dispatch-1",
                                  "accepted": True, "workflow": "verify.yml", "ref": "main"}),
    ("github.get_commit_checks", checks()),
])
def test_receipt_survives_repository_restart_without_repeating_provider(tmp_path, operation, body):
    engine, keyring, jobs, worker, calls = run_job(tmp_path, body, operation=operation)
    original = GitHubReceiptReader(jobs).read(tenant_id="team-a", run_id="run-1", job_id="job-1")
    engine.dispose()
    restarted = SQLAlchemyToolJobRepository(engine=engine, keyring=keyring)
    assert GitHubReceiptReader(restarted).read(
        tenant_id="team-a", run_id="run-1", job_id="job-1",
    ) == original
    assert asyncio.run(worker.run_once()) is False
    assert len(calls) == 1
    assert "Private input" not in str(original)
    with pytest.raises(ResourceNotFound):
        GitHubReceiptReader(restarted).read(tenant_id="team-a", run_id="other", job_id="job-1")
    engine.dispose()


@pytest.mark.parametrize("body,expected", [
    (checks(), "PASS"), (checks(conclusion="failure"), "FAIL"),
    (checks(conclusion="skipped"), "PENDING"), (checks(complete=False), "PENDING"),
    ({**checks(), "checks": []}, "PENDING"),
])
def test_checks_require_complete_subject_bound_evidence(tmp_path, body, expected):
    engine, _, jobs, _, _ = run_job(tmp_path, body)
    reader = GitHubReceiptReader(jobs)
    args = {"tenant_id": "team-a", "run_id": "run-1", "job_id": "job-1",
            "repository": "owner/repo", "commit_sha": "a" * 40,
            "required_checks": ["pytest"]}
    assert reader.evaluate_checks(**args) == expected
    with pytest.raises(ValueError, match="subject"):
        reader.evaluate_checks(**{**args, "commit_sha": "b" * 40})
    engine.dispose()


@pytest.mark.parametrize("body", [
    {"accepted": True}, checks(sha="b" * 40),
    {**checks(), "checks": [{"id": 1, "name": "pytest", "status": "completed",
                             "conclusion": "made-up"}]},
])
def test_invalid_adapter_evidence_cannot_be_persisted_as_success(tmp_path, body):
    engine, _, jobs, _, _ = run_job(tmp_path, body)
    job = jobs.get(tenant_id="team-a", job_id="job-1", include_payloads=True)
    assert job.status == ToolJobStatus.FAILED
    assert job.error_code == "github_invalid_receipt"
    assert GitHubReceiptReader(jobs).read(tenant_id="team-a", run_id="run-1", job_id="job-1") is None
    engine.dispose()
