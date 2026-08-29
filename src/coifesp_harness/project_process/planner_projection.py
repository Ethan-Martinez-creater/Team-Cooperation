"""Project Planner AgentRun terminal projection with strict command validation."""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import and_, select

from ..agent_runs import DurableRunStatus
from ..agent_runs.repository import AGENT_RUNS
from ..errors import GovernanceConflictError
from ..product.repository import PROJECT_TEAMS
from ..security import Classification, Principal
from .command_service import ProjectProcessCommandService
from .command_validator import ProjectOrchestrationCommandValidator
from .commands import (
    ProjectOrchestrationDecisionStatus,
    ProjectPlannerIntentStatus,
)
from .planner import ProjectPlannerIntentService

logger = logging.getLogger("coifesp.project_process.planner_projection")


class ProjectPlannerProjection:
    def __init__(
        self,
        *,
        intent_service: ProjectPlannerIntentService,
        command_service: ProjectProcessCommandService,
        process_service,
        work_graph,
        run_reader,
        validator: ProjectOrchestrationCommandValidator | None = None,
    ) -> None:
        self.intent_service = intent_service
        self.command_service = command_service
        self.process_service = process_service
        self.work_graph = work_graph
        self.run_reader = run_reader
        self.validator = validator or ProjectOrchestrationCommandValidator()

    def on_run_terminal(self, run) -> None:
        intent = self.intent_service.find_by_run(run.run_id)
        if intent is None:
            intent = self._intent_from_correlation(run)
        if intent is None or intent.status not in {
            ProjectPlannerIntentStatus.PENDING,
            ProjectPlannerIntentStatus.RUNNING,
        }:
            return
        if intent.run_id is None:
            intent = self.intent_service.bind_run(intent.planner_intent_id, run.run_id)
        if run.status is DurableRunStatus.FAILED:
            self.intent_service.finish(
                intent.planner_intent_id,
                status=ProjectPlannerIntentStatus.FAILED,
                error_code="agent_run_failed",
            )
            return
        if run.status is DurableRunStatus.CANCELLED:
            self.intent_service.finish(
                intent.planner_intent_id,
                status=ProjectPlannerIntentStatus.CANCELLED,
                error_code="agent_run_cancelled",
            )
            return
        if run.status is not DurableRunStatus.COMPLETED:
            return
        try:
            payload = self._payload(run, intent)
            graph = self.work_graph.snapshot(project_id=intent.project_id)
            commands = ()
            if graph.digest == intent.graph_snapshot_digest:
                commands = self.validator.validate(
                    planner_intent_id=intent.planner_intent_id,
                    project_id=intent.project_id,
                    graph=graph,
                    graph_snapshot_digest=intent.graph_snapshot_digest,
                    participating_team_ids=self._participating_teams(intent.project_id),
                    commands=payload["commands"],
                )
        except (GovernanceConflictError, TypeError, ValueError, json.JSONDecodeError):
            self.intent_service.finish(
                intent.planner_intent_id,
                status=ProjectPlannerIntentStatus.REJECTED,
                error_code="invalid_planner_output",
            )
            return
        decision_id = self._decision_id(intent.planner_intent_id)
        stale_graph = graph.digest != intent.graph_snapshot_digest
        decision = self.command_service.record_decision(
            decision_id=decision_id,
            process_id=intent.process_id,
            project_id=intent.project_id,
            reason=intent.reason,
            based_on_process_version=intent.based_on_process_version,
            based_on_event_sequence=intent.based_on_event_sequence,
            graph_snapshot_digest=intent.graph_snapshot_digest,
            current_graph_snapshot_digest=graph.digest,
            decision_json={**payload, "commands": []} if stale_graph else payload,
            commands=() if stale_graph else tuple(item.as_record() for item in commands),
        )
        status = (
            ProjectPlannerIntentStatus.STALE
            if decision.status is ProjectOrchestrationDecisionStatus.STALE
            else ProjectPlannerIntentStatus.PROJECTED
        )
        if status is ProjectPlannerIntentStatus.STALE:
            self._emit_stale(intent, decision.decision_id, graph.digest, run.run_id)
        self.intent_service.finish(
            intent.planner_intent_id,
            status=status,
            decision_id=decision.decision_id,
        )

    def replay_pending(self, run_service) -> int:
        replayed = 0
        for intent in self.intent_service.pending():
            run_id = intent.run_id or self._find_run(intent)
            if run_id is None:
                continue
            principal = Principal(
                "service:project-orchestrator",
                intent.owner_team_id,
                frozenset({"agent_run_controller"}),
                Classification.RESTRICTED,
                frozenset(),
                True,
            )
            try:
                run = run_service.get(principal=principal, run_id=run_id)
                if run.status not in {
                    DurableRunStatus.COMPLETED,
                    DurableRunStatus.FAILED,
                    DurableRunStatus.CANCELLED,
                }:
                    continue
                self.on_run_terminal(run)
                replayed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "planner projection replay skipped run_id=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
        return replayed

    def _payload(self, run, intent) -> dict:
        messages = self.run_reader(run)
        content = next(
            (
                item["content"] if isinstance(item, dict) else item.content
                for item in reversed(tuple(messages))
                if (item.get("role") if isinstance(item, dict) else item.role)
                == "assistant"
            ),
            None,
        )
        if not isinstance(content, str):
            raise TypeError("planner run has no assistant decision")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise TypeError("planner output must be an object")
        required = {
            "schema",
            "planner_intent_id",
            "process_id",
            "project_id",
            "reason",
            "based_on_process_version",
            "based_on_event_sequence",
            "graph_snapshot_digest",
            "commands",
        }
        if set(payload) != required:
            raise ValueError("planner output fields are invalid")
        expected = self.intent_service.protocol(intent)
        for name in required - {"commands"}:
            if payload[name] != expected[name]:
                raise GovernanceConflictError("planner output binding changed")
        if not isinstance(payload["commands"], list):
            raise TypeError("planner commands must be an array")
        return payload

    def _participating_teams(self, project_id: str) -> tuple[str, ...]:
        with self.intent_service.repository.engine.connect() as connection:
            return tuple(
                connection.execute(
                    select(PROJECT_TEAMS.c.team_id)
                    .where(PROJECT_TEAMS.c.project_id == project_id)
                    .order_by(PROJECT_TEAMS.c.team_id)
                ).scalars()
            )

    def _emit_stale(self, intent, decision_id: str, current_digest: str, run_id: str) -> None:
        with self.intent_service.repository.transaction() as connection:
            process = self.intent_service.repository.process(
                connection, intent.process_id
            )
        event_digest = hashlib.sha256(decision_id.encode()).hexdigest()
        self.process_service.append_fact(
            process_id=intent.process_id,
            event_id=f"planner-stale:{event_digest}",
            event_type="project.orchestrator.decision_stale",
            expected_version=process.version,
            expected_event_sequence=process.last_event_sequence,
            subject_type="orchestration_decision",
            subject_id=decision_id,
            initiated_by="service:project-orchestrator",
            executed_as="service:project-orchestrator",
            correlation_id=f"planner-intent:{intent.planner_intent_id}",
            causation_id=run_id,
            payload={
                "decision_id": decision_id,
                "planner_intent_id": intent.planner_intent_id,
                "based_on_process_version": intent.based_on_process_version,
                "based_on_event_sequence": intent.based_on_event_sequence,
                "graph_snapshot_digest": intent.graph_snapshot_digest,
                "current_graph_snapshot_digest": current_digest,
            },
        )

    def _intent_from_correlation(self, run):
        prefix = "planner-intent:"
        correlation = getattr(run, "correlation_id", "")
        if not correlation.startswith(prefix):
            return None
        intent_id = correlation[len(prefix) :]
        try:
            return self.intent_service.get(intent_id)
        except Exception:  # noqa: BLE001
            return None

    def _find_run(self, intent) -> str | None:
        correlation = f"planner-intent:{intent.planner_intent_id}"
        with self.intent_service.repository.engine.connect() as connection:
            return connection.execute(
                select(AGENT_RUNS.c.run_id).where(
                    and_(
                        AGENT_RUNS.c.tenant_id == intent.owner_team_id,
                        AGENT_RUNS.c.correlation_id == correlation,
                    )
                )
            ).scalar_one_or_none()

    @staticmethod
    def _decision_id(intent_id: str) -> str:
        digest = hashlib.sha256(intent_id.encode()).hexdigest()
        return f"planner-decision:{digest}"


__all__ = ["ProjectPlannerProjection"]
