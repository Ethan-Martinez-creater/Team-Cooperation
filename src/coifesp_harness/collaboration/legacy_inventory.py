"""Read-only inventory used to retire the legacy Governance execution path."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import Engine, func, or_, select

from ..execution.repository import EXECUTION_TASKS
from .repository import GOVERNANCE_ASSIGNMENTS, GOVERNANCE_PLANS, GOVERNANCE_PROGRAMS

_ACTIVE_PLAN_STATES = ("draft", "discussion", "approved")
_ACTIVE_ASSIGNMENT_STATES = ("proposed", "accepted", "in_progress", "submitted")
_ACTIVE_EXECUTION_STATES = ("queued", "leased", "running", "retry_wait")


@dataclass(frozen=True, slots=True)
class LegacyGovernanceInventory:
    program_count: int
    plan_count: int
    active_plan_count: int
    assignment_count: int
    active_assignment_count: int
    execution_count: int
    active_execution_count: int

    @property
    def has_active_work(self) -> bool:
        return any(
            (
                self.active_plan_count,
                self.active_assignment_count,
                self.active_execution_count,
            )
        )

    @property
    def is_retired(self) -> bool:
        return not any(
            (
                self.program_count,
                self.plan_count,
                self.assignment_count,
                self.execution_count,
            )
        )

    def as_dict(self) -> dict[str, int | bool]:
        return {
            **asdict(self),
            "has_active_work": self.has_active_work,
            "is_retired": self.is_retired,
        }


def collect_legacy_governance_inventory(engine: Engine) -> LegacyGovernanceInventory:
    """Count legacy objects without mutating or bypassing tenant protections."""

    legacy_execution = or_(
        EXECUTION_TASKS.c.program_id.is_not(None),
        EXECUTION_TASKS.c.assignment_id.is_not(None),
    )
    with engine.connect() as connection:
        return LegacyGovernanceInventory(
            program_count=connection.scalar(
                select(func.count()).select_from(GOVERNANCE_PROGRAMS)
            )
            or 0,
            plan_count=connection.scalar(
                select(func.count()).select_from(GOVERNANCE_PLANS)
            )
            or 0,
            active_plan_count=connection.scalar(
                select(func.count())
                .select_from(GOVERNANCE_PLANS)
                .where(GOVERNANCE_PLANS.c.state.in_(_ACTIVE_PLAN_STATES))
            )
            or 0,
            assignment_count=connection.scalar(
                select(func.count()).select_from(GOVERNANCE_ASSIGNMENTS)
            )
            or 0,
            active_assignment_count=connection.scalar(
                select(func.count())
                .select_from(GOVERNANCE_ASSIGNMENTS)
                .where(GOVERNANCE_ASSIGNMENTS.c.state.in_(_ACTIVE_ASSIGNMENT_STATES))
            )
            or 0,
            execution_count=connection.scalar(
                select(func.count()).select_from(EXECUTION_TASKS).where(legacy_execution)
            )
            or 0,
            active_execution_count=connection.scalar(
                select(func.count())
                .select_from(EXECUTION_TASKS)
                .where(
                    legacy_execution,
                    EXECUTION_TASKS.c.status.in_(_ACTIVE_EXECUTION_STATES),
                )
            )
            or 0,
        )


__all__ = ["LegacyGovernanceInventory", "collect_legacy_governance_inventory"]
