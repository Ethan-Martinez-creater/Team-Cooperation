"""Task-scoped human acceptance of fixed verification evidence.

These records are not global budget/delivery Gates: one task awaiting a human
must not suspend unrelated project work or confer project-completion authority.
"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select

from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..product.repository import PROJECT_AGENT_RUNS
from ..product.service import TeamCollaborationService
from ..project_process.repository import PROJECT_PROCESSES
from ..project_process.service import ProjectProcessService
from .checks import _aggregate
from .human_models import parse_human_decision
from .repository import HUMAN_REVIEWS, TASK_VERIFICATIONS
from .subjects import submission_is_current


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _time(value):
    return None if value is None else (value.replace(tzinfo=UTC) if value.tzinfo is None
                                      else value.astimezone(UTC)).isoformat()


class HumanReviewChecks:
    def __init__(self, *, repository, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def evaluate(self, *, connection, outcome, verification_id, subject_digest, binding, task):
        if outcome["status"] == "FAIL":
            return outcome
        ready = all(not check["required"] or check["status"] == "PASS"
                    for check in outcome["checks"] if check["type"] != "human_review")
        checks = []
        for original in outcome["checks"]:
            check = dict(original)
            if check["type"] != "human_review":
                checks.append(check)
                continue
            if not check["required"]:
                check.update(status="PENDING", code="optional_human_review_not_requested")
            elif not ready:
                check.update(status="PENDING", code="human_review_waiting_for_checks")
            else:
                key = _hash(check["criterion_id"])
                row = connection.execute(select(HUMAN_REVIEWS).where(
                    HUMAN_REVIEWS.c.verification_id == verification_id,
                    HUMAN_REVIEWS.c.criterion_key == key,
                ).with_for_update()).mappings().one_or_none()
                if row is None:
                    now = self.clock()
                    row = {"review_id": "human-review:" + _hash(verification_id + ":" + key),
                           "verification_id": verification_id, "project_id": task["project_id"],
                           "process_id": binding["process_id"], "task_id": task["task_id"],
                           "source_run_id": binding["run_id"], "reviewer_team_id": task["source_team_id"],
                           "criterion_id": check["criterion_id"], "criterion_key": key,
                           "subject_digest": subject_digest,
                           "contract_version": binding["task_contract_version"],
                           "status": "OPEN", "version": 1, "created_at": now, "updated_at": now,
                           "completed_at": None, "decision": None, "decision_key": None,
                           "decision_digest": None, "decided_by": None, "reason": None, "decided_at": None}
                    connection.execute(HUMAN_REVIEWS.insert().values(**row))
                    self._emit(connection, row, "opened")
                if (row["criterion_id"] != check["criterion_id"] or row["subject_digest"] != subject_digest
                        or row["source_run_id"] != binding["run_id"]
                        or row["reviewer_team_id"] != task["source_team_id"]):
                    raise GovernanceConflictError("human review binding changed")
                status = {"ACCEPTED": "PASS", "REJECTED": "FAIL"}.get(row["status"], "PENDING")
                check.update(status=status, code={"PASS": "human_review_accepted", "FAIL": "human_review_rejected"}
                             .get(status, "human_review_pending"), human_review_id=row["review_id"],
                             human_review_version=row["version"], reviewer_team_id=row["reviewer_team_id"])
                if row["decision"] is not None:
                    check["human_decision"] = {"decision": row["decision"], "reason": row["reason"],
                                               "decided_by": row["decided_by"], "decided_at": _time(row["decided_at"])}
            checks.append(check)
        return {"status": _aggregate(checks), "checks": checks}

    def close_stale(self, connection, verification_id):
        now = self.clock()
        rows = connection.execute(select(HUMAN_REVIEWS).where(
            HUMAN_REVIEWS.c.verification_id == verification_id, HUMAN_REVIEWS.c.status == "OPEN",
        ).with_for_update()).mappings().all()
        for row in rows:
            values = {"status": "STALE", "version": row["version"] + 1,
                      "completed_at": now, "updated_at": now}
            connection.execute(HUMAN_REVIEWS.update().where(HUMAN_REVIEWS.c.review_id == row["review_id"])
                               .values(**values))
            self._emit(connection, {**row, **values}, "closed")

    def _emit(self, connection, row, kind, actor="service:project-verifier"):
        repository = self.repository.using_connection(connection)
        process = repository.process(connection, row["process_id"])
        ProjectProcessService(repository, clock=self.clock).append_fact(
            process_id=process.process_id, event_id=row["review_id"] + ":" + kind,
            event_type="task_verification.human_review." + kind,
            expected_version=process.version, expected_event_sequence=process.last_event_sequence,
            subject_type="human_review", subject_id=row["review_id"],
            initiated_by=actor, executed_as=actor, correlation_id=row["source_run_id"],
            source_aggregate_version=row["version"],
            payload={"review_id": row["review_id"], "verification_id": row["verification_id"],
                     "task_id": row["task_id"], "status": row["status"], "version": row["version"],
                     "subject_digest": row["subject_digest"], "contract_version": row["contract_version"]},
        )

    def list_reviews(self, *, verifier, project_id, task_id, actor_id):
        with self.repository.transaction() as connection:
            task = TeamCollaborationService._task_row(connection, project_id, task_id)
            verifier._authorize(connection, task, actor_id)
            rows = connection.execute(select(HUMAN_REVIEWS).where(
                HUMAN_REVIEWS.c.project_id == project_id, HUMAN_REVIEWS.c.task_id == task_id,
            ).order_by(HUMAN_REVIEWS.c.created_at, HUMAN_REVIEWS.c.review_id)).mappings().all()
            result = []
            for row in rows:
                evidence = connection.execute(select(TASK_VERIFICATIONS).where(
                    TASK_VERIFICATIONS.c.verification_id == row["verification_id"],
                )).mappings().one()
                result.append({**self._view(row), "verification_status": evidence["status"],
                               "artifacts": evidence["artifacts_json"], "checks": evidence["checks_json"]})
            return result

    def decide(self, *, verifier, project_id, task_id, review_id, actor_id, decision):
        payload = parse_human_decision(decision)
        digest = _hash(json.dumps({**payload, "actor_id": actor_id}, sort_keys=True, ensure_ascii=False))
        with self.repository.transaction() as connection:
            row = connection.execute(select(HUMAN_REVIEWS).where(
                HUMAN_REVIEWS.c.review_id == review_id, HUMAN_REVIEWS.c.project_id == project_id,
                HUMAN_REVIEWS.c.task_id == task_id,
            )).mappings().one_or_none()
            if row is None:
                raise ResourceNotFound("human review is unavailable")
            connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == row["process_id"],
            ).with_for_update()).scalar_one()
            task = TeamCollaborationService._task_row(connection, project_id, task_id)
            actor = TeamCollaborationService._participant(connection, project_id, actor_id)
            if (not actor["enabled"] or actor["registration_status"] != "active"
                    or actor["team_id"] != row["reviewer_team_id"]
                    or actor["team_id"] != task["source_team_id"]):
                raise PolicyDenied("only an active account in the requesting team may decide this review")
            row = connection.execute(select(HUMAN_REVIEWS).where(
                HUMAN_REVIEWS.c.review_id == review_id,
            ).with_for_update()).mappings().one()
            evidence = connection.execute(select(TASK_VERIFICATIONS).where(
                TASK_VERIFICATIONS.c.verification_id == row["verification_id"],
            ).with_for_update()).mappings().one()
            if row["decision_key"] == payload["idempotency_key"]:
                if row["decision_digest"] != digest:
                    raise GovernanceConflictError("human review decision key was reused with different content")
                return {"review": self._view(row), "verification": verifier._view(evidence)}
            if row["status"] != "OPEN" or row["version"] != payload["expected_version"]:
                raise GovernanceConflictError("human review is already decided or its version changed")
            binding = connection.execute(select(PROJECT_AGENT_RUNS).where(
                PROJECT_AGENT_RUNS.c.run_id == row["source_run_id"],
            )).mappings().one()
            if (evidence["status"] != "PENDING" or row["subject_digest"] != evidence["subject_digest"]
                    or not submission_is_current(connection,
                        process=self.repository.process(connection, row["process_id"]), binding=binding, task=task)
                    or not verifier._resources_current(connection, task, binding["task_result_json"],
                                                       evidence["artifacts_json"])):
                raise GovernanceConflictError("human review submission is no longer current")
            now = self.clock()
            values = {"status": "ACCEPTED" if payload["decision"] == "ACCEPT" else "REJECTED",
                      "version": row["version"] + 1, "decision": payload["decision"],
                      "decision_key": payload["idempotency_key"], "decision_digest": digest,
                      "decided_by": actor_id, "reason": payload["reason"], "decided_at": now,
                      "updated_at": now, "completed_at": now}
            changed = connection.execute(HUMAN_REVIEWS.update().where(
                HUMAN_REVIEWS.c.review_id == review_id, HUMAN_REVIEWS.c.status == "OPEN",
                HUMAN_REVIEWS.c.version == payload["expected_version"],
            ).values(**values)).rowcount
            if changed != 1:
                raise GovernanceConflictError("human review changed concurrently")
            self._emit(connection, {**row, **values}, "decided", actor=actor_id)
            # Decision, aggregate, task status, and its authoritative event/outbox
            # commit together. A failed verification write rolls back the decision.
            result = verifier.using_connection(connection).verify_run(run_id=row["source_run_id"])
            return {"review": self._view({**row, **values}), "verification": result}

    @staticmethod
    def _view(row):
        value = {key: row[key] for key in ("review_id", "verification_id", "project_id", "task_id",
                 "source_run_id", "reviewer_team_id", "criterion_id", "subject_digest", "contract_version",
                 "status", "version", "decision", "decided_by", "reason")}
        value.update({key: _time(row[key]) for key in ("created_at", "updated_at", "decided_at", "completed_at")})
        return value
