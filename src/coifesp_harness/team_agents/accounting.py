"""Replayable terminal accounting for Harness-dispatched Team Agent runs."""

from __future__ import annotations

from sqlalchemy import and_, select

from ..agent_runs.models import TERMINAL_RUN_STATES
from ..capabilities.repository import CAPACITY_RESERVATIONS
from ..errors import GovernanceConflictError
from ..product.repository import PROJECT_AGENT_RUNS
from ..project_process.budget_service import ProjectExecutionBudgetService
from ..project_process.repository import (
    PROJECT_EXECUTION_RESERVATIONS,
    PROJECT_PROCESSES,
)
from ..project_process.service import ProjectProcessService
from .identity import ORCHESTRATOR_PRINCIPAL_ID


class TeamTaskRunAccounting:
    def __init__(self, *, repository, run_repository, capability_repository):
        if (repository.engine is not run_repository.engine
                or repository.engine is not capability_repository.engine):
            raise ValueError("task accounting requires a shared database engine")
        self.repository = repository
        self.runs = run_repository
        self.capabilities = capability_repository

    def on_run_terminal(self, run) -> bool:
        return self.settle(run_id=run.run_id)

    def settle(self, *, run_id: str) -> bool:
        with self.repository.transaction() as connection:
            binding = connection.execute(select(PROJECT_AGENT_RUNS).where(and_(
                PROJECT_AGENT_RUNS.c.run_id == run_id,
                PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
            ))).mappings().one_or_none()
            if binding is None:
                return False
            connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == binding["process_id"]
            ).with_for_update()).scalar_one()
            run = self.runs.using_connection(connection).get(
                tenant_id=binding["team_id"], run_id=run_id,
            )
            if run.owner_principal_id != binding["executed_as_principal_id"]:
                raise GovernanceConflictError("terminal run owner does not match task binding")
            if run.status not in TERMINAL_RUN_STATES:
                return False
            event_id = f"task-terminal:{run_id}"
            repository = self.repository.using_connection(connection)
            usage = repository.usage(connection, binding["process_id"])
            ProjectExecutionBudgetService(repository).settle(
                reservation_id=binding["project_budget_reservation_id"],
                terminal_event_id=event_id, agent_run_id=run_id,
                total_tokens=run.total_tokens,
                model_cost_microusd=run.model_cost_microusd,
                expected_usage_version=usage.version,
            )
            capabilities = self.capabilities.using_connection(connection)
            # The persisted machine binding is the release authority. Do not
            # require current capability availability or team membership: either
            # can legitimately change while a previously admitted run finishes.
            with capabilities.transaction(binding["team_id"]):
                capacity = connection.execute(select(CAPACITY_RESERVATIONS).where(and_(
                    CAPACITY_RESERVATIONS.c.provider_tenant_id == binding["team_id"],
                    CAPACITY_RESERVATIONS.c.reservation_id == binding["capacity_reservation_id"],
                )).with_for_update()).mappings().one_or_none()
                if capacity is None or capacity["created_by"] != ORCHESTRATOR_PRINCIPAL_ID:
                    raise GovernanceConflictError("task capacity reservation binding is invalid")
                if capacity["status"] != "released":
                    capabilities.release_reservation(
                        connection, provider_tenant_id=binding["team_id"],
                        reservation_id=binding["capacity_reservation_id"],
                        actor_tenant_id=binding["team_id"], actor_id=ORCHESTRATOR_PRINCIPAL_ID,
                    )
            process = repository.process(connection, binding["process_id"])
            ProjectProcessService(repository).append_fact(
                process_id=process.process_id, event_id=event_id,
                event_type=f"agent_run.{run.status.value}",
                expected_version=process.version,
                expected_event_sequence=process.last_event_sequence,
                subject_type="agent_run", subject_id=run_id,
                initiated_by=binding["initiated_by_principal_id"],
                executed_as=binding["executed_as_principal_id"],
                correlation_id=run.correlation_id,
                payload={
                    "run_id": run_id, "team_task_id": binding["team_task_id"],
                    "work_node_id": binding["work_node_id"], "status": run.status.value,
                    "total_tokens": run.total_tokens,
                    "model_cost_microusd": run.model_cost_microusd,
                },
            )
            return True

    def replay_pending(self, _run_service=None) -> int:
        """Recover terminal callbacks lost after the durable Run committed."""
        with self.repository.transaction() as connection:
            run_ids = connection.execute(select(PROJECT_AGENT_RUNS.c.run_id).join(
                PROJECT_EXECUTION_RESERVATIONS,
                PROJECT_EXECUTION_RESERVATIONS.c.reservation_id
                == PROJECT_AGENT_RUNS.c.project_budget_reservation_id,
            ).where(and_(
                PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
            )).order_by(PROJECT_AGENT_RUNS.c.run_id)).scalars().all()
        return sum(self.settle(run_id=run_id) for run_id in run_ids)
