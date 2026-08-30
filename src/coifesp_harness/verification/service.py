"""Task verification evidence and Product status commit in one transaction.

This service verifies a pinned submission, not project completion. Unknown
checks remain pending; no HTTP input or model statement can manufacture PASS.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from ..artifacts.repository import ARTIFACT_MANIFESTS
from ..errors import GovernanceConflictError, PolicyDenied
from ..product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    TEAM_TASKS,
)
from ..product.service import TeamCollaborationService
from ..project_process.repository import PROJECT_PROCESSES
from ..project_process.service import ProjectProcessService
from ..team_agents.identity import ORCHESTRATOR_PRINCIPAL_ID
from .checks import evaluate_checks
from .human_reviews import HumanReviewChecks
from .repository import TASK_VERIFICATIONS
from .subjects import submission_is_current

logger = logging.getLogger("coifesp.verification")
VERIFIER_PRINCIPAL = "service:project-verifier"


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _baseline(status, code):
    return {
        "status": status,
        "checks": [
            {
                "criterion_id": "__artifact_integrity__",
                "type": "tool_check",
                "required": True,
                "status": status,
                "code": code,
                "evidence_refs": [],
            }
        ],
    }


def _time(value):
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


class TaskVerificationService:
    def __init__(self, *, repository, artifact_content=None, notifier=None, clock=None,
                 tool_checks=None, review_checks=None, human_checks=None):
        self.repository = repository
        self.artifact_content = artifact_content
        self.notifier = notifier
        self.tool_checks = tool_checks
        self.review_checks = review_checks
        self.clock = clock or (lambda: datetime.now(UTC))
        self.human_checks = human_checks or HumanReviewChecks(repository=repository, clock=self.clock)

    def using_connection(self, connection):
        return TaskVerificationService(repository=self.repository.using_connection(connection),
            artifact_content=self.artifact_content, notifier=self.notifier, clock=self.clock,
            tool_checks=self.tool_checks, review_checks=self.review_checks, human_checks=self.human_checks)

    def human_reviews(self, **kwargs):
        return self.human_checks.list_reviews(verifier=self, **kwargs)

    def decide_human_review(self, **kwargs):
        return self.human_checks.decide(verifier=self, **kwargs)

    @staticmethod
    def _authorize(connection, task, actor_id):
        actor = TeamCollaborationService._participant(connection, task["project_id"], actor_id)
        if actor["team_id"] not in {task["source_team_id"], task["target_team_id"]}:
            raise PolicyDenied("verification belongs to the task parties")

    def verify_task(self, *, project_id, task_id, actor_id, retry_tools=False, retry_reviews=False):
        with self.repository.transaction() as connection:
            task = TeamCollaborationService._task_row(connection, project_id, task_id)
            self._authorize(connection, task, actor_id)
            if task["status"] not in {"submitted", "verified"}:
                raise GovernanceConflictError("task has no current submission to verify")
            run_id = connection.execute(
                select(PROJECT_AGENT_RUNS.c.run_id)
                .where(
                    PROJECT_AGENT_RUNS.c.project_id == project_id,
                    PROJECT_AGENT_RUNS.c.team_task_id == task_id,
                    PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                    PROJECT_AGENT_RUNS.c.task_result_status == "submitted",
                    PROJECT_AGENT_RUNS.c.task_contract_version == task["accepted_contract_version"],
                )
                .order_by(PROJECT_AGENT_RUNS.c.execution_attempt.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run_id is None:
                raise GovernanceConflictError("verification requires a structured Agent submission")
        # Reauthorize under the process/task locks before doing any work.
        return self.verify_run(run_id=run_id, actor_id=actor_id, retry_tools=retry_tools,
                               retry_reviews=retry_reviews)

    def results(self, *, project_id, task_id, actor_id):
        with self.repository.transaction() as connection:
            task = TeamCollaborationService._task_row(connection, project_id, task_id)
            self._authorize(connection, task, actor_id)
            rows = connection.execute(
                select(TASK_VERIFICATIONS)
                .where(
                    TASK_VERIFICATIONS.c.project_id == project_id,
                    TASK_VERIFICATIONS.c.task_id == task_id,
                )
                .order_by(TASK_VERIFICATIONS.c.created_at, TASK_VERIFICATIONS.c.verification_id)
            ).mappings()
            return [self._view(row) for row in rows]

    def on_run_terminal(self, run):
        if self.review_checks is not None:
            source = self.review_checks.project_terminal(run.run_id)
            if source is not None:
                return self.verify_run(run_id=source)
        return self.verify_run(run_id=run.run_id)

    def verify_run(self, *, run_id, actor_id=None, retry_tools=False, retry_reviews=False):
        if (retry_tools or retry_reviews) and actor_id is None:
            raise PolicyDenied("verification retries require an authorized task participant")
        with self.repository.transaction() as connection:
            binding = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS).where(
                        PROJECT_AGENT_RUNS.c.run_id == run_id,
                        PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if binding is None or binding["task_result_status"] != "submitted":
                return None
            connection.execute(
                select(PROJECT_PROCESSES.c.process_id)
                .where(
                    PROJECT_PROCESSES.c.process_id == binding["process_id"],
                )
                .with_for_update()
            ).scalar_one()
            binding = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS)
                    .where(
                        PROJECT_AGENT_RUNS.c.run_id == run_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            task = TeamCollaborationService._task_row(
                connection,
                binding["project_id"],
                binding["team_task_id"],
            )
            if actor_id is not None:
                self._authorize(connection, task, actor_id)
            if (
                binding["team_id"] != task["target_team_id"]
                or binding["initiated_by_principal_id"] != ORCHESTRATOR_PRINCIPAL_ID
                or binding["executed_as_principal_id"] != f"team-agent:{task['target_team_id']}"
            ):
                raise GovernanceConflictError("verification Run binding identity mismatch")
            receipt = binding["task_result_json"]
            existing = (
                connection.execute(
                    select(TASK_VERIFICATIONS)
                    .where(
                        TASK_VERIFICATIONS.c.source_run_id == run_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            artifacts = receipt.get("artifact_manifests")
            policy = receipt.get("verification_policy") or task["verification_policy_json"]
            if existing:
                # Policy changes on a later task contract cannot create a new
                # interpretation of this old Run's evidence or strand PENDING.
                policy = existing["policy_json"]
                artifacts = existing["artifacts_json"]
            subject_digest = _digest(
                {
                    "run_id": run_id,
                    "contract_version": binding["task_contract_version"],
                    "submitted_at": _time(binding["task_result_at"]),
                    "artifact_refs": receipt["artifact_refs"],
                    "artifacts": artifacts,
                    "policy": policy,
                }
            )
            verification_id = "verification:" + _digest([run_id, subject_digest])
            if existing:
                verification_id = existing["verification_id"]
                subject_digest = existing["subject_digest"]
            if existing is not None and existing["status"] != "PENDING":
                return self._view(existing)
            process = self.repository.process(connection, binding["process_id"])
            current = submission_is_current(connection, process=process, binding=binding, task=task)
            if not current:
                outcome = _baseline("PENDING", "submission_changed")
                status = "STALE"
            elif (
                receipt.get("artifact_manifests") is None
                or receipt.get("verification_policy") is None
            ):
                # Do not infer a historical digest from a mutable current resource.
                outcome = _baseline("PENDING", "submission_snapshot_unavailable")
                status = "PENDING"
            elif not self._resources_current(connection, task, receipt, artifacts):
                outcome = _baseline("FAIL", "submission_artifacts_changed")
                status = "FAIL"
            else:
                outcome = evaluate_checks(
                    policy=policy,
                    artifacts=artifacts,
                    artifact_content=self.artifact_content,
                )
                if self.tool_checks is not None:
                    outcome = self.tool_checks.evaluate(
                        connection=connection, outcome=outcome,
                        existing_checks=existing["checks_json"] if existing else [],
                        verification_id=verification_id, subject_digest=subject_digest,
                        run_id=run_id, tenant_id=task["target_team_id"],
                        retry_tools=retry_tools,
                    )
                if self.review_checks is not None:
                    outcome = self.review_checks.evaluate(
                        connection=connection, outcome=outcome, verification_id=verification_id,
                        subject_digest=subject_digest, binding=binding, task=task,
                        artifacts=artifacts, retry_reviews=retry_reviews,
                    )
                outcome = self.human_checks.evaluate(
                    connection=connection, outcome=outcome, verification_id=verification_id,
                    subject_digest=subject_digest, binding=binding, task=task,
                )
                status = outcome["status"]
            now = self.clock()
            values = {
                "verification_id": verification_id,
                "project_id": task["project_id"],
                "process_id": binding["process_id"],
                "task_id": task["task_id"],
                "source_run_id": run_id,
                "contract_version": binding["task_contract_version"],
                "subject_digest": subject_digest,
                "policy_json": policy,
                "artifacts_json": artifacts if artifacts is not None else [],
                "checks_json": outcome["checks"],
                "status": status,
                "initiated_by": existing["initiated_by"]
                if existing
                else (actor_id or ORCHESTRATOR_PRINCIPAL_ID),
                "executed_as": VERIFIER_PRINCIPAL,
                "version": existing["version"] + 1 if existing else 1,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                "completed_at": None if status == "PENDING" else now,
            }
            if existing and status == "PENDING" and existing["checks_json"] == outcome["checks"]:
                return self._view(existing)
            if existing:
                changed = connection.execute(
                    TASK_VERIFICATIONS.update()
                    .where(
                        TASK_VERIFICATIONS.c.verification_id == verification_id,
                        TASK_VERIFICATIONS.c.version == existing["version"],
                        TASK_VERIFICATIONS.c.status == "PENDING",
                    )
                    .values(**values)
                ).rowcount
                if changed != 1:
                    raise GovernanceConflictError("verification evidence changed concurrently")
            else:
                connection.execute(TASK_VERIFICATIONS.insert().values(**values))
            if status in {"STALE", "FAIL"} and self.review_checks is not None:
                self.review_checks.close_stale(connection, verification_id)
            if status in {"STALE", "FAIL"}:
                self.human_checks.close_stale(connection, verification_id)
            if current and status in {"PASS", "FAIL"}:
                task_status = "verified" if status == "PASS" else "changes_requested"
                changed = connection.execute(
                    TEAM_TASKS.update()
                    .where(
                        TEAM_TASKS.c.task_id == task["task_id"],
                        TEAM_TASKS.c.status == "submitted",
                        TEAM_TASKS.c.updated_at == task["updated_at"],
                        TEAM_TASKS.c.accepted_contract_version == binding["task_contract_version"],
                    )
                    .values(
                        status=task_status,
                        updated_at=now,
                        completed_at=now if status == "PASS" else None,
                        review_note="Verification passed"
                        if status == "PASS"
                        else "Verification requires changes",
                    )
                ).rowcount
                if changed != 1:
                    raise GovernanceConflictError("submission changed before verification commit")
                if status == "PASS" and self.notifier is not None and task["due_at"] is not None:
                    self.notifier.refresh_task_reminders(
                        connection,
                        {**task, "status": task_status, "completed_at": now, "updated_at": now},
                        now=now,
                    )
                # Opportunistic review projection may append a Run terminal
                # fact in this same transaction. Use the resulting sequence.
                process = self.repository.process(connection, binding["process_id"])
                ProjectProcessService(
                    self.repository.using_connection(connection), clock=self.clock
                ).append_fact(
                    process_id=process.process_id,
                    event_id=f"{verification_id}:result",
                    event_type=f"team_task.{task_status}",
                    expected_version=process.version,
                    expected_event_sequence=process.last_event_sequence,
                    subject_type="team_task",
                    subject_id=task["task_id"],
                    initiated_by=values["initiated_by"],
                    executed_as=VERIFIER_PRINCIPAL,
                    source_aggregate_version=binding["task_contract_version"],
                    correlation_id=run_id,
                    payload={
                        "verification_id": verification_id,
                        "task_id": task["task_id"],
                        "source_run_id": run_id,
                        "status": status,
                        "subject_digest": subject_digest,
                        "contract_version": binding["task_contract_version"],
                    },
                )
            return self._view(values)

    @staticmethod
    def _resources_current(connection, task, receipt, artifacts):
        if (
            type(artifacts) is not list
            or [item.get("resource_id") for item in artifacts] != receipt["artifact_refs"]
        ):
            return False
        teams = set(
            connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    PROJECT_TEAMS.c.project_id == task["project_id"],
                )
            ).scalars()
        )
        if not {task["source_team_id"], task["target_team_id"]} <= teams:
            return False
        for item in artifacts:
            if item.get("owner_team_id") != task["target_team_id"]:
                return False
            resource = connection.execute(
                select(PROJECT_RESOURCES.c.resource_id)
                .where(
                    PROJECT_RESOURCES.c.resource_id == item["resource_id"],
                    PROJECT_RESOURCES.c.project_id == task["project_id"],
                    PROJECT_RESOURCES.c.owner_team_id == item["owner_team_id"],
                    PROJECT_RESOURCES.c.artifact_owner_team_id == item["owner_team_id"],
                    PROJECT_RESOURCES.c.artifact_id == item["artifact_id"],
                    PROJECT_RESOURCES.c.artifact_sha256 == item["sha256"],
                    PROJECT_RESOURCES.c.media_type == item["media_type"],
                    PROJECT_RESOURCES.c.propagation.in_(("project_readonly", "portable")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            manifest = connection.execute(
                select(ARTIFACT_MANIFESTS.c.artifact_id)
                .where(
                    ARTIFACT_MANIFESTS.c.owner_tenant_id == item["owner_team_id"],
                    ARTIFACT_MANIFESTS.c.artifact_id == item["artifact_id"],
                    ARTIFACT_MANIFESTS.c.sha256 == item["sha256"],
                    ARTIFACT_MANIFESTS.c.media_type == item["media_type"],
                    ARTIFACT_MANIFESTS.c.size_bytes == item["size_bytes"],
                )
                .with_for_update()
            ).scalar_one_or_none()
            if resource is None or manifest is None:
                return False
        return True

    def replay_pending(self, _run_service=None):
        if self.review_checks is not None:
            self.review_checks.replay_pending()
        with self.repository.transaction() as connection:
            run_ids = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS.c.run_id)
                    .join(
                        TEAM_TASKS,
                        TEAM_TASKS.c.task_id == PROJECT_AGENT_RUNS.c.team_task_id,
                    )
                    .where(
                        PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                        PROJECT_AGENT_RUNS.c.task_result_status == "submitted",
                        TEAM_TASKS.c.status == "submitted",
                    )
                    .order_by(PROJECT_AGENT_RUNS.c.run_id)
                )
                .scalars()
                .all()
            )
            # Pending evidence must also become STALE if the task changed while
            # waiting; do not leave an obsolete verification waiting forever.
            run_ids += (
                connection.execute(
                    select(TASK_VERIFICATIONS.c.source_run_id).where(
                        TASK_VERIFICATIONS.c.status == "PENDING",
                    )
                )
                .scalars()
                .all()
            )
        count = 0
        for run_id in dict.fromkeys(run_ids):
            try:
                count += self.verify_run(run_id=run_id) is not None
            except Exception as exc:  # noqa: BLE001 - independent verification recovery
                logger.warning(
                    "verification replay deferred run_id=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
        return count

    @staticmethod
    def _view(row):
        return {
            "verification_id": row["verification_id"],
            "source_run_id": row["source_run_id"],
            "task_id": row["task_id"],
            "status": row["status"],
            "version": row["version"],
            "contract_version": row["contract_version"],
            "subject_digest": row["subject_digest"],
            "checks": row["checks_json"],
            "created_at": _time(row["created_at"]),
            "completed_at": _time(row["completed_at"]),
        }
