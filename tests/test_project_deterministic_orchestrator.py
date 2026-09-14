from datetime import UTC, datetime

import pytest

from coifesp_harness.project_process.models import (
    ProjectProcess,
    ProjectProcessPhase,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
)
from coifesp_harness.project_process.orchestrator import (
    DeliveryOutcome,
    DeterministicAction,
    DeterministicProjectOrchestrator,
    DeterministicReason,
    IntegrationOutcome,
    VerificationOutcome,
)
from coifesp_harness.project_process.readiness import (
    ReadinessEvaluation,
    WorkItemSnapshot,
    WorkReadiness,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def process(
    phase=ProjectProcessPhase.EXECUTION,
    status=ProjectProcessStatus.READY,
    wait_reason=ProjectProcessWaitReason.NONE,
):
    return ProjectProcess(
        process_id="process-a",
        project_id="project-a",
        phase=phase,
        status=status,
        wait_reason=wait_reason,
        version=5,
        root_goal_id="goal-a",
        active_plan_id="plan-a",
        execution_policy_id="policy-a",
        execution_policy_version=1,
        started_by="lead-a",
        started_at=NOW,
        updated_at=NOW,
        last_event_sequence=6,
        last_orchestration_sequence=5,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        completed_at=None,
    )


def evaluation(*, ready=(), verification_ready=False):
    return ReadinessEvaluation(
        ready_work=tuple(WorkReadiness(item, True) for item in ready),
        blocked_work=(),
        all_work_terminal=False,
        verification_ready=verification_ready,
    )


def test_human_wait_takes_priority_over_ready_work():
    orchestrator = DeterministicProjectOrchestrator()
    ready = evaluation(ready=(WorkItemSnapshot("task-a", team_id="team-a"),))
    assert orchestrator.decide(
        process=process(), readiness=ready, has_open_gate=True
    ).reason is DeterministicReason.HUMAN_APPROVAL
    waiting = process(
        status=ProjectProcessStatus.WAITING,
        wait_reason=ProjectProcessWaitReason.SCHEDULE,
    )
    assert orchestrator.decide(
        process=waiting, readiness=ready
    ).action is DeterministicAction.SLEEP


def test_execution_dispatches_only_one_ready_work_per_cycle():
    decision = DeterministicProjectOrchestrator().decide(
        process=process(),
        readiness=evaluation(
            ready=(
                WorkItemSnapshot("task-a", team_id="team-a"),
                WorkItemSnapshot("task-b", team_id="team-b"),
            )
        ),
    )
    assert decision.action is DeterministicAction.DISPATCH_WORK
    assert decision.work_id == "task-a"
    assert decision.transition_key == "work.dispatched"


def test_manual_submitted_work_reconciles_then_enters_verification():
    orchestrator = DeterministicProjectOrchestrator()
    ready = evaluation(verification_ready=True)
    first = orchestrator.decide(process=process(), readiness=ready)
    assert first.action is DeterministicAction.RECONCILE_RUNNING
    second = orchestrator.decide(
        process=process(status=ProjectProcessStatus.RUNNING), readiness=ready
    )
    assert second.action is DeterministicAction.ENTER_VERIFICATION
    assert second.transition_key == "all_required_work_submitted"


def test_active_work_waits_without_model_reasoning():
    decision = DeterministicProjectOrchestrator().decide(
        process=process(status=ProjectProcessStatus.RUNNING),
        readiness=evaluation(),
        has_active_operation=True,
    )
    assert decision.action is DeterministicAction.WAIT_FOR_WORK
    assert decision.reason is DeterministicReason.ACTIVE_WORK


def test_verification_integration_and_delivery_follow_explicit_facts():
    orchestrator = DeterministicProjectOrchestrator()
    empty = evaluation()
    verification = process(phase=ProjectProcessPhase.VERIFICATION)
    assert orchestrator.decide(
        process=verification, readiness=empty
    ).action is DeterministicAction.WAIT_FOR_VERIFICATION
    assert orchestrator.decide(
        process=verification,
        readiness=empty,
        verification_outcome=VerificationOutcome.PASSED,
    ).action is DeterministicAction.ENTER_INTEGRATION
    assert orchestrator.decide(
        process=verification,
        readiness=empty,
        verification_outcome=VerificationOutcome.FAILED,
    ).action is DeterministicAction.REOPEN_WORK
    integration = process(phase=ProjectProcessPhase.INTEGRATION)
    assert orchestrator.decide(
        process=integration,
        readiness=empty,
        integration_outcome=IntegrationOutcome.PASSED,
    ).action is DeterministicAction.ENTER_DELIVERY
    with pytest.raises(ValueError, match="IntegrationService"):
        orchestrator.decide(
            process=integration,
            readiness=empty,
            integration_outcome=IntegrationOutcome.FAILED,
        )
    delivery = process(phase=ProjectProcessPhase.DELIVERY)
    assert orchestrator.decide(
        process=delivery,
        readiness=empty,
        delivery_outcome=DeliveryOutcome.ACCEPTED,
    ).action is DeterministicAction.COMPLETE
    assert orchestrator.decide(
        process=delivery,
        readiness=empty,
        delivery_outcome=DeliveryOutcome.REJECTED,
    ).action is DeterministicAction.REOPEN_WORK
