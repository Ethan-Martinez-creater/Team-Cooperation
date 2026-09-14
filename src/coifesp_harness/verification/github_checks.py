"""GitHub evidence bound to the accepted policy and exact submitted execution."""

from __future__ import annotations

from ..connectors.github_receipts import GitHubReceiptReader, digest
from ..errors import GovernanceConflictError
from ..tool_jobs import ToolJobStatus

VERIFIER = "service:project-verifier"
TOOL = "github.get_commit_checks"


def evaluate_github_check(*, jobs, check, subject, previous, verification_id,
                          subject_digest, run_id, tenant_id, retry_tools=False):
    if not subject or not check["required"]:
        return {**check, "status": "PENDING", "code": "github_check_not_scheduled"}
    attempt = previous.get("tool_attempt", 1) if previous else 1
    superseded = previous.get("superseded_tool_job_ids", []) if previous else []
    job_id = "github-verify-" + digest([
        verification_id, subject_digest, check["criterion_id"], subject, attempt,
    ])
    if previous and previous.get("tool_job_id") not in {None, job_id}:
        raise GovernanceConflictError("GitHub verification job binding changed")
    arguments = {key: subject[key] for key in ("connector_id", "repository", "commit_sha")}
    jobs.enqueue(tenant_id=tenant_id, actor_id=VERIFIER, job_id=job_id, run_id=run_id,
                 call_id=job_id, tool_name=TOOL, idempotency_key=job_id, arguments=arguments)
    job = jobs.get(tenant_id=tenant_id, job_id=job_id, include_payloads=True)
    if (job.created_by != VERIFIER or job.arguments != arguments or job.tool_name != TOOL
            or job.run_id != run_id or job.call_id != job_id or job.idempotency_key != job_id):
        raise GovernanceConflictError("GitHub verification job identity mismatch")
    check = {**check, "status": "PENDING", "code": "github_execution_pending",
             "tool_job_id": job_id, "tool_attempt": attempt,
             "superseded_tool_job_ids": superseded}
    check.pop("receipt_digest", None)
    if job.status in {ToolJobStatus.FAILED, ToolJobStatus.CANCELLED}:
        check["code"] = "github_execution_unavailable"
    elif job.status == ToolJobStatus.SUCCEEDED:
        reader = GitHubReceiptReader(jobs)
        try:
            status = reader.evaluate_checks(
                tenant_id=tenant_id, run_id=run_id, job_id=job_id,
                repository=subject["repository"], commit_sha=subject["commit_sha"],
                required_checks=subject["required_checks"],
            )
            receipt = reader.read(tenant_id=tenant_id, run_id=run_id, job_id=job_id)
        except (ValueError, TypeError, KeyError):
            check["code"] = "github_result_invalid"
        else:
            check.update(status=status, code="github_checks_" + status.lower(),
                         receipt_digest=receipt["receipt_digest"])
    if retry_tools and check["code"] in {
        "github_execution_unavailable", "github_result_invalid", "github_checks_pending",
    }:
        if attempt >= 10:
            raise GovernanceConflictError("GitHub verification retry limit reached")
        return evaluate_github_check(
            jobs=jobs, check=check, subject=subject,
            previous={"tool_attempt": attempt + 1, "superseded_tool_job_ids": [*superseded, job_id]},
            verification_id=verification_id, subject_digest=subject_digest,
            run_id=run_id, tenant_id=tenant_id,
        )
    return check
