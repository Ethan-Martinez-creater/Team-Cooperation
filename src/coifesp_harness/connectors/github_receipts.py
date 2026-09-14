"""Validated GitHub observations stored atomically in the encrypted Tool Job result."""

from __future__ import annotations

import hashlib
import json

from ..errors import ResourceNotFound
from ..tool_jobs import ToolJobStatus

SCHEMA = "coifesp.github-receipt.v1"
OPERATIONS = {
    "/v1/github/issues": "github.create_issue",
    "/v1/github/workflow-dispatches": "github.dispatch_workflow",
    "/v1/github/commit-checks": "github.get_commit_checks",
}


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def build_receipt(*, context, path, arguments, response):
    body = response.body
    if not 200 <= response.status_code < 300 or type(body) is not dict:
        raise ValueError("invalid GitHub adapter response")
    if body.get("repository") != arguments["repository"]:
        raise ValueError("GitHub repository mismatch")
    operation = OPERATIONS[path]
    if operation == "github.create_issue":
        number = body.get("issue_number")
        if type(number) is not int or number < 1:
            raise ValueError("missing GitHub issue identity")
        observation = {"issue_number": number}
    elif operation == "github.dispatch_workflow":
        dispatch_id = body.get("dispatch_id")
        if (not isinstance(dispatch_id, str) or not 1 <= len(dispatch_id) <= 256
                or body.get("workflow") != arguments["workflow"]
                or body.get("ref") != arguments["ref"] or body.get("accepted") is not True):
            raise ValueError("invalid GitHub dispatch identity")
        observation = {"dispatch_id": dispatch_id, "workflow": body["workflow"],
                       "ref": body["ref"], "status": "ACCEPTED"}
    else:
        if body.get("commit_sha") != arguments["commit_sha"]:
            raise ValueError("GitHub commit mismatch")
        checks = body.get("checks")
        complete = body.get("complete")
        if type(checks) is not list or len(checks) > 100 or type(complete) is not bool:
            raise ValueError("invalid GitHub checks response")
        normalized, ids = [], set()
        conclusions = {"success", "failure", "cancelled", "timed_out", "neutral",
                       "skipped", "action_required", "stale", "startup_failure"}
        for check in checks:
            if type(check) is not dict:
                raise ValueError("invalid GitHub check")
            identity, name = check.get("id"), check.get("name")
            status, conclusion = check.get("status"), check.get("conclusion")
            if (type(identity) is not int or identity < 1 or identity in ids
                    or not isinstance(name, str) or not 1 <= len(name) <= 256
                    or status not in {"queued", "in_progress", "completed"}
                    or (status == "completed" and conclusion not in conclusions)
                    or (status != "completed" and conclusion is not None)):
                raise ValueError("invalid GitHub check identity or state")
            ids.add(identity)
            normalized.append({"id": identity, "name": name, "status": status,
                               "conclusion": conclusion})
        observation = {"commit_sha": body["commit_sha"], "complete": complete,
                       "checks": sorted(normalized, key=lambda item: item["id"])}
    receipt = {
        "schema": SCHEMA, "tenant_id": context.tenant_id, "run_id": context.run_id,
        "job_id": context.job_id, "call_id": context.call_id,
        "operation": operation, "connector_id": arguments["connector_id"],
        "repository": arguments["repository"], "arguments_digest": digest(arguments),
        "idempotency_digest": digest(context.idempotency_key), "observation": observation,
    }
    return {**receipt, "receipt_digest": digest(receipt)}


class GitHubReceiptReader:
    """Read only committed, tenant/run-bound observations; never repeat provider writes."""

    def __init__(self, jobs):
        self.jobs = jobs

    def read(self, *, tenant_id, run_id, job_id):
        job = self.jobs.get(tenant_id=tenant_id, job_id=job_id, include_payloads=True)
        if job.run_id != run_id or job.tool_name not in OPERATIONS.values():
            raise ResourceNotFound("GitHub receipt is absent or hidden")
        if job.status != ToolJobStatus.SUCCEEDED:
            return None
        result = job.result
        if type(result) is not dict or type(result.get("receipt")) is not dict:
            raise ValueError("GitHub receipt is unavailable or invalid")
        receipt = dict(result["receipt"])
        stored_digest = receipt.pop("receipt_digest", None)
        if (stored_digest != digest(receipt) or receipt.get("schema") != SCHEMA
                or receipt.get("tenant_id") != tenant_id or receipt.get("run_id") != run_id
                or receipt.get("job_id") != job_id or receipt.get("call_id") != job.call_id
                or receipt.get("operation") != job.tool_name
                or receipt.get("arguments_digest") != digest(job.arguments)
                or receipt.get("idempotency_digest") != digest(job.idempotency_key)
                or receipt.get("connector_id") != job.arguments["connector_id"]
                or receipt.get("repository") != job.arguments["repository"]):
            raise ValueError("GitHub receipt binding mismatch")
        return {**receipt, "receipt_digest": stored_digest}

    def evaluate_checks(self, *, tenant_id, run_id, job_id, repository, commit_sha,
                        required_checks):
        if not required_checks or any(not isinstance(name, str) or not name for name in required_checks):
            raise ValueError("explicit required checks are necessary")
        receipt = self.read(tenant_id=tenant_id, run_id=run_id, job_id=job_id)
        if receipt is None:
            return "PENDING"
        observation = receipt["observation"]
        if (receipt["operation"] != "github.get_commit_checks"
                or receipt["repository"] != repository
                or observation.get("commit_sha") != commit_sha):
            raise ValueError("GitHub verification subject mismatch")
        if not observation["complete"]:
            return "PENDING"
        selected = []
        for name in required_checks:
            matches = [check for check in observation["checks"] if check["name"] == name]
            if len(matches) != 1 or matches[0]["status"] != "completed":
                return "PENDING"
            selected.append(matches[0]["conclusion"])
        if any(value in {"failure", "cancelled", "timed_out", "action_required",
                         "stale", "startup_failure"} for value in selected):
            return "FAIL"
        return "PASS" if all(value == "success" for value in selected) else "PENDING"
