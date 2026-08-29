from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select

from ..errors import GovernanceConflictError
from .event_catalog import validate_event_contract
from .gates import ProjectInputRequestStatus
from .models import ProjectProcessPhase, ProjectProcessStatus, ProjectProcessWaitReason
from .repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_USAGE,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESSES,
    SQLAlchemyProjectProcessRepository,
)
from .service import ProjectProcessService
from .transitions import ProjectTransitionGuard


class ProjectProcessShadowAdapter:
    """Transaction-aware projection from existing product actions into A2 state."""

    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))
        self.guard = ProjectTransitionGuard(clock=self.clock)

    def on_project_created(self, connection, *, project_id: str, actor_id: str, description: str):
        process_id = f"process:{project_id}"
        existing = connection.execute(
            select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == process_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self.repository.process(connection, process_id)
        now = self.clock()
        policy_id = f"policy:{project_id}"
        connection.execute(
            PROJECT_EXECUTION_POLICIES.insert().values(
                policy_id=policy_id,
                project_id=project_id,
                max_agent_runs=100,
                max_total_tokens=2_000_000,
                max_model_cost_microusd=100_000_000,
                max_replans=10,
                max_generated_tasks=200,
                max_active_agent_runs=8,
                max_active_runs_per_team=4,
                max_specialist_depth=3,
                max_specialist_runs_per_task=4,
                deadline_at=now + timedelta(days=90),
                version=1,
                created_at=now,
            )
        )
        values = {
            "process_id": process_id,
            "project_id": project_id,
            "phase": ProjectProcessPhase.INTAKE.value,
            "status": ProjectProcessStatus.WAITING.value,
            "wait_reason": ProjectProcessWaitReason.HUMAN_INPUT.value,
            "version": 1,
            "root_goal_id": None,
            "active_plan_id": None,
            "execution_policy_id": policy_id,
            "execution_policy_version": 1,
            "started_by": actor_id,
            "started_at": now,
            "updated_at": now,
            "last_event_sequence": 1,
            "last_orchestration_sequence": 0,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "completed_at": None,
        }
        connection.execute(PROJECT_PROCESSES.insert().values(**values))
        request_id = f"{process_id}:goal-input"
        connection.execute(
            PROJECT_INPUT_REQUESTS.insert().values(
                request_id=request_id,
                process_id=process_id,
                project_id=project_id,
                work_node_id=None,
                requested_by_run_id=None,
                requested_by_agent_id="service:project-orchestrator",
                question="Describe and confirm the project goal.",
                input_schema_json={"type": "object", "required": ["goal"]},
                context_projection_json={"project_id": project_id},
                status=ProjectInputRequestStatus.OPEN.value,
                response_json=None,
                version=1,
                created_by="service:project-orchestrator",
                created_at=now,
                answered_by=None,
                answered_at=None,
                resolution_idempotency_key=None,
                resolution_event_id=None,
                resolution_sha256=None,
            )
        )
        connection.execute(
            PROJECT_EXECUTION_USAGE.insert().values(
                process_id=process_id,
                project_id=project_id,
                agent_runs_started=0,
                agent_runs_completed=0,
                total_tokens=0,
                model_cost_microusd=0,
                replan_count=0,
                generated_task_count=0,
                active_agent_runs=0,
                version=1,
                updated_at=now,
            )
        )
        self._append_event(
            connection,
            process=self.repository._process(values),
            event_id=f"{process_id}:goal-input-requested",
            event_type="project.input.requested",
            transition_key=None,
            subject_type="project_input_request",
            subject_id=request_id,
            version_after=None,
            initiated_by=actor_id,
            executed_as="service:project-orchestrator",
            payload={"request_id": request_id, "purpose": "initial_project_goal"},
            sequence=1,
        )
        return self.repository.process(connection, process_id)

    def on_plan_approved(
        self,
        connection,
        *,
        project_id: str,
        draft_id: str,
        actor_id: str,
        goal_summary: str,
    ):
        process_id = f"process:{project_id}"
        if connection.execute(
            select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == process_id
            )
        ).scalar_one_or_none() is None:
            self.on_project_created(
                connection,
                project_id=project_id,
                actor_id=actor_id,
                description=goal_summary,
            )
        process = self.repository.process(connection, process_id)
        if process.phase is ProjectProcessPhase.EXECUTION:
            return process
        if process.phase is ProjectProcessPhase.INTAKE:
            process = self._answer_initial_goal(
                connection,
                process=process,
                actor_id=actor_id,
                goal_summary=goal_summary,
                draft_id=draft_id,
            )
        chain = (
            (ProjectProcessPhase.INTAKE, "project.goal.confirmed", "goal.confirmed", "project_goal"),
            (ProjectProcessPhase.ANALYSIS, "project.analysis.started", "analysis.started", "plan_draft"),
            (ProjectProcessPhase.ANALYSIS, "project.analysis.completed", "analysis.completed", "plan_draft"),
            (ProjectProcessPhase.PLANNING, "project.plan.approved", "plan.approved", "plan_draft"),
        )
        for expected_phase, fact, selector, subject_type in chain:
            process = self.repository.process(connection, process_id)
            if process.phase is not expected_phase:
                continue
            process = self._transition(
                connection,
                process=process,
                event_id=f"shadow:{draft_id}:{selector}",
                event_type=fact,
                transition_key=selector,
                subject_type=subject_type,
                subject_id=(project_id if selector == "goal.confirmed" else draft_id),
                initiated_by=actor_id,
                executed_as=(actor_id if selector == "goal.confirmed" else "service:project-orchestrator"),
                payload={"draft_id": draft_id, "shadow_projection": True},
            )
        return self.repository.process(connection, process_id)

    def on_team_task_changed(
        self,
        connection,
        *,
        project_id: str,
        task_id: str,
        actor_id: str,
        activity_type: str,
        occurred_at: datetime,
        source_aggregate_version: int | None = None,
    ):
        """Append a Product TeamTask fact in the caller's transaction.

        This remains a shadow projection: it records and wakes durable process
        state without dispatching an Agent or inventing a second task status.
        """
        event_types = {
            "task.accepted": "team_task.accepted",
            "task.rejected": "team_task.rejected",
            "task.started": "team_task.started",
            "task.submitted": "team_task.submitted",
            "task.verified": "team_task.verified",
            "task.changes_requested": "team_task.changes_requested",
            "task_schedule_changed": "task.schedule.changed",
        }
        event_type = event_types.get(activity_type)
        if event_type is None:
            raise ValueError("team task activity is not a project process fact")
        process_id = f"process:{project_id}"
        if connection.execute(
            select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == process_id
            )
        ).scalar_one_or_none() is None:
            self.on_project_created(
                connection,
                project_id=project_id,
                actor_id=actor_id,
                description="",
            )
        process = self.repository.process(connection, process_id)
        identity = f"{project_id}:{task_id}:{event_type}:{occurred_at.isoformat()}"
        event_id = f"shadow:task:{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
        existing = self.repository.event(connection, event_id)
        if existing is not None:
            return process, existing
        now = self.clock()
        next_sequence = process.last_event_sequence + 1
        updated = connection.execute(
            PROJECT_PROCESSES.update()
            .where(
                and_(
                    PROJECT_PROCESSES.c.process_id == process_id,
                    PROJECT_PROCESSES.c.version == process.version,
                    PROJECT_PROCESSES.c.last_event_sequence == process.last_event_sequence,
                )
            )
            .values(last_event_sequence=next_sequence, updated_at=now)
        ).rowcount
        if updated != 1:
            raise GovernanceConflictError("shadow project process event cursor is stale")
        event = self._append_event(
            connection,
            process=process,
            event_id=event_id,
            event_type=event_type,
            transition_key=None,
            subject_type="team_task",
            subject_id=task_id,
            version_after=None,
            initiated_by=actor_id,
            executed_as=actor_id,
            payload={"task_id": task_id, "activity_type": activity_type},
            sequence=next_sequence,
            source_aggregate_version=source_aggregate_version,
        )
        return self.repository.process(connection, process_id), event

    def _answer_initial_goal(self, connection, *, process, actor_id, goal_summary, draft_id):
        request_id = f"{process.process_id}:goal-input"
        row = connection.execute(
            select(PROJECT_INPUT_REQUESTS).where(PROJECT_INPUT_REQUESTS.c.request_id == request_id)
        ).mappings().one()
        if row["status"] == ProjectInputRequestStatus.OPEN.value:
            now = self.clock()
            event_id = f"shadow:{draft_id}:goal-input-provided"
            payload = {"goal": goal_summary[:4000]}
            _, digest = self.repository.canonical_payload(payload)
            connection.execute(
                PROJECT_INPUT_REQUESTS.update()
                .where(PROJECT_INPUT_REQUESTS.c.request_id == request_id)
                .values(
                    status=ProjectInputRequestStatus.ANSWERED.value,
                    response_json=payload,
                    version=row["version"] + 1,
                    answered_by=actor_id,
                    answered_at=now,
                    resolution_idempotency_key=event_id,
                    resolution_event_id=event_id,
                    resolution_sha256=digest,
                )
            )
            connection.execute(
                PROJECT_PROCESSES.update()
                .where(PROJECT_PROCESSES.c.process_id == process.process_id)
                .values(last_event_sequence=process.last_event_sequence + 1, updated_at=now)
            )
            process = self.repository.process(connection, process.process_id)
            self._append_event(
                connection,
                process=process,
                event_id=event_id,
                event_type="human.input.provided",
                transition_key=None,
                subject_type="project_input_request",
                subject_id=request_id,
                version_after=None,
                initiated_by=actor_id,
                executed_as=actor_id,
                payload=payload,
                sequence=process.last_event_sequence,
                process_version_before=process.version,
            )
        return self.repository.process(connection, process.process_id)

    def _transition(self, connection, *, process, event_id, event_type, transition_key,
                    subject_type, subject_id, initiated_by, executed_as, payload):
        changed = self.guard.transition(
            process=process,
            event_type=transition_key,
            expected_version=process.version,
        )
        updated = connection.execute(
            PROJECT_PROCESSES.update()
            .where(
                and_(
                    PROJECT_PROCESSES.c.process_id == process.process_id,
                    PROJECT_PROCESSES.c.version == process.version,
                    PROJECT_PROCESSES.c.last_event_sequence == process.last_event_sequence,
                )
            )
            .values(
                phase=changed.phase.value,
                status=changed.status.value,
                wait_reason=changed.wait_reason.value,
                version=changed.version,
                last_event_sequence=changed.last_event_sequence,
                updated_at=changed.updated_at,
                active_plan_id=subject_id if transition_key == "plan.approved" else process.active_plan_id,
            )
        ).rowcount
        if updated != 1:
            raise GovernanceConflictError("shadow project process cursor is stale")
        self._append_event(
            connection,
            process=process,
            event_id=event_id,
            event_type=event_type,
            transition_key=transition_key,
            subject_type=subject_type,
            subject_id=subject_id,
            version_after=changed.version,
            initiated_by=initiated_by,
            executed_as=executed_as,
            payload=payload,
            sequence=changed.last_event_sequence,
        )
        return self.repository.process(connection, process.process_id)

    def _append_event(self, connection, *, process, event_id, event_type, transition_key,
                      subject_type, subject_id, version_after, initiated_by, executed_as,
                      payload, sequence, process_version_before=None,
                      source_aggregate_version=1):
        validate_event_contract(
            event_type=event_type,
            transition_key=transition_key,
            schema_version="v1",
        )
        normalized, digest = self.repository.canonical_payload(payload)
        return self.repository.append_event(
            connection,
            ProjectProcessService._event_values(
                process=process,
                event_id=event_id,
                event_type=event_type,
                idempotency_key=event_id,
                transition_key=transition_key,
                schema_version="v1",
                subject_type=subject_type,
                subject_id=subject_id,
                source_aggregate_version=source_aggregate_version,
                version_after=version_after,
                initiated_by=initiated_by,
                executed_as=executed_as,
                correlation_id=f"process:{process.process_id}:shadow",
                causation_id=None,
                payload=normalized,
                digest=digest,
                occurred_at=self.clock(),
            )
            | {
                "sequence": sequence,
                "process_version_before": (
                    process.version if process_version_before is None else process_version_before
                ),
            },
        )
