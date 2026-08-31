from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ..errors import GovernanceConflictError
from .models import (
    TERMINAL_STATUSES,
    ProjectProcess,
    validate_process_state,
)
from .models import (
    ProjectProcessPhase as Phase,
)
from .models import (
    ProjectProcessStatus as Status,
)
from .models import (
    ProjectProcessWaitReason as WaitReason,
)


@dataclass(frozen=True, slots=True)
class ProjectTransition:
    from_phase: Phase
    from_status: Status
    from_wait_reason: WaitReason
    event_type: str
    to_phase: Phase
    to_status: Status
    to_wait_reason: WaitReason


MAIN_TRANSITIONS = (
    ProjectTransition(
        Phase.INTAKE,
        Status.WAITING,
        WaitReason.HUMAN_INPUT,
        "goal.confirmed",
        Phase.ANALYSIS,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.ANALYSIS,
        Status.READY,
        WaitReason.NONE,
        "analysis.started",
        Phase.ANALYSIS,
        Status.RUNNING,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.ANALYSIS,
        Status.RUNNING,
        WaitReason.NONE,
        "analysis.completed",
        Phase.PLANNING,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.PLANNING,
        Status.READY,
        WaitReason.NONE,
        "plan.approved",
        Phase.EXECUTION,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.EXECUTION,
        Status.READY,
        WaitReason.NONE,
        "work.dispatched",
        Phase.EXECUTION,
        Status.RUNNING,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.EXECUTION,
        Status.RUNNING,
        WaitReason.NONE,
        "all_required_work_submitted",
        Phase.VERIFICATION,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.VERIFICATION,
        Status.READY,
        WaitReason.NONE,
        "verification.failed",
        Phase.EXECUTION,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.VERIFICATION,
        Status.READY,
        WaitReason.NONE,
        "verification.passed",
        Phase.INTEGRATION,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.INTEGRATION,
        Status.READY,
        WaitReason.NONE,
        "integration.passed",
        Phase.DELIVERY,
        Status.READY,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.DELIVERY,
        Status.READY,
        WaitReason.NONE,
        "delivery.accepted",
        Phase.TERMINAL,
        Status.COMPLETED,
        WaitReason.NONE,
    ),
    ProjectTransition(
        Phase.DELIVERY,
        Status.READY,
        WaitReason.NONE,
        "delivery.rejected",
        Phase.EXECUTION,
        Status.READY,
        WaitReason.NONE,
    ),
)


INTEGRATION_FAILURE_TRANSITIONS = (
    ProjectTransition(Phase.INTEGRATION, Status.READY, WaitReason.NONE,
                      "integration.failed", Phase.EXECUTION, Status.READY, WaitReason.NONE),
)


class ProjectTransitionGuard:
    def __init__(self, transitions=None, *, clock=None) -> None:
        if transitions is None:
            transitions = MAIN_TRANSITIONS + INTEGRATION_FAILURE_TRANSITIONS
        self._clock = clock or (lambda: datetime.now(UTC))
        self._matrix = {}
        for item in transitions:
            validate_process_state(item.from_phase, item.from_status, item.from_wait_reason)
            validate_process_state(item.to_phase, item.to_status, item.to_wait_reason)
            if (
                item.from_phase,
                item.from_status,
                item.from_wait_reason,
            ) == (item.to_phase, item.to_status, item.to_wait_reason):
                raise ValueError("project transition must change process state")
            key = (item.from_phase, item.from_status, item.from_wait_reason, item.event_type)
            if key in self._matrix:
                raise ValueError("duplicate project transition")
            self._matrix[key] = item

    def transition(
        self, *, process: ProjectProcess, event_type: str, expected_version: int
    ) -> ProjectProcess:
        validate_process_state(process.phase, process.status, process.wait_reason)
        if process.version != expected_version:
            raise GovernanceConflictError("project process version is stale")
        if process.status in TERMINAL_STATUSES:
            raise GovernanceConflictError("terminal project process is immutable")
        item = self._matrix.get((process.phase, process.status, process.wait_reason, event_type))
        if item is None:
            raise GovernanceConflictError("project process transition is not allowed")
        now = self._clock()
        return replace(
            process,
            phase=item.to_phase,
            status=item.to_status,
            wait_reason=item.to_wait_reason,
            version=process.version + 1,
            last_event_sequence=process.last_event_sequence + 1,
            updated_at=now,
            completed_at=now if item.to_status in TERMINAL_STATUSES else None,
        )

    def enter_human_wait(
        self,
        *,
        process: ProjectProcess,
        reason: WaitReason,
        expected_version: int,
    ) -> tuple[ProjectProcess, str | None]:
        """Apply the frozen non-main-chain Human wait selectors.

        Opening an additional lower-priority human object is a fact without a
        second state migration. BLOCKED and non-human waits are not overwritten;
        the deterministic orchestrator must resolve those conditions explicitly.
        """
        validate_process_state(process.phase, process.status, process.wait_reason)
        if process.version != expected_version:
            raise GovernanceConflictError("project process version is stale")
        if process.status in TERMINAL_STATUSES:
            raise GovernanceConflictError("terminal project process is immutable")
        if reason not in {WaitReason.HUMAN_INPUT, WaitReason.HUMAN_APPROVAL}:
            raise ValueError("human wait reason is invalid")
        if process.status is Status.BLOCKED:
            raise GovernanceConflictError("blocked project process cannot hide its blocker")
        if process.status is Status.WAITING and process.wait_reason not in {
            WaitReason.HUMAN_INPUT,
            WaitReason.HUMAN_APPROVAL,
        }:
            raise GovernanceConflictError("project process has another durable wait")
        priority = {WaitReason.HUMAN_INPUT: 1, WaitReason.HUMAN_APPROVAL: 2}
        if (
            process.status is Status.WAITING
            and priority[process.wait_reason] >= priority[reason]
        ):
            return process, None
        now = self._clock()
        selector = (
            "human_approval.opened"
            if reason is WaitReason.HUMAN_APPROVAL
            else "human_input.opened"
        )
        return (
            replace(
                process,
                status=Status.WAITING,
                wait_reason=reason,
                version=process.version + 1,
                last_event_sequence=process.last_event_sequence + 1,
                updated_at=now,
            ),
            selector,
        )
