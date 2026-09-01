"""Automatic, budget-governed launch of durable project Planner runs."""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from ..errors import ResourceNotFound
from .budget import ProjectBudgetExhausted
from .commands import ProjectPlannerIntentStatus
from .gates import ProjectGateStatus
from .models import ProjectProcessStatus
from .repository import PROJECT_EXECUTION_RESERVATIONS, PROJECT_GATES
from .runner import ProjectOrchestratorWorkerOutcome, ProjectOrchestratorWorkerStatus


class ProjectPlannerRunLauncher:
    def __init__(
        self,
        *,
        intent_service,
        work_graph_repository,
        run_service,
        budget_service,
        human_gate_service,
    ) -> None:
        engines = {
            id(intent_service.repository.engine),
            id(work_graph_repository.engine),
            id(run_service.repository.engine),
            id(budget_service.repository.engine),
            id(human_gate_service.repository.engine),
        }
        if len(engines) != 1:
            raise ValueError("Planner launcher requires one shared database engine")
        self.intents = intent_service
        self.graph = work_graph_repository
        self.runs = run_service
        self.budgets = budget_service
        self.gates = human_gate_service

    def process_once(self, *, worker_id: str, process_id: str | None = None):
        del worker_id
        for intent in self.intents.pending():
            if process_id is not None and intent.process_id != process_id:
                continue
            if self._has_open_gate(intent.process_id):
                continue
            if intent.status is ProjectPlannerIntentStatus.RUNNING and intent.run_id:
                try:
                    self.runs.repository.get(
                        tenant_id=intent.owner_team_id, run_id=intent.run_id
                    )
                except ResourceNotFound:
                    pass
                else:
                    # Projection/accounting own terminal runs. Any existing active
                    # Run must not keep the orchestration loop spinning on APPLIED.
                    continue
            process, graph = self._snapshot(intent)
            if process.status in {
                ProjectProcessStatus.WAITING,
                ProjectProcessStatus.BLOCKED,
            }:
                continue
            if process.status in {
                ProjectProcessStatus.COMPLETED,
                ProjectProcessStatus.FAILED,
                ProjectProcessStatus.CANCELLED,
            }:
                cancelled = self.intents.finish(
                    intent.planner_intent_id,
                    status=ProjectPlannerIntentStatus.CANCELLED,
                    error_code="process_terminal",
                )
                return ProjectOrchestratorWorkerOutcome(
                    ProjectOrchestratorWorkerStatus.STALE,
                    decision_id=cancelled.planner_intent_id,
                )
            if (
                process.version != intent.based_on_process_version
                or process.last_event_sequence != intent.based_on_event_sequence
                or graph.digest != intent.graph_snapshot_digest
            ):
                stale = self.intents.finish(
                    intent.planner_intent_id,
                    status=ProjectPlannerIntentStatus.STALE,
                    error_code="snapshot_stale",
                )
                return ProjectOrchestratorWorkerOutcome(
                    ProjectOrchestratorWorkerStatus.STALE,
                    decision_id=stale.planner_intent_id,
                )
            try:
                run = self.intents.launch(
                    intent=intent,
                    graph=graph,
                    run_service=self.runs,
                    budget_service=self.budgets,
                )
            except IntegrityError:
                # SQLite cannot honor SELECT FOR UPDATE and two replicas can
                # race on the deterministic reservation unique key.  Only
                # converge when the exact expected reservation committed;
                # unrelated integrity failures remain visible.
                reservation = self.intents.budget_reservation(intent)
                with self.intents.repository.transaction() as connection:
                    existing = connection.execute(
                        select(PROJECT_EXECUTION_RESERVATIONS.c.reservation_id).where(
                            PROJECT_EXECUTION_RESERVATIONS.c.reservation_id
                            == reservation["reservation_id"]
                        )
                    ).scalar_one_or_none()
                if existing is None:
                    raise
                fresh = self.intents.get(intent.planner_intent_id)
                run = self.intents.launch(
                    intent=fresh,
                    graph=graph,
                    run_service=self.runs,
                    budget_service=self.budgets,
                )
            except ProjectBudgetExhausted:
                with self.budgets.repository.transaction() as connection:
                    usage = self.budgets.repository.usage(connection, intent.process_id)
                reservation = self.intents.budget_reservation(intent)
                _, _, gate = self.budgets.reserve_or_open_budget_gate(
                    human_gate_service=self.gates,
                    created_by="service:project-orchestrator",
                    correlation_id=f"planner-intent:{intent.planner_intent_id}",
                    **reservation,
                    expected_usage_version=usage.version,
                )
                return ProjectOrchestratorWorkerOutcome(
                    ProjectOrchestratorWorkerStatus.RETRY,
                    decision_id=intent.planner_intent_id,
                    error="project Planner budget requires a human decision" if gate else None,
                )
            return ProjectOrchestratorWorkerOutcome(
                ProjectOrchestratorWorkerStatus.APPLIED,
                decision_id=intent.planner_intent_id,
                error=None if run is not None else "Planner Run was not launched",
            )
        return None

    def _snapshot(self, intent):
        with self.intents.repository.transaction() as connection:
            process = self.intents.repository.process(connection, intent.process_id)
            graph = self.graph.snapshot(connection, project_id=intent.project_id)
        return process, graph

    def _has_open_gate(self, process_id: str) -> bool:
        with self.intents.repository.engine.connect() as connection:
            return connection.execute(
                select(PROJECT_GATES.c.gate_id).where(
                    and_(
                        PROJECT_GATES.c.process_id == process_id,
                        PROJECT_GATES.c.status == ProjectGateStatus.OPEN.value,
                    )
                ).limit(1)
            ).scalar_one_or_none() is not None

__all__ = ["ProjectPlannerRunLauncher"]
