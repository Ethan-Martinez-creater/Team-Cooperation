"""Replayable project-budget accounting for project Planner AgentRuns."""

from __future__ import annotations

import hashlib

from sqlalchemy import and_, select

from ..agent_runs.models import TERMINAL_RUN_STATES
from ..errors import GovernanceConflictError
from .budget_service import ProjectExecutionBudgetService
from .repository import PROJECT_EXECUTION_RESERVATIONS, PROJECT_PLANNER_INTENTS


class ProjectPlannerRunAccounting:
    def __init__(self, *, repository, run_repository) -> None:
        if repository.engine is not run_repository.engine:
            raise ValueError("Planner accounting requires a shared database engine")
        self.repository = repository
        self.runs = run_repository

    def on_run_terminal(self, run) -> bool:
        return self.settle(run_id=run.run_id)

    def settle(self, *, run_id: str) -> bool:
        with self.repository.transaction() as connection:
            binding = connection.execute(
                select(
                    PROJECT_PLANNER_INTENTS.c.process_id,
                    PROJECT_PLANNER_INTENTS.c.planner_intent_id,
                    PROJECT_PLANNER_INTENTS.c.owner_team_id,
                    PROJECT_PLANNER_INTENTS.c.run_id,
                    PROJECT_EXECUTION_RESERVATIONS.c.reservation_id,
                    PROJECT_EXECUTION_RESERVATIONS.c.status.label(
                        "reservation_status"
                    ),
                )
                .join(
                    PROJECT_EXECUTION_RESERVATIONS,
                    PROJECT_EXECUTION_RESERVATIONS.c.agent_run_id
                    == PROJECT_PLANNER_INTENTS.c.run_id,
                )
                .where(PROJECT_PLANNER_INTENTS.c.run_id == run_id)
            ).mappings().one_or_none()
            if binding is None:
                return False
            digest = hashlib.sha256(
                binding["planner_intent_id"].encode()
            ).hexdigest()
            expected_reservation = f"planner-budget:{digest[:40]}"
            if binding["reservation_id"] != expected_reservation:
                raise GovernanceConflictError(
                    "Planner Run is bound to an unexpected budget reservation"
                )
            run = self.runs.using_connection(connection).get(
                tenant_id=binding["owner_team_id"], run_id=run_id
            )
            if run.owner_principal_id != "service:project-orchestrator":
                raise GovernanceConflictError(
                    "terminal Planner Run owner does not match its durable intent"
                )
            if run.status not in TERMINAL_RUN_STATES:
                return False
            repository = self.repository.using_connection(connection)
            usage = repository.usage(connection, binding["process_id"])
            was_reserved = binding["reservation_status"] == "RESERVED"
            ProjectExecutionBudgetService(repository).settle(
                reservation_id=binding["reservation_id"],
                terminal_event_id=f"planner-terminal:{run_id}",
                agent_run_id=run_id,
                total_tokens=run.total_tokens,
                model_cost_microusd=run.model_cost_microusd,
                expected_usage_version=usage.version,
            )
            return was_reserved

    def replay_pending(self, _run_service=None) -> int:
        with self.repository.transaction() as connection:
            run_ids = connection.execute(
                select(PROJECT_PLANNER_INTENTS.c.run_id)
                .join(
                    PROJECT_EXECUTION_RESERVATIONS,
                    PROJECT_EXECUTION_RESERVATIONS.c.agent_run_id
                    == PROJECT_PLANNER_INTENTS.c.run_id,
                )
                .where(
                    and_(
                        PROJECT_PLANNER_INTENTS.c.run_id.is_not(None),
                        PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
                    )
                )
                .order_by(PROJECT_PLANNER_INTENTS.c.run_id)
            ).scalars().all()
        return sum(self.settle(run_id=run_id) for run_id in run_ids)

__all__ = ["ProjectPlannerRunAccounting"]
