"""Deterministic Project Orchestrator decision ordering.

This module is intentionally free of model calls and persistence. It turns a
fenced process/readiness snapshot plus explicit verification, integration and
delivery facts into exactly one next action. A transactional runner applies
that action and records its command/events.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ProjectProcess, ProjectProcessPhase, ProjectProcessStatus
from .readiness import ReadinessEvaluation


class DeterministicAction(StrEnum):
    SLEEP = "sleep"
    DISPATCH_WORK = "dispatch_work"
    RECONCILE_RUNNING = "reconcile_running"
    ENTER_VERIFICATION = "enter_verification"
    WAIT_FOR_WORK = "wait_for_work"
    WAIT_FOR_VERIFICATION = "wait_for_verification"
    ENTER_INTEGRATION = "enter_integration"
    ASSEMBLE_INTEGRATION = "assemble_integration"
    WAIT_FOR_INTEGRATION = "wait_for_integration"
    ENTER_DELIVERY = "enter_delivery"
    WAIT_FOR_DELIVERY = "wait_for_delivery"
    REOPEN_WORK = "reopen_work"
    COMPLETE = "complete"


class DeterministicReason(StrEnum):
    TERMINAL = "TERMINAL"
    HUMAN_INPUT = "HUMAN_INPUT"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    DURABLE_WAIT = "DURABLE_WAIT"
    READY_WORK = "READY_WORK"
    ACTIVE_WORK = "ACTIVE_WORK"
    WORK_NOT_READY = "WORK_NOT_READY"
    ALL_REQUIRED_WORK_SUBMITTED = "ALL_REQUIRED_WORK_SUBMITTED"
    MANUAL_WORK_OBSERVED = "MANUAL_WORK_OBSERVED"
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    INTEGRATION_PENDING = "INTEGRATION_PENDING"
    INTEGRATION_PASSED = "INTEGRATION_PASSED"
    INTEGRATION_FAILED = "INTEGRATION_FAILED"
    DELIVERY_PENDING = "DELIVERY_PENDING"
    DELIVERY_ACCEPTED = "DELIVERY_ACCEPTED"
    DELIVERY_REJECTED = "DELIVERY_REJECTED"


class VerificationOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class IntegrationOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class DeliveryOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class DeterministicDecision:
    action: DeterministicAction
    reason: DeterministicReason
    work_id: str | None = None
    transition_key: str | None = None


class DeterministicProjectOrchestrator:
    """Choose one bounded action using the frozen deterministic rule order."""

    def decide(
        self,
        *,
        process: ProjectProcess,
        readiness: ReadinessEvaluation,
        has_open_input: bool = False,
        has_open_gate: bool = False,
        has_active_operation: bool = False,
        verification_outcome: VerificationOutcome | str | None = None,
        integration_outcome: IntegrationOutcome | str | None = None,
        integration_available: bool = False,
        delivery_outcome: DeliveryOutcome | str | None = None,
    ) -> DeterministicDecision:
        if process.status in {
            ProjectProcessStatus.COMPLETED,
            ProjectProcessStatus.FAILED,
            ProjectProcessStatus.CANCELLED,
        }:
            return DeterministicDecision(
                DeterministicAction.SLEEP, DeterministicReason.TERMINAL
            )
        if has_open_gate:
            return DeterministicDecision(
                DeterministicAction.SLEEP, DeterministicReason.HUMAN_APPROVAL
            )
        if has_open_input:
            return DeterministicDecision(
                DeterministicAction.SLEEP, DeterministicReason.HUMAN_INPUT
            )
        if process.status in {ProjectProcessStatus.WAITING, ProjectProcessStatus.BLOCKED}:
            return DeterministicDecision(
                DeterministicAction.SLEEP, DeterministicReason.DURABLE_WAIT
            )
        if process.phase is ProjectProcessPhase.EXECUTION:
            return self._execution(
                process=process,
                readiness=readiness,
                has_active_operation=has_active_operation,
            )
        if process.phase is ProjectProcessPhase.VERIFICATION:
            if verification_outcome is None:
                return DeterministicDecision(
                    DeterministicAction.WAIT_FOR_VERIFICATION,
                    DeterministicReason.VERIFICATION_PENDING,
                )
            if VerificationOutcome(verification_outcome) is VerificationOutcome.PASSED:
                return DeterministicDecision(
                    DeterministicAction.ENTER_INTEGRATION,
                    DeterministicReason.VERIFICATION_PASSED,
                    transition_key="verification.passed",
                )
            return DeterministicDecision(
                DeterministicAction.REOPEN_WORK,
                DeterministicReason.VERIFICATION_FAILED,
                transition_key="verification.failed",
            )
        if process.phase is ProjectProcessPhase.INTEGRATION:
            if integration_outcome is None:
                return DeterministicDecision(
                    (DeterministicAction.ASSEMBLE_INTEGRATION if integration_available
                     else DeterministicAction.WAIT_FOR_INTEGRATION),
                    DeterministicReason.INTEGRATION_PENDING,
                )
            if IntegrationOutcome(integration_outcome) is IntegrationOutcome.PASSED:
                return DeterministicDecision(
                    DeterministicAction.ENTER_DELIVERY,
                    DeterministicReason.INTEGRATION_PASSED,
                    transition_key="integration.passed",
                )
            return DeterministicDecision(
                DeterministicAction.REOPEN_WORK,
                DeterministicReason.INTEGRATION_FAILED,
            )
        if process.phase is ProjectProcessPhase.DELIVERY:
            if delivery_outcome is None:
                return DeterministicDecision(
                    DeterministicAction.WAIT_FOR_DELIVERY,
                    DeterministicReason.DELIVERY_PENDING,
                )
            if DeliveryOutcome(delivery_outcome) is DeliveryOutcome.ACCEPTED:
                return DeterministicDecision(
                    DeterministicAction.COMPLETE,
                    DeterministicReason.DELIVERY_ACCEPTED,
                    transition_key="delivery.accepted",
                )
            return DeterministicDecision(
                DeterministicAction.REOPEN_WORK,
                DeterministicReason.DELIVERY_REJECTED,
                transition_key="delivery.rejected",
            )
        return DeterministicDecision(
            DeterministicAction.SLEEP, DeterministicReason.DURABLE_WAIT
        )

    @staticmethod
    def _execution(
        *,
        process: ProjectProcess,
        readiness: ReadinessEvaluation,
        has_active_operation: bool,
    ) -> DeterministicDecision:
        if readiness.verification_ready:
            if process.status is ProjectProcessStatus.READY:
                return DeterministicDecision(
                    DeterministicAction.RECONCILE_RUNNING,
                    DeterministicReason.MANUAL_WORK_OBSERVED,
                    transition_key="work.dispatched",
                )
            return DeterministicDecision(
                DeterministicAction.ENTER_VERIFICATION,
                DeterministicReason.ALL_REQUIRED_WORK_SUBMITTED,
                transition_key="all_required_work_submitted",
            )
        if readiness.ready_work:
            item = readiness.ready_work[0]
            return DeterministicDecision(
                DeterministicAction.DISPATCH_WORK,
                DeterministicReason.READY_WORK,
                work_id=item.work_id,
                transition_key=(
                    "work.dispatched"
                    if process.status is ProjectProcessStatus.READY
                    else None
                ),
            )
        if has_active_operation:
            return DeterministicDecision(
                DeterministicAction.WAIT_FOR_WORK, DeterministicReason.ACTIVE_WORK
            )
        return DeterministicDecision(
            DeterministicAction.WAIT_FOR_WORK, DeterministicReason.WORK_NOT_READY
        )


__all__ = [
    "DeliveryOutcome",
    "DeterministicAction",
    "DeterministicDecision",
    "DeterministicProjectOrchestrator",
    "DeterministicReason",
    "IntegrationOutcome",
    "VerificationOutcome",
]
