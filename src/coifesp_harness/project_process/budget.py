from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ..errors import GovernanceConflictError


class ProjectBudgetExhausted(GovernanceConflictError):
    pass


class BudgetGateDecision(StrEnum):
    INCREASE_BUDGET = "INCREASE_BUDGET"
    REDUCE_SCOPE = "REDUCE_SCOPE"
    TERMINATE = "TERMINATE"


class ProjectExecutionReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class ProjectExecutionPolicy:
    policy_id: str
    project_id: str
    max_agent_runs: int
    max_total_tokens: int
    max_model_cost_microusd: int
    max_replans: int
    max_generated_tasks: int
    max_active_agent_runs: int
    max_active_runs_per_team: int
    max_specialist_depth: int
    max_specialist_runs_per_task: int
    deadline_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        bounded = (
            self.max_agent_runs,
            self.max_total_tokens,
            self.max_model_cost_microusd,
            self.max_replans,
            self.max_generated_tasks,
            self.max_active_agent_runs,
            self.max_active_runs_per_team,
            self.max_specialist_depth,
            self.max_specialist_runs_per_task,
        )
        if any(value < 0 for value in bounded) or self.version < 1:
            raise ValueError("project execution policy limits are invalid")


@dataclass(frozen=True, slots=True)
class ProjectExecutionUsage:
    process_id: str
    project_id: str
    agent_runs_started: int
    agent_runs_completed: int
    total_tokens: int
    model_cost_microusd: int
    replan_count: int
    generated_task_count: int
    active_agent_runs: int
    version: int

    def __post_init__(self) -> None:
        counters = (
            self.agent_runs_started,
            self.agent_runs_completed,
            self.total_tokens,
            self.model_cost_microusd,
            self.replan_count,
            self.generated_task_count,
            self.active_agent_runs,
        )
        if any(value < 0 for value in counters) or self.version < 1:
            raise ValueError("project execution usage is invalid")
        if self.agent_runs_completed > self.agent_runs_started:
            raise ValueError("completed project runs exceed started runs")
        if self.active_agent_runs > self.agent_runs_started - self.agent_runs_completed:
            raise ValueError("active project runs exceed unsettled runs")


@dataclass(frozen=True, slots=True)
class ProjectExecutionReservation:
    reservation_id: str
    reservation_key: str
    process_id: str
    project_id: str
    work_node_id: str
    team_id: str
    policy_id: str
    policy_version: int
    execution_attempt: int
    specialist_depth: int
    reserved_tokens: int
    reserved_model_cost_microusd: int
    agent_run_id: str | None
    status: ProjectExecutionReservationStatus
    created_at: datetime
    settled_at: datetime | None
    terminal_event_id: str | None
    terminal_total_tokens: int | None
    terminal_model_cost_microusd: int | None

    def __post_init__(self) -> None:
        if (
            self.policy_version < 1
            or self.execution_attempt < 1
            or self.specialist_depth < 0
            or self.reserved_tokens < 0
            or self.reserved_model_cost_microusd < 0
        ):
            raise ValueError("project execution reservation is invalid")
        if self.status is ProjectExecutionReservationStatus.RESERVED:
            if self.settled_at is not None:
                raise ValueError("reserved execution cannot have a settlement timestamp")
        elif self.settled_at is None:
            raise ValueError("terminal execution reservation requires a settlement timestamp")


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    team_active_runs: int
    specialist_depth: int = 0
    specialist_runs_for_task: int = 0
    unsettled_reserved_tokens: int = 0
    unsettled_reserved_model_cost_microusd: int = 0
    requested_tokens: int = 0
    requested_model_cost_microusd: int = 0
    now: datetime | None = None


class ProjectBudgetEvaluator:
    def check(
        self,
        *,
        policy: ProjectExecutionPolicy,
        usage: ProjectExecutionUsage,
        request: AdmissionRequest,
    ) -> None:
        now = request.now or datetime.now(UTC)
        checks = (
            (usage.agent_runs_started, policy.max_agent_runs, "agent run limit"),
            (usage.replan_count, policy.max_replans, "replan limit"),
            (usage.generated_task_count, policy.max_generated_tasks, "task limit"),
            (usage.active_agent_runs, policy.max_active_agent_runs, "active run limit"),
            (request.team_active_runs, policy.max_active_runs_per_team, "team run limit"),
        )
        for current, limit, label in checks:
            if current >= limit:
                raise ProjectBudgetExhausted(f"project {label} is exhausted")
        token_committed = usage.total_tokens + request.unsettled_reserved_tokens
        if (
            token_committed >= policy.max_total_tokens
            or token_committed + request.requested_tokens > policy.max_total_tokens
        ):
            raise ProjectBudgetExhausted("project token limit is exhausted")
        cost_committed = (
            usage.model_cost_microusd + request.unsettled_reserved_model_cost_microusd
        )
        if (
            cost_committed >= policy.max_model_cost_microusd
            or cost_committed + request.requested_model_cost_microusd
            > policy.max_model_cost_microusd
        ):
            raise ProjectBudgetExhausted("project cost limit is exhausted")
        if request.specialist_depth > policy.max_specialist_depth:
            raise ProjectBudgetExhausted("project specialist depth limit is exhausted")
        if (
            request.specialist_depth > 0
            and request.specialist_runs_for_task >= policy.max_specialist_runs_per_task
        ):
            raise ProjectBudgetExhausted("project specialist run limit is exhausted")
        if policy.deadline_at is not None and now >= policy.deadline_at:
            raise ProjectBudgetExhausted("project deadline is exhausted")
