from datetime import UTC, datetime

import pytest

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.project_process import (
    MAIN_TRANSITIONS,
    ProjectProcess,
    ProjectTransitionGuard,
    select_blocked_wait_reason,
    validate_process_state,
)
from coifesp_harness.project_process import (
    ProjectProcessPhase as Phase,
)
from coifesp_harness.project_process import (
    ProjectProcessStatus as Status,
)
from coifesp_harness.project_process import (
    ProjectProcessWaitReason as WaitReason,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _process(
    *, phase=Phase.INTAKE, status=Status.WAITING, reason=WaitReason.HUMAN_INPUT, version=1
):
    return ProjectProcess(
        process_id="process-1",
        project_id="project-1",
        phase=phase,
        status=status,
        wait_reason=reason,
        version=version,
        root_goal_id=None,
        active_plan_id=None,
        execution_policy_id="policy-1",
        execution_policy_version=1,
        started_by="lead-1",
        started_at=NOW,
        updated_at=NOW,
        last_event_sequence=0,
        last_orchestration_sequence=0,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        completed_at=None,
    )


def test_main_transition_chain_is_executable_and_versioned():
    guard = ProjectTransitionGuard(clock=lambda: NOW)
    process = _process()
    events = [
        "goal.confirmed",
        "analysis.started",
        "analysis.completed",
        "plan.approved",
        "work.dispatched",
        "all_required_work_submitted",
        "verification.passed",
        "integration.passed",
        "delivery.accepted",
    ]
    for event in events:
        process = guard.transition(
            process=process, event_type=event, expected_version=process.version
        )
    assert process.phase is Phase.TERMINAL
    assert process.status is Status.COMPLETED
    assert process.version == 1 + len(events)
    assert process.last_event_sequence == len(events)
    assert process.completed_at == NOW


@pytest.mark.parametrize("transition", MAIN_TRANSITIONS)
def test_every_matrix_entry_produces_its_declared_target(transition):
    process = _process(
        phase=transition.from_phase,
        status=transition.from_status,
        reason=transition.from_wait_reason,
    )
    result = ProjectTransitionGuard(clock=lambda: NOW).transition(
        process=process, event_type=transition.event_type, expected_version=1
    )
    assert (result.phase, result.status, result.wait_reason) == (
        transition.to_phase,
        transition.to_status,
        transition.to_wait_reason,
    )


def test_stale_illegal_and_terminal_transitions_fail_closed():
    guard = ProjectTransitionGuard(clock=lambda: NOW)
    with pytest.raises(GovernanceConflictError, match="stale"):
        guard.transition(process=_process(), event_type="goal.confirmed", expected_version=2)
    with pytest.raises(GovernanceConflictError, match="not allowed"):
        guard.transition(process=_process(), event_type="delivery.accepted", expected_version=1)
    terminal = _process(phase=Phase.TERMINAL, status=Status.COMPLETED, reason=WaitReason.NONE)
    with pytest.raises(GovernanceConflictError, match="immutable"):
        guard.transition(process=terminal, event_type="scope.changed", expected_version=1)


@pytest.mark.parametrize(
    ("phase", "status", "reason"),
    [
        (Phase.EXECUTION, Status.WAITING, WaitReason.NONE),
        (Phase.EXECUTION, Status.READY, WaitReason.HUMAN_INPUT),
        (Phase.TERMINAL, Status.READY, WaitReason.NONE),
        (Phase.EXECUTION, Status.COMPLETED, WaitReason.NONE),
    ],
)
def test_invalid_state_combinations_are_rejected(phase, status, reason):
    with pytest.raises(ValueError):
        validate_process_state(phase, status, reason)


def test_verification_and_delivery_rework_are_explicit_only():
    guard = ProjectTransitionGuard(clock=lambda: NOW)
    verification = _process(phase=Phase.VERIFICATION, status=Status.READY, reason=WaitReason.NONE)
    rework = guard.transition(
        process=verification, event_type="verification.failed", expected_version=1
    )
    assert (rework.phase, rework.status) == (Phase.EXECUTION, Status.READY)
    delivery = _process(phase=Phase.DELIVERY, status=Status.READY, reason=WaitReason.NONE)
    rejected = guard.transition(
        process=delivery, event_type="delivery.rejected", expected_version=1
    )
    assert (rejected.phase, rejected.status) == (Phase.EXECUTION, Status.READY)


def test_self_transition_is_not_a_versioned_transition():
    from coifesp_harness.project_process import ProjectTransition

    with pytest.raises(ValueError, match="must change"):
        ProjectTransitionGuard(
            (
                ProjectTransition(
                    Phase.EXECUTION,
                    Status.RUNNING,
                    WaitReason.NONE,
                    "work.redispatched",
                    Phase.EXECUTION,
                    Status.RUNNING,
                    WaitReason.NONE,
                ),
            )
        )


def test_blocked_reason_priority_is_deterministic():
    assert (
        select_blocked_wait_reason(
            [WaitReason.VERIFICATION, WaitReason.DEPENDENCY, WaitReason.TEAM_RESPONSE]
        )
        is WaitReason.TEAM_RESPONSE
    )
    assert (
        select_blocked_wait_reason([WaitReason.VERIFICATION, WaitReason.DEPENDENCY])
        is WaitReason.DEPENDENCY
    )
