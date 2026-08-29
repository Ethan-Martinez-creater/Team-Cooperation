from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, func, select

from ..errors import GovernanceConflictError
from .budget import (
    AdmissionRequest,
    ProjectBudgetEvaluator,
    ProjectBudgetExhausted,
    ProjectExecutionPolicy,
    ProjectExecutionReservationStatus,
)
from .models import ProjectProcessStatus
from .repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_RESERVATIONS,
    PROJECT_EXECUTION_USAGE,
    SQLAlchemyProjectProcessRepository,
)


class ProjectExecutionBudgetService:
    """Atomic project-level admission and terminal usage accounting."""

    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))
        self.evaluator = ProjectBudgetEvaluator()

    def reserve(
        self,
        *,
        reservation_id: str,
        reservation_key: str,
        process_id: str,
        work_node_id: str,
        team_id: str,
        execution_attempt: int,
        expected_usage_version: int,
        specialist_depth: int = 0,
        reserved_tokens: int = 0,
        reserved_model_cost_microusd: int = 0,
    ):
        if not all((reservation_id, reservation_key, process_id, work_node_id, team_id)):
            raise ValueError("project reservation identity is required")
        if (
            execution_attempt < 1
            or specialist_depth < 0
            or reserved_tokens < 0
            or reserved_model_cost_microusd < 0
        ):
            raise ValueError("project reservation attempt is invalid")
        now = self.clock()
        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
            usage_row = (
                connection.execute(
                    select(PROJECT_EXECUTION_USAGE)
                    .where(PROJECT_EXECUTION_USAGE.c.process_id == process_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            matches = (
                connection.execute(
                    select(PROJECT_EXECUTION_RESERVATIONS).where(
                        (PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == reservation_id)
                        | (PROJECT_EXECUTION_RESERVATIONS.c.reservation_key == reservation_key)
                    )
                )
                .mappings()
                .all()
            )
            expected = {
                "reservation_id": reservation_id,
                "reservation_key": reservation_key,
                "process_id": process_id,
                "project_id": process.project_id,
                "work_node_id": work_node_id,
                "team_id": team_id,
                "policy_id": process.execution_policy_id,
                "policy_version": process.execution_policy_version,
                "execution_attempt": execution_attempt,
                "specialist_depth": specialist_depth,
                "reserved_tokens": reserved_tokens,
                "reserved_model_cost_microusd": reserved_model_cost_microusd,
            }
            if len(matches) > 1:
                raise GovernanceConflictError(
                    "project reservation id and idempotency key refer to different reservations"
                )
            existing = matches[0] if matches else None
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise GovernanceConflictError(
                        "project reservation idempotency key was reused with different content"
                    )
                return self.repository._reservation(existing), self.repository.usage(
                    connection, process_id
                )
            if process.status in {
                ProjectProcessStatus.WAITING,
                ProjectProcessStatus.BLOCKED,
                ProjectProcessStatus.FAILED,
                ProjectProcessStatus.CANCELLED,
                ProjectProcessStatus.COMPLETED,
            }:
                raise GovernanceConflictError("project process cannot admit work")
            if usage_row["version"] != expected_usage_version:
                raise GovernanceConflictError("project execution usage version is stale")
            policy_row = (
                connection.execute(
                    select(PROJECT_EXECUTION_POLICIES).where(
                        and_(
                            PROJECT_EXECUTION_POLICIES.c.policy_id
                            == process.execution_policy_id,
                            PROJECT_EXECUTION_POLICIES.c.version
                            == process.execution_policy_version,
                        )
                    )
                )
                .mappings()
                .one()
            )
            team_active = connection.execute(
                select(func.count()).select_from(PROJECT_EXECUTION_RESERVATIONS).where(
                    and_(
                        PROJECT_EXECUTION_RESERVATIONS.c.process_id == process_id,
                        PROJECT_EXECUTION_RESERVATIONS.c.team_id == team_id,
                        PROJECT_EXECUTION_RESERVATIONS.c.status
                        == ProjectExecutionReservationStatus.RESERVED.value,
                    )
                )
            ).scalar_one()
            specialist_runs = connection.execute(
                select(func.count()).select_from(PROJECT_EXECUTION_RESERVATIONS).where(
                    and_(
                        PROJECT_EXECUTION_RESERVATIONS.c.process_id == process_id,
                        PROJECT_EXECUTION_RESERVATIONS.c.work_node_id == work_node_id,
                        PROJECT_EXECUTION_RESERVATIONS.c.specialist_depth > 0,
                    )
                )
            ).scalar_one()
            reserved_sums = connection.execute(
                select(
                    func.coalesce(func.sum(PROJECT_EXECUTION_RESERVATIONS.c.reserved_tokens), 0),
                    func.coalesce(
                        func.sum(
                            PROJECT_EXECUTION_RESERVATIONS.c.reserved_model_cost_microusd
                        ),
                        0,
                    ),
                ).where(
                    and_(
                        PROJECT_EXECUTION_RESERVATIONS.c.process_id == process_id,
                        PROJECT_EXECUTION_RESERVATIONS.c.status
                        == ProjectExecutionReservationStatus.RESERVED.value,
                    )
                )
            ).one()
            policy = self._policy(policy_row)
            usage = self.repository.usage(connection, process_id)
            self.evaluator.check(
                policy=policy,
                usage=usage,
                request=AdmissionRequest(
                    team_active_runs=team_active,
                    specialist_depth=specialist_depth,
                    specialist_runs_for_task=specialist_runs,
                    unsettled_reserved_tokens=reserved_sums[0],
                    unsettled_reserved_model_cost_microusd=reserved_sums[1],
                    requested_tokens=reserved_tokens,
                    requested_model_cost_microusd=reserved_model_cost_microusd,
                    now=now,
                ),
            )
            row = {
                **expected,
                "status": ProjectExecutionReservationStatus.RESERVED.value,
                "created_at": now,
                "settled_at": None,
                "agent_run_id": None,
                "terminal_event_id": None,
                "terminal_total_tokens": None,
                "terminal_model_cost_microusd": None,
            }
            connection.execute(PROJECT_EXECUTION_RESERVATIONS.insert().values(**row))
            updated = connection.execute(
                PROJECT_EXECUTION_USAGE.update()
                .where(
                    and_(
                        PROJECT_EXECUTION_USAGE.c.process_id == process_id,
                        PROJECT_EXECUTION_USAGE.c.version == expected_usage_version,
                    )
                )
                .values(
                    agent_runs_started=usage.agent_runs_started + 1,
                    active_agent_runs=usage.active_agent_runs + 1,
                    version=usage.version + 1,
                    updated_at=now,
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project execution usage version is stale")
            return self.repository._reservation(row), self.repository.usage(connection, process_id)

    def reserve_or_open_budget_gate(
        self,
        *,
        human_gate_service,
        created_by: str,
        correlation_id: str,
        **reservation,
    ):
        """Admit work or atomically persist the exhausted fact + budget Gate."""
        try:
            admitted, usage = self.reserve(**reservation)
            return admitted, usage, None
        except ProjectBudgetExhausted as error:
            process_id = reservation["process_id"]
            with self.repository.transaction() as connection:
                process = self.repository.process(connection, process_id)
            gate_id = (
                f"gate:budget:{process_id}:policy:{process.execution_policy_version}:"
                f"usage:{reservation['expected_usage_version']}"
            )
            gate, _, _ = human_gate_service.create_gate(
                gate_id=gate_id,
                process_id=process_id,
                gate_type="BUDGET",
                subject_type="project_execution_policy",
                subject_id=process.execution_policy_id,
                required_roles=("owner", "admin", "lead"),
                allowed_decisions=("INCREASE_BUDGET", "REDUCE_SCOPE", "TERMINATE"),
                reason=str(error),
                created_by=created_by,
                event_id=f"event:{gate_id}:opened",
                expected_process_version=process.version,
                expected_event_sequence=process.last_event_sequence,
                correlation_id=correlation_id,
                cause_event_type="project.budget.exhausted",
                cause_subject_type="project_execution_policy",
                cause_subject_id=process.execution_policy_id,
                cause_payload={
                    "reason": str(error),
                    "policy_version": process.execution_policy_version,
                    "usage_version": reservation["expected_usage_version"],
                },
            )
            return None, None, gate

    def bind_agent_run(self, *, reservation_id: str, agent_run_id: str):
        if not agent_run_id:
            raise ValueError("agent run id is required")
        with self.repository.transaction() as connection:
            row = (
                connection.execute(
                    select(PROJECT_EXECUTION_RESERVATIONS).where(
                        PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == reservation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise GovernanceConflictError("project execution reservation is unavailable")
            if row["agent_run_id"] is not None:
                if row["agent_run_id"] != agent_run_id:
                    raise GovernanceConflictError("project reservation is bound to another run")
                return self.repository._reservation(row)
            if row["status"] != ProjectExecutionReservationStatus.RESERVED.value:
                raise GovernanceConflictError("terminal project reservation cannot bind a run")
            connection.execute(
                PROJECT_EXECUTION_RESERVATIONS.update()
                .where(
                    and_(
                        PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == reservation_id,
                        PROJECT_EXECUTION_RESERVATIONS.c.agent_run_id.is_(None),
                        PROJECT_EXECUTION_RESERVATIONS.c.status
                        == ProjectExecutionReservationStatus.RESERVED.value,
                    )
                )
                .values(agent_run_id=agent_run_id)
            )
            return self.repository.reservation(connection, reservation_id)

    def settle(
        self,
        *,
        reservation_id: str,
        terminal_event_id: str,
        agent_run_id: str,
        total_tokens: int,
        model_cost_microusd: int,
        expected_usage_version: int,
    ):
        if total_tokens < 0 or model_cost_microusd < 0:
            raise ValueError("project terminal usage is invalid")
        return self._finish(
            reservation_id=reservation_id,
            terminal_event_id=terminal_event_id,
            agent_run_id=agent_run_id,
            total_tokens=total_tokens,
            model_cost_microusd=model_cost_microusd,
            expected_usage_version=expected_usage_version,
            target=ProjectExecutionReservationStatus.SETTLED,
        )

    def release(
        self,
        *,
        reservation_id: str,
        release_event_id: str,
        expected_usage_version: int,
        agent_run_id: str | None = None,
    ):
        return self._finish(
            reservation_id=reservation_id,
            terminal_event_id=release_event_id,
            agent_run_id=agent_run_id,
            total_tokens=None,
            model_cost_microusd=None,
            expected_usage_version=expected_usage_version,
            target=ProjectExecutionReservationStatus.RELEASED,
        )

    def _finish(
        self,
        *,
        reservation_id: str,
        terminal_event_id: str,
        agent_run_id: str | None,
        total_tokens: int | None,
        model_cost_microusd: int | None,
        expected_usage_version: int,
        target: ProjectExecutionReservationStatus,
    ):
        now = self.clock()
        with self.repository.transaction() as connection:
            row = (
                connection.execute(
                    select(PROJECT_EXECUTION_RESERVATIONS)
                    .where(PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == reservation_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise GovernanceConflictError("project execution reservation is unavailable")
            if target is ProjectExecutionReservationStatus.SETTLED:
                if row["agent_run_id"] is None or row["agent_run_id"] != agent_run_id:
                    raise GovernanceConflictError(
                        "project reservation terminal event does not match its agent run"
                    )
                if total_tokens is not None and total_tokens > row["reserved_tokens"]:
                    raise GovernanceConflictError("agent run exceeded its reserved token budget")
                if (
                    model_cost_microusd is not None
                    and model_cost_microusd > row["reserved_model_cost_microusd"]
                ):
                    raise GovernanceConflictError("agent run exceeded its reserved cost budget")
            elif row["agent_run_id"] != agent_run_id:
                raise GovernanceConflictError("project reservation release run does not match")
            if row["status"] != ProjectExecutionReservationStatus.RESERVED.value:
                exact = (
                    row["status"] == target.value
                    and row["terminal_event_id"] == terminal_event_id
                    and row["terminal_total_tokens"] == total_tokens
                    and row["terminal_model_cost_microusd"] == model_cost_microusd
                )
                if not exact:
                    raise GovernanceConflictError(
                        "project reservation terminal retry conflicts with stored result"
                    )
                return self.repository._reservation(row), self.repository.usage(
                    connection, row["process_id"]
                )
            usage = self.repository.usage(connection, row["process_id"])
            if usage.version != expected_usage_version:
                raise GovernanceConflictError("project execution usage version is stale")
            connection.execute(
                PROJECT_EXECUTION_RESERVATIONS.update()
                .where(PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == reservation_id)
                .values(
                    status=target.value,
                    settled_at=now,
                    terminal_event_id=terminal_event_id,
                    terminal_total_tokens=total_tokens,
                    terminal_model_cost_microusd=model_cost_microusd,
                )
            )
            values = {
                "active_agent_runs": usage.active_agent_runs - 1,
                "version": usage.version + 1,
                "updated_at": now,
            }
            if target is ProjectExecutionReservationStatus.SETTLED:
                values.update(
                    agent_runs_completed=usage.agent_runs_completed + 1,
                    total_tokens=usage.total_tokens + int(total_tokens or 0),
                    model_cost_microusd=(
                        usage.model_cost_microusd + int(model_cost_microusd or 0)
                    ),
                )
            updated = connection.execute(
                PROJECT_EXECUTION_USAGE.update()
                .where(
                    and_(
                        PROJECT_EXECUTION_USAGE.c.process_id == row["process_id"],
                        PROJECT_EXECUTION_USAGE.c.version == expected_usage_version,
                        PROJECT_EXECUTION_USAGE.c.active_agent_runs > 0,
                    )
                )
                .values(**values)
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project execution usage version is stale")
            stored = self.repository.reservation(connection, reservation_id)
            return stored, self.repository.usage(connection, row["process_id"])

    @staticmethod
    def _policy(row) -> ProjectExecutionPolicy:
        deadline = row["deadline_at"]
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return ProjectExecutionPolicy(
            row["policy_id"], row["project_id"], row["max_agent_runs"],
            row["max_total_tokens"], row["max_model_cost_microusd"], row["max_replans"],
            row["max_generated_tasks"], row["max_active_agent_runs"],
            row["max_active_runs_per_team"], row["max_specialist_depth"],
            row["max_specialist_runs_per_task"], deadline, row["version"],
        )
