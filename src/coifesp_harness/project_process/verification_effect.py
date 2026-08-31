"""Atomic verification transitions composed with normal task dispatch."""

from sqlalchemy import select

from ..errors import GovernanceConflictError
from ..verification.project_evidence import load_project_verification_evidence
from .commands import ProjectOrchestrationDecisionStatus
from .models import ProjectProcessPhase, ProjectProcessStatus
from .orchestrator import DeterministicAction, DeterministicReason, VerificationOutcome
from .repository import PROJECT_PROCESSES


class VerificationOrchestrationEffect:
    def __init__(self, *, repository, work_graph_repository, dispatcher, integration_service=None):
        if repository.engine is not work_graph_repository.engine:
            raise ValueError("verification effect requires one shared database")
        self.repository = repository
        self.work_graph = work_graph_repository
        self.dispatcher = dispatcher
        self.integration_service = integration_service

    def apply_with_event(self, *, decision_id, process, decision, mutation_fence, publish_dispatch):
        if decision.action is DeterministicAction.ASSEMBLE_INTEGRATION:
            return self._assemble(decision_id=decision_id, process=process,
                decision=decision, mutation_fence=mutation_fence)
        if decision.action is DeterministicAction.DISPATCH_WORK:
            return self.dispatcher.apply_with_event(
                decision_id=decision_id, process=process, decision=decision,
                mutation_fence=mutation_fence, publish_dispatch=publish_dispatch,
            )
        transitions = {
            DeterministicAction.REOPEN_WORK: (DeterministicReason.VERIFICATION_FAILED,
                "verification.failed", VerificationOutcome.FAILED, ProjectProcessPhase.EXECUTION),
            DeterministicAction.ENTER_INTEGRATION: (DeterministicReason.VERIFICATION_PASSED,
                "verification.passed", VerificationOutcome.PASSED, ProjectProcessPhase.INTEGRATION),
        }
        expected_transition = transitions.get(decision.action)
        if (expected_transition is None or decision.reason is not expected_transition[0]
                or decision.transition_key != expected_transition[1] or decision.work_id is not None):
            raise GovernanceConflictError("effect only handles dispatch and verification decisions")
        if not callable(mutation_fence) or not callable(publish_dispatch):
            raise TypeError("verification reopening requires a live fence and atomic event publisher")
        with self.repository.transaction() as connection:
            mutation_fence(connection)
            connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == process.process_id,
            ).with_for_update()).scalar_one()
            current = self.repository.process(connection, process.process_id)
            stored = self.repository.decision(connection, decision_id)
            expected = {"action": decision.action.value, "reason": decision.reason.value,
                        "transition_key": decision.transition_key, "work_id": decision.work_id}
            if (stored is None or stored.process_id != current.process_id
                    or stored.project_id != current.project_id or stored.decision_json != expected):
                raise GovernanceConflictError("reopening decision binding mismatch")
            event = self.repository.event(connection, f"event:{decision_id}")
            if event is not None:
                # Commit-before-ack recovery: do not reapply against the new phase.
                if (event.process_id != current.process_id
                        or event.event_type != "project.verification.completed"
                        or event.transition_key != decision.transition_key
                        or event.payload.get("decision_id") != decision_id
                        or event.payload.get("reason") != decision.reason.value):
                    raise GovernanceConflictError("reopening event binding mismatch")
                mutation_fence(connection)
                return
            if (stored.status is not ProjectOrchestrationDecisionStatus.PENDING
                    or stored.based_on_process_version != current.version
                    or stored.based_on_event_sequence != current.last_event_sequence
                    or current.phase is not ProjectProcessPhase.VERIFICATION
                    or current.status is not ProjectProcessStatus.READY):
                raise GovernanceConflictError("verification reopening snapshot is stale")
            graph = self.work_graph.snapshot(connection, project_id=current.project_id)
            if graph.digest != stored.graph_snapshot_digest:
                raise GovernanceConflictError("verification reopening graph is stale")
            evidence = load_project_verification_evidence(connection, process=current, graph=graph)
            if evidence.outcome is not expected_transition[2]:
                raise GovernanceConflictError("verification transition needs matching current evidence")
            # Verification already marked affected tasks changes_requested. Keep
            # their accepted contracts and evidence intact; only Guard changes phase.
            # This deterministic retry is not a Planner replan and consumes no
            # replan/generated-task quota. The next dispatch reserves run budget.
            publish_dispatch(connection)
            updated = self.repository.process(connection, current.process_id)
            event = self.repository.event(connection, f"event:{decision_id}")
            if (event is None or event.transition_key != decision.transition_key
                    or updated.phase is not expected_transition[3]
                    or updated.status is not ProjectProcessStatus.READY
                    or updated.version != current.version + 1):
                raise GovernanceConflictError("verification reopening publisher omitted transition")
            mutation_fence(connection)

    def _assemble(self, *, decision_id, process, decision, mutation_fence):
        if (self.integration_service is None or not callable(mutation_fence)
                or decision.reason is not DeterministicReason.INTEGRATION_PENDING
                or decision.transition_key is not None or decision.work_id is not None):
            raise GovernanceConflictError("integration requires a bound assembly decision")
        with self.repository.transaction() as connection:
            mutation_fence(connection)
            connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == process.process_id).with_for_update()).scalar_one()
            stored = self.repository.decision(connection, decision_id)
            expected = {"action": decision.action.value, "reason": decision.reason.value,
                        "transition_key": None, "work_id": None}
            if (stored is None or stored.process_id != process.process_id
                    or stored.project_id != process.project_id or stored.decision_json != expected
                    or stored.status is not ProjectOrchestrationDecisionStatus.PENDING):
                raise GovernanceConflictError("integration decision binding mismatch")
            return self.integration_service.execute(bound_connection=connection,
                process_id=process.process_id, expected_version=stored.based_on_process_version,
                expected_event_sequence=stored.based_on_event_sequence,
                expected_graph_digest=stored.graph_snapshot_digest,
                event_id=f"event:{decision_id}", decision_id=decision_id, mutation_fence=mutation_fence)
