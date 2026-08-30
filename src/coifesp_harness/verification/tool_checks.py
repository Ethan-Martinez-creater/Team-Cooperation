"""Durable, submission-bound tool evidence; never execute code in the API process."""

from __future__ import annotations

import hashlib
import json
import logging

from jsonschema import Draft202012Validator
from sqlalchemy import select, text

from ..product.repository import PROJECT_AGENT_RUNS
from ..sandbox.models import WorkspaceAccess
from ..tool_jobs import ToolJobStatus
from .checks import _aggregate
from .repository import TASK_VERIFICATIONS

logger = logging.getLogger("coifesp.verification.tools")
TOOL_NAME = "verification.run_profile"
VERIFIER = "service:project-verifier"
RESULT_SCHEMA = "coifesp.verification-tool-result.v1"


class DurableVerificationChecks:
    """Enqueue and consume evidence in the caller's verification transaction.

    The persisted check pins the profile digest and job identity. Configuration
    edits cannot silently reinterpret a previously dispatched verification.
    """

    def __init__(self, *, jobs, profiles):
        self.jobs = jobs
        self.profiles = {profile.profile_id: profile for profile in profiles}
        if len(self.profiles) != len(profiles):
            raise ValueError("duplicate verification profiles")

    def evaluate(self, *, connection, outcome, existing_checks, verification_id,
                 subject_digest, run_id, tenant_id, retry_tools=False):
        # An unavailable or corrupt baseline cannot authorize external reads.
        if outcome["checks"][0]["status"] != "PASS" or outcome["status"] == "FAIL":
            return outcome
        previous = {check["criterion_id"]: check for check in existing_checks}
        checks = []
        for original in outcome["checks"]:
            check = dict(original)
            tool = check.get("tool", "")
            if check["type"] == "tool_check" and tool.startswith("sandbox.profile:"):
                if not check["required"]:
                    # Optional checks do not hold task completion. Do not create
                    # a job which would immediately lose its PENDING authority.
                    check.update(status="PENDING", code="optional_tool_not_scheduled")
                else:
                    check = self._evaluate_one(
                        connection=connection, check=check,
                        previous=previous.get(check["criterion_id"]),
                        verification_id=verification_id, subject_digest=subject_digest,
                        run_id=run_id, tenant_id=tenant_id,
                        retry_tools=retry_tools,
                    )
            checks.append(check)
        return {"status": _aggregate(checks), "checks": checks}

    def _evaluate_one(self, *, connection, check, previous, verification_id,
                      subject_digest, run_id, tenant_id, retry_tools=False):
        from .sandbox_tool import profile_digest

        profile_id = check["tool"].removeprefix("sandbox.profile:")
        profile = self.profiles.get(profile_id)
        if previous and previous.get("profile_digest"):
            digest = previous["profile_digest"]
        elif (profile is None or profile.workspace_access != WorkspaceAccess.READ_ONLY
              or not Draft202012Validator(
                  profile.arguments_schema or {"type": "array", "maxItems": 0}
              ).is_valid([])):
            check.update(status="PENDING", code="verification_profile_unavailable")
            return check
        else:
            digest = profile_digest(profile)
        attempt = previous.get("tool_attempt", 1) if previous else 1
        superseded = previous.get("superseded_tool_job_ids", []) if previous else []
        identity = hashlib.sha256(json.dumps(
            [verification_id, check["criterion_id"], digest, attempt], separators=(",", ":")
        ).encode()).hexdigest()
        job_id = "verify-" + identity
        arguments = {
            "verification_id": verification_id, "subject_digest": subject_digest,
            "criterion_id": check["criterion_id"], "profile_id": profile_id,
            "profile_digest": digest,
        }
        if previous and previous.get("tool_job_id") not in {None, job_id}:
            raise ValueError("verification job binding changed")
        jobs = self.jobs.using_connection(connection)
        jobs.enqueue(
            tenant_id=tenant_id, actor_id=VERIFIER, job_id=job_id, run_id=run_id,
            call_id=job_id, tool_name=TOOL_NAME, idempotency_key=job_id,
            arguments=arguments,
        )
        job = jobs.get(tenant_id=tenant_id, job_id=job_id,
                       include_payloads=True, connection=connection)
        if (job.run_id != run_id or job.call_id != job_id or job.tool_name != TOOL_NAME
                or job.created_by != VERIFIER or job.arguments != arguments):
            raise ValueError("verification tool evidence identity mismatch")
        check.update(tool_job_id=job_id, profile_digest=digest,
                     tool_attempt=attempt, superseded_tool_job_ids=superseded,
                     status="PENDING", code="tool_execution_pending")
        if job.status in {ToolJobStatus.FAILED, ToolJobStatus.CANCELLED}:
            # Infrastructure failure is not a failed submission. Keep explicit
            # unresolved evidence; do not turn worker errors into a false verdict.
            check["code"] = "tool_execution_unavailable"
        elif job.status == ToolJobStatus.SUCCEEDED:
            result = job.result
            expected = {key: arguments[key] for key in (
                "verification_id", "subject_digest", "criterion_id", "profile_digest"
            )}
            if (type(result) is not dict
                    or set(result) != set(expected) | {
                        "schema", "exit_code", "timed_out", "output_truncated"}
                    or result.get("schema") != RESULT_SCHEMA
                    or any(result.get(key) != value for key, value in expected.items())
                    or type(result.get("exit_code")) is not int
                    or type(result.get("timed_out")) is not bool
                    or type(result.get("output_truncated")) is not bool):
                check["code"] = "tool_result_invalid"
            elif result["timed_out"] or result["output_truncated"]:
                check["code"] = "tool_execution_incomplete"
            else:
                passed = result["exit_code"] == 0
                check.update(status="PASS" if passed else "FAIL",
                             code="tool_check_passed" if passed else "tool_check_failed")
        if retry_tools and check["code"] in {
            "tool_execution_unavailable", "tool_result_invalid", "tool_execution_incomplete"
        }:
            # Explicit task-party action only. Never endlessly requeue a broken
            # runtime during reconciliation, and retain all old job evidence.
            from ..errors import GovernanceConflictError

            if attempt >= 10:
                raise GovernanceConflictError("verification tool retry limit reached")
            return self._evaluate_one(
                connection=connection, check=check,
                previous={"profile_digest": digest, "tool_attempt": attempt + 1,
                          "superseded_tool_job_ids": [*superseded, job_id]},
                verification_id=verification_id, subject_digest=subject_digest,
                run_id=run_id, tenant_id=tenant_id,
            )
        return check


class VerificationToolReconciler:
    """Compose Agent tool wakeups with bounded, tenant-scoped verification replay."""

    def __init__(self, *, coordinator, verifier):
        self.coordinator, self.verifier = coordinator, verifier
        self._after = {}

    def reconcile(self, *, tenant_id, actor_id, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("verification reconciliation limit is invalid")
        count = self.coordinator.reconcile(tenant_id=tenant_id, actor_id=actor_id, limit=limit)
        with self.verifier.repository.transaction() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
                                   {"tenant": tenant_id})
            query = (
                select(TASK_VERIFICATIONS.c.verification_id, TASK_VERIFICATIONS.c.source_run_id)
                .join(PROJECT_AGENT_RUNS,
                      PROJECT_AGENT_RUNS.c.run_id == TASK_VERIFICATIONS.c.source_run_id)
                .where(TASK_VERIFICATIONS.c.status == "PENDING",
                       PROJECT_AGENT_RUNS.c.team_id == tenant_id)
                .order_by(TASK_VERIFICATIONS.c.verification_id)
                .limit(limit)
            )
            after = self._after.get(tenant_id)
            rows = connection.execute(query.where(
                TASK_VERIFICATIONS.c.verification_id > after
            ) if after else query).all()
            if not rows and after:
                rows = connection.execute(query).all()
            self._after[tenant_id] = rows[-1].verification_id if rows else None
        # Rotate across pending records: unavailable profiles/reviewers must not
        # permanently starve newer completed jobs beyond the per-poll limit.
        for _, run_id in rows:
            try:
                result = self.verifier.verify_run(run_id=run_id)
                count += result is not None and result["status"] != "PENDING"
            except Exception as exc:  # noqa: BLE001 - independent durable retries
                logger.warning("tool verification replay deferred run_id=%s error_type=%s",
                               run_id, type(exc).__name__)
        return count
