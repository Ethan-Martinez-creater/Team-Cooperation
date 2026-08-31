"""Fenced, crash-recoverable execution of deterministic project decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.engine import Connection

from ..errors import GovernanceConflictError
from .command_service import ProjectProcessCommandService
from .commands import ProjectOrchestrationDecisionStatus
from .models import ProjectProcess
from .orchestrator import (
    DeliveryOutcome,
    DeterministicAction,
    DeterministicDecision,
    DeterministicProjectOrchestrator,
    DeterministicReason,
    IntegrationOutcome,
    VerificationOutcome,
)
from .readiness import ReadinessEvaluation
from .repository import SQLAlchemyProjectProcessRepository
from .scheduler import (
    ProjectProcessScheduler,
    ProjectProcessWakeup,
    ProjectProcessWakeupStatus,
)
from .service import ProjectProcessService

ORCHESTRATOR_PRINCIPAL_ID = "service:project-orchestrator"


@dataclass(frozen=True, slots=True)
class ProjectOrchestrationSnapshot:
    """All authoritative facts used by one deterministic decision."""

    graph_digest: str
    readiness: ReadinessEvaluation
    has_open_input: bool = False
    has_open_gate: bool = False
    has_active_operation: bool = False
    verification_outcome: VerificationOutcome | str | None = None
    integration_outcome: IntegrationOutcome | str | None = None
    integration_available: bool = False
    delivery_outcome: DeliveryOutcome | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.graph_digest, str) or not self.graph_digest.strip():
            raise ValueError("graph_digest is required")
        if not isinstance(self.readiness, ReadinessEvaluation):
            raise TypeError("readiness must be ReadinessEvaluation")


class ProjectOrchestrationSnapshotLoader(Protocol):
    def __call__(self, process: ProjectProcess) -> ProjectOrchestrationSnapshot: ...


class ProjectOrchestrationEffect(Protocol):
    def __call__(
        self,
        *,
        decision_id: str,
        process: ProjectProcess,
        decision: DeterministicDecision,
        mutation_fence: Callable[[Connection], object],
    ) -> None: ...


class ProjectOrchestratorWorkerStatus(StrEnum):
    IDLE = "IDLE"
    APPLIED = "APPLIED"
    STALE = "STALE"
    RETRY = "RETRY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ProjectOrchestratorWorkerOutcome:
    status: ProjectOrchestratorWorkerStatus
    wakeup_id: str | None = None
    decision_id: str | None = None
    action: DeterministicAction | None = None
    error: str | None = None


class ProjectOrchestratorRunner:
    """Consume one durable wakeup without making a model call.

    The decision id and emitted event id are derived from the wakeup identity.
    A retry therefore resumes the same pending decision instead of recomputing
    from a newer snapshot.  All persistent mutations validate the scheduler's
    lease inside their own transaction.
    """

    def __init__(
        self,
        *,
        repository: SQLAlchemyProjectProcessRepository,
        process_service: ProjectProcessService,
        command_service: ProjectProcessCommandService,
        scheduler: ProjectProcessScheduler,
        snapshot_loader: ProjectOrchestrationSnapshotLoader,
        orchestrator: DeterministicProjectOrchestrator | None = None,
        effect: ProjectOrchestrationEffect | None = None,
        retry_after_seconds: float = 0,
        after_effect: Callable[[], None] | None = None,
        after_decision_finished: Callable[[], None] | None = None,
    ) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        self.repository = repository
        self.process_service = process_service
        self.command_service = command_service
        self.scheduler = scheduler
        self.snapshot_loader = snapshot_loader
        self.orchestrator = orchestrator or DeterministicProjectOrchestrator()
        self.effect = effect
        self.retry_after_seconds = retry_after_seconds
        self.after_effect = after_effect
        self.after_decision_finished = after_decision_finished

    def process_once(
        self,
        *,
        worker_id: str,
        process_id: str | None = None,
        lease_seconds: int = 30,
    ) -> ProjectOrchestratorWorkerOutcome:
        wakeup = self.scheduler.claim(
            owner=worker_id,
            process_id=process_id,
            lease_seconds=lease_seconds,
        )
        if wakeup is None:
            return ProjectOrchestratorWorkerOutcome(ProjectOrchestratorWorkerStatus.IDLE)
        try:
            return self._process_claimed(wakeup=wakeup, worker_id=worker_id)
        # A worker boundary must convert unexpected adapter failures into the
        # durable retry budget; the persisted error is deliberately sanitized.
        except Exception as exc:  # noqa: BLE001
            retried = self.scheduler.retry(
                wakeup_id=wakeup.wakeup_id,
                owner=worker_id,
                lease_token=wakeup.lease_token,
                fencing_token=wakeup.fencing_token,
                error=self._safe_error(exc),
                retry_after_seconds=self.retry_after_seconds,
            )
            status = (
                ProjectOrchestratorWorkerStatus.FAILED
                if retried.status is ProjectProcessWakeupStatus.FAILED
                else ProjectOrchestratorWorkerStatus.RETRY
            )
            return ProjectOrchestratorWorkerOutcome(
                status,
                wakeup_id=wakeup.wakeup_id,
                decision_id=self._decision_id(wakeup),
                error=self._safe_error(exc),
            )

    def _process_claimed(
        self,
        *,
        wakeup: ProjectProcessWakeup,
        worker_id: str,
    ) -> ProjectOrchestratorWorkerOutcome:
        decision_id = self._decision_id(wakeup)
        fence = self._fence(wakeup=wakeup, worker_id=worker_id)
        existing = self._decision(decision_id)
        if existing is not None:
            if existing.status is ProjectOrchestrationDecisionStatus.STALE:
                self._emit_stale(
                    wakeup=wakeup,
                    decision_id=decision_id,
                    worker_id=worker_id,
                    mutation_fence=fence,
                )
                self._complete(wakeup=wakeup, worker_id=worker_id)
                return ProjectOrchestratorWorkerOutcome(
                    ProjectOrchestratorWorkerStatus.STALE,
                    wakeup.wakeup_id,
                    decision_id,
                )
            if existing.status in {
                ProjectOrchestrationDecisionStatus.APPLIED,
                ProjectOrchestrationDecisionStatus.REJECTED,
            }:
                action = self._decision_from_json(existing.decision_json).action
                self._complete(wakeup=wakeup, worker_id=worker_id)
                return ProjectOrchestratorWorkerOutcome(
                    ProjectOrchestratorWorkerStatus.APPLIED,
                    wakeup.wakeup_id,
                    decision_id,
                    action,
                )
            decision = self._decision_from_json(existing.decision_json)
            process = self._process(wakeup.process_id)
            if self._event(self._effect_event_id(decision_id)) is None:
                snapshot = self.snapshot_loader(process)
                if not isinstance(snapshot, ProjectOrchestrationSnapshot):
                    raise TypeError("snapshot_loader must return ProjectOrchestrationSnapshot")
                current = self.command_service.invalidate_unapplied_decision(
                    decision_id=decision_id, current_graph_snapshot_digest=snapshot.graph_digest,
                    mutation_fence=fence,
                )
                if current.status is ProjectOrchestrationDecisionStatus.STALE:
                    self._emit_stale(wakeup=wakeup, decision_id=decision_id,
                                     worker_id=worker_id, mutation_fence=fence)
                    self._complete(wakeup=wakeup, worker_id=worker_id)
                    return ProjectOrchestratorWorkerOutcome(
                        ProjectOrchestratorWorkerStatus.STALE, wakeup.wakeup_id,
                        decision_id, decision.action,
                    )
        else:
            process = self._process(wakeup.process_id)
            snapshot = self.snapshot_loader(process)
            if not isinstance(snapshot, ProjectOrchestrationSnapshot):
                raise TypeError("snapshot_loader must return ProjectOrchestrationSnapshot")
            decision = self.orchestrator.decide(
                process=process,
                readiness=snapshot.readiness,
                has_open_input=snapshot.has_open_input,
                has_open_gate=snapshot.has_open_gate,
                has_active_operation=snapshot.has_active_operation,
                verification_outcome=snapshot.verification_outcome,
                integration_outcome=snapshot.integration_outcome,
                integration_available=snapshot.integration_available,
                delivery_outcome=snapshot.delivery_outcome,
            )
            persisted = self.command_service.record_decision(
                decision_id=decision_id,
                process_id=process.process_id,
                reason=decision.reason.value,
                based_on_process_version=process.version,
                based_on_event_sequence=process.last_event_sequence,
                graph_snapshot_digest=snapshot.graph_digest,
                current_graph_snapshot_digest=snapshot.graph_digest,
                decision_json=self._decision_json(decision),
                commands=(),
                mutation_fence=fence,
            )
            if persisted.status is ProjectOrchestrationDecisionStatus.STALE:
                self._emit_stale(
                    wakeup=wakeup,
                    decision_id=decision_id,
                    worker_id=worker_id,
                    mutation_fence=fence,
                )
                self._complete(wakeup=wakeup, worker_id=worker_id)
                return ProjectOrchestratorWorkerOutcome(
                    ProjectOrchestratorWorkerStatus.STALE,
                    wakeup.wakeup_id,
                    decision_id,
                    decision.action,
                )

        self._apply_decision(
            wakeup=wakeup,
            decision_id=decision_id,
            process=process,
            decision=decision,
            mutation_fence=fence,
        )
        if self.after_effect is not None:
            self.after_effect()
        self.command_service.finish_decision(
            decision_id=decision_id,
            status=ProjectOrchestrationDecisionStatus.APPLIED,
            mutation_fence=fence,
        )
        if self.after_decision_finished is not None:
            self.after_decision_finished()
        self._complete(wakeup=wakeup, worker_id=worker_id)
        return ProjectOrchestratorWorkerOutcome(
            ProjectOrchestratorWorkerStatus.APPLIED,
            wakeup.wakeup_id,
            decision_id,
            decision.action,
        )

    def _apply_decision(
        self,
        *,
        wakeup: ProjectProcessWakeup,
        decision_id: str,
        process: ProjectProcess,
        decision: DeterministicDecision,
        mutation_fence: Callable[[Connection], object],
    ) -> None:
        event_id = self._effect_event_id(decision_id)
        if self._event(event_id) is not None:
            return
        needs_external_effect = decision.action in {
            DeterministicAction.DISPATCH_WORK,
            DeterministicAction.REOPEN_WORK,
            DeterministicAction.ENTER_INTEGRATION,
            DeterministicAction.ASSEMBLE_INTEGRATION,
        }
        if needs_external_effect:
            if self.effect is None:
                raise GovernanceConflictError(
                    "orchestrator decision requires an idempotent effect adapter"
                )
            atomic_effect = getattr(self.effect, "apply_with_event", None)
            if callable(atomic_effect):
                atomic_effect(
                    decision_id=decision_id, process=process, decision=decision,
                    mutation_fence=mutation_fence,
                    publish_dispatch=lambda connection: self._publish_effect(
                        wakeup=wakeup, decision_id=decision_id, process=process,
                        decision=decision, mutation_fence=mutation_fence,
                        connection=connection,
                    ),
                )
                if self._event(event_id) is None:
                    raise GovernanceConflictError("atomic orchestration effect omitted its domain event")
                return
            if decision.action is DeterministicAction.ASSEMBLE_INTEGRATION:
                raise GovernanceConflictError("integration requires an atomic evidence adapter")
            self.effect(
                decision_id=decision_id,
                process=process,
                decision=decision,
                mutation_fence=mutation_fence,
            )
        self._publish_effect(
            wakeup=wakeup, decision_id=decision_id, process=process,
            decision=decision, mutation_fence=mutation_fence,
        )

    def _publish_effect(
        self, *, wakeup, decision_id, process, decision, mutation_fence,
        connection: Connection | None = None,
    ) -> None:
        event_type = self._event_type(decision)
        if event_type is None:
            return
        event_id = self._effect_event_id(decision_id)
        if connection is None:
            service = self.process_service
            current = self._process(process.process_id)
            initiated_by = self._initiated_by(wakeup)
        else:
            repository = self.repository.using_connection(connection)
            service = ProjectProcessService(
                repository, guard=self.process_service.guard, clock=self.process_service.clock
            )
            current = repository.process(connection, process.process_id)
            source = repository.event(connection, wakeup.source_event_id)
            initiated_by = source.initiated_by if source else ORCHESTRATOR_PRINCIPAL_ID
        kwargs = {
            "process_id": current.process_id,
            "event_id": event_id,
            "event_type": event_type,
            "expected_version": current.version,
            "subject_type": "work" if decision.work_id else "project",
            "subject_id": decision.work_id or current.project_id,
            "initiated_by": initiated_by,
            "executed_as": ORCHESTRATOR_PRINCIPAL_ID,
            "correlation_id": self._correlation_id(wakeup),
            "causation_id": wakeup.source_event_id,
            "payload": {
                "decision_id": decision_id,
                "action": decision.action.value,
                "reason": decision.reason.value,
                "work_id": decision.work_id,
            },
            "mutation_fence": mutation_fence,
        }
        if decision.transition_key in {"verification.passed", "verification.failed"}:
            kwargs["payload"]["outcome"] = (
                "PASS" if decision.transition_key == "verification.passed" else "FAIL"
            )
        if decision.transition_key is not None:
            service.apply_transition(
                **kwargs,
                transition_key=decision.transition_key,
            )
        else:
            service.append_fact(
                **kwargs,
                expected_event_sequence=current.last_event_sequence,
            )

    def _emit_stale(
        self,
        *,
        wakeup: ProjectProcessWakeup,
        decision_id: str,
        worker_id: str,
        mutation_fence: Callable[[Connection], object],
    ) -> None:
        del worker_id
        event_id = self._stale_event_id(decision_id)
        if self._event(event_id) is not None:
            return
        process = self._process(wakeup.process_id)
        self.process_service.append_fact(
            process_id=process.process_id,
            event_id=event_id,
            event_type="project.orchestrator.decision_stale",
            expected_version=process.version,
            expected_event_sequence=process.last_event_sequence,
            subject_type="orchestration_decision",
            subject_id=decision_id,
            initiated_by=self._initiated_by(wakeup),
            executed_as=ORCHESTRATOR_PRINCIPAL_ID,
            correlation_id=self._correlation_id(wakeup),
            causation_id=wakeup.source_event_id,
            payload={"decision_id": decision_id, "source_event_id": wakeup.source_event_id},
            mutation_fence=mutation_fence,
        )

    def _fence(self, *, wakeup: ProjectProcessWakeup, worker_id: str):
        def require_fence(connection: Connection):
            return self.scheduler.assert_fence_in_transaction(
                connection,
                wakeup_id=wakeup.wakeup_id,
                owner=worker_id,
                lease_token=wakeup.lease_token,
                fencing_token=wakeup.fencing_token,
            )

        return require_fence

    def _complete(self, *, wakeup: ProjectProcessWakeup, worker_id: str) -> None:
        self.scheduler.complete(
            wakeup_id=wakeup.wakeup_id,
            owner=worker_id,
            lease_token=wakeup.lease_token,
            fencing_token=wakeup.fencing_token,
        )

    def _process(self, process_id: str) -> ProjectProcess:
        with self.repository.transaction() as connection:
            return self.repository.process(connection, process_id)

    def _decision(self, decision_id: str):
        with self.repository.transaction() as connection:
            return self.repository.decision(connection, decision_id)

    def _event(self, event_id: str):
        with self.repository.transaction() as connection:
            return self.repository.event(connection, event_id)

    def _initiated_by(self, wakeup: ProjectProcessWakeup) -> str:
        source = self._event(wakeup.source_event_id)
        return source.initiated_by if source is not None else ORCHESTRATOR_PRINCIPAL_ID

    @staticmethod
    def _decision_json(decision: DeterministicDecision) -> dict:
        return {
            "action": decision.action.value,
            "reason": decision.reason.value,
            "work_id": decision.work_id,
            "transition_key": decision.transition_key,
        }

    @staticmethod
    def _decision_from_json(payload: dict) -> DeterministicDecision:
        try:
            return DeterministicDecision(
                action=DeterministicAction(payload["action"]),
                reason=DeterministicReason(payload["reason"]),
                work_id=payload.get("work_id"),
                transition_key=payload.get("transition_key"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GovernanceConflictError("persisted orchestrator decision is invalid") from exc

    @staticmethod
    def _event_type(decision: DeterministicDecision) -> str | None:
        if decision.action in {
            DeterministicAction.SLEEP,
            DeterministicAction.WAIT_FOR_WORK,
            DeterministicAction.WAIT_FOR_VERIFICATION,
            DeterministicAction.WAIT_FOR_INTEGRATION,
            DeterministicAction.WAIT_FOR_DELIVERY,
        }:
            return None
        if decision.action in {
            DeterministicAction.DISPATCH_WORK,
            DeterministicAction.RECONCILE_RUNNING,
        }:
            return "project.work.dispatched"
        if decision.action is DeterministicAction.ENTER_VERIFICATION:
            return "project.work.required_submitted"
        if decision.action in {
            DeterministicAction.ENTER_INTEGRATION,
            DeterministicAction.REOPEN_WORK,
        } and decision.transition_key in {"verification.passed", "verification.failed"}:
            return "project.verification.completed"
        if decision.action is DeterministicAction.ENTER_DELIVERY:
            return "project.integration.completed"
        if decision.action is DeterministicAction.COMPLETE:
            return "project.delivery.accepted"
        if decision.action is DeterministicAction.REOPEN_WORK and decision.transition_key == "delivery.rejected":
            return "project.delivery.rejected"
        return None

    @staticmethod
    def _stable_digest(*parts: str) -> str:
        encoded = json.dumps(parts, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _decision_id(cls, wakeup: ProjectProcessWakeup) -> str:
        return f"decision:{cls._stable_digest(wakeup.process_id, wakeup.source_event_id)}"

    @staticmethod
    def _effect_event_id(decision_id: str) -> str:
        return f"event:{decision_id}"

    @staticmethod
    def _stale_event_id(decision_id: str) -> str:
        return f"stale:{decision_id}"

    @classmethod
    def _correlation_id(cls, wakeup: ProjectProcessWakeup) -> str:
        return f"orchestrator:{cls._stable_digest(wakeup.wakeup_id)}"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        return message[:1000]


__all__ = [
    "ORCHESTRATOR_PRINCIPAL_ID",
    "ProjectOrchestrationEffect",
    "ProjectOrchestrationSnapshot",
    "ProjectOrchestrationSnapshotLoader",
    "ProjectOrchestratorRunner",
    "ProjectOrchestratorWorkerOutcome",
    "ProjectOrchestratorWorkerStatus",
]
