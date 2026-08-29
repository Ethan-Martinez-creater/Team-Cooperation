from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select

from ..errors import GovernanceConflictError
from .event_catalog import validate_event_contract
from .gates import ProjectInputRequestStatus
from .models import (
    TERMINAL_STATUSES,
    ProjectProcess,
    ProjectProcessPhase,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
)
from .repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_USAGE,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESS_EVENTS,
    PROJECT_PROCESSES,
    SQLAlchemyProjectProcessRepository,
)
from .transitions import ProjectTransitionGuard


class ProjectProcessService:
    def __init__(
        self,
        repository: SQLAlchemyProjectProcessRepository,
        *,
        guard: ProjectTransitionGuard | None = None,
        clock=None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))
        self.guard = guard or ProjectTransitionGuard(clock=self.clock)

    def create_policy(self, **values) -> None:
        now = self.clock()
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, values["project_id"])
            existing = (
                connection.execute(
                    select(PROJECT_EXECUTION_POLICIES).where(
                        and_(
                            PROJECT_EXECUTION_POLICIES.c.policy_id == values["policy_id"],
                            PROJECT_EXECUTION_POLICIES.c.version == values["version"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            row = {**values, "created_at": now}
            if existing is not None:
                comparable = {key: value for key, value in row.items() if key != "created_at"}
                if any(existing[key] != value for key, value in comparable.items()):
                    raise GovernanceConflictError(
                        "project policy id was reused with different content"
                    )
                return
            connection.execute(PROJECT_EXECUTION_POLICIES.insert().values(**row))

    def start_process(
        self,
        *,
        process_id: str,
        project_id: str,
        execution_policy_id: str,
        started_by: str,
        initial_goal_question: str = "Describe and confirm the project goal.",
    ) -> ProjectProcess:
        now = self.clock()
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, project_id)
            existing = (
                connection.execute(
                    select(PROJECT_PROCESSES).where(PROJECT_PROCESSES.c.process_id == process_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["project_id"] != project_id:
                    raise GovernanceConflictError("project process id was reused")
                return self.repository._process(existing)
            active = connection.execute(
                select(PROJECT_PROCESSES.c.process_id).where(
                    and_(
                        PROJECT_PROCESSES.c.project_id == project_id,
                        PROJECT_PROCESSES.c.status.not_in(
                            [item.value for item in TERMINAL_STATUSES]
                        ),
                    )
                )
            ).scalar_one_or_none()
            if active is not None:
                raise GovernanceConflictError("project already has an active process")
            policy = (
                connection.execute(
                    select(PROJECT_EXECUTION_POLICIES).where(
                        and_(
                            PROJECT_EXECUTION_POLICIES.c.policy_id == execution_policy_id,
                            PROJECT_EXECUTION_POLICIES.c.project_id == project_id,
                        )
                    ).order_by(PROJECT_EXECUTION_POLICIES.c.version.desc()).limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if policy is None:
                raise GovernanceConflictError("project execution policy is unavailable")
            values = {
                "process_id": process_id,
                "project_id": project_id,
                "phase": ProjectProcessPhase.INTAKE.value,
                "status": ProjectProcessStatus.WAITING.value,
                "wait_reason": ProjectProcessWaitReason.HUMAN_INPUT.value,
                "version": 1,
                "root_goal_id": None,
                "active_plan_id": None,
                "execution_policy_id": execution_policy_id,
                "execution_policy_version": policy["version"],
                "started_by": started_by,
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
            event_id = f"{process_id}:goal-input-requested"
            connection.execute(
                PROJECT_INPUT_REQUESTS.insert().values(
                    request_id=request_id,
                    process_id=process_id,
                    project_id=project_id,
                    work_node_id=None,
                    requested_by_run_id=None,
                    requested_by_agent_id="service:project-orchestrator",
                    question=initial_goal_question,
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
            payload, digest = self.repository.canonical_payload(
                {"request_id": request_id, "purpose": "initial_project_goal"}
            )
            self.repository.append_event(
                connection,
                {
                    "event_id": event_id,
                    "process_id": process_id,
                    "project_id": project_id,
                    "sequence": 1,
                    "event_type": "project.input.requested",
                    "idempotency_key": event_id,
                    "transition_key": None,
                    "schema_version": "v1",
                    "subject_type": "project_input_request",
                    "subject_id": request_id,
                    "source_aggregate_version": 1,
                    "process_version_before": 1,
                    "process_version_after": None,
                    "initiated_by": started_by,
                    "executed_as": "service:project-orchestrator",
                    "correlation_id": f"process:{process_id}:start",
                    "causation_id": None,
                    "payload_json": payload,
                    "payload_sha256": digest,
                    "occurred_at": now,
                },
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
            return self.repository._process(values)

    def apply_transition(
        self,
        *,
        process_id: str,
        event_id: str,
        event_type: str,
        transition_key: str,
        expected_version: int,
        subject_type: str,
        subject_id: str,
        initiated_by: str,
        executed_as: str,
        correlation_id: str,
        payload: dict,
        source_aggregate_version: int | None = None,
        causation_id: str | None = None,
        schema_version: str = "v1",
        idempotency_key: str | None = None,
    ):
        idempotency_key = idempotency_key or event_id
        validate_event_contract(
            event_type=event_type,
            transition_key=transition_key,
            schema_version=schema_version,
        )
        normalized, digest = self.repository.canonical_payload(payload)
        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
            duplicate = self._event_retry(connection, process_id, event_id, idempotency_key)
            if duplicate is not None:
                self._require_duplicate(
                    duplicate,
                    event_id=event_id,
                    process_id=process_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    transition_key=transition_key,
                    digest=digest,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    initiated_by=initiated_by,
                    executed_as=executed_as,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    source_aggregate_version=source_aggregate_version,
                )
                return process, duplicate
            changed = self.guard.transition(
                process=process,
                event_type=transition_key,
                expected_version=expected_version,
            )
            updated = connection.execute(
                PROJECT_PROCESSES.update()
                .where(
                    and_(
                        PROJECT_PROCESSES.c.process_id == process_id,
                        PROJECT_PROCESSES.c.version == expected_version,
                    )
                )
                .values(
                    phase=changed.phase.value,
                    status=changed.status.value,
                    wait_reason=changed.wait_reason.value,
                    version=changed.version,
                    last_event_sequence=changed.last_event_sequence,
                    updated_at=changed.updated_at,
                    completed_at=changed.completed_at,
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project process version is stale")
            event = self.repository.append_event(
                connection,
                self._event_values(
                    process=process,
                    event_id=event_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    transition_key=transition_key,
                    schema_version=schema_version,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    source_aggregate_version=source_aggregate_version,
                    version_after=changed.version,
                    initiated_by=initiated_by,
                    executed_as=executed_as,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    payload=normalized,
                    digest=digest,
                    occurred_at=changed.updated_at,
                ),
            )
            return changed, event

    def append_fact(
        self,
        *,
        process_id: str,
        event_id: str,
        event_type: str,
        expected_version: int,
        expected_event_sequence: int,
        subject_type: str,
        subject_id: str,
        initiated_by: str,
        executed_as: str,
        correlation_id: str,
        payload: dict,
        source_aggregate_version: int | None = None,
        causation_id: str | None = None,
        schema_version: str = "v1",
        idempotency_key: str | None = None,
    ):
        idempotency_key = idempotency_key or event_id
        validate_event_contract(
            event_type=event_type,
            transition_key=None,
            schema_version=schema_version,
        )
        normalized, digest = self.repository.canonical_payload(payload)
        now = self.clock()
        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
            duplicate = self._event_retry(connection, process_id, event_id, idempotency_key)
            if duplicate is not None:
                self._require_duplicate(
                    duplicate,
                    event_id=event_id,
                    process_id=process_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    transition_key=None,
                    digest=digest,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    initiated_by=initiated_by,
                    executed_as=executed_as,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    source_aggregate_version=source_aggregate_version,
                )
                return process, duplicate
            if (
                process.version != expected_version
                or process.last_event_sequence != expected_event_sequence
            ):
                raise GovernanceConflictError("project process event cursor is stale")
            next_sequence = expected_event_sequence + 1
            updated = connection.execute(
                PROJECT_PROCESSES.update()
                .where(
                    and_(
                        PROJECT_PROCESSES.c.process_id == process_id,
                        PROJECT_PROCESSES.c.version == expected_version,
                        PROJECT_PROCESSES.c.last_event_sequence == expected_event_sequence,
                    )
                )
                .values(last_event_sequence=next_sequence, updated_at=now)
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project process event cursor is stale")
            event = self.repository.append_event(
                connection,
                self._event_values(
                    process=process,
                    event_id=event_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    transition_key=None,
                    schema_version=schema_version,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    source_aggregate_version=source_aggregate_version,
                    version_after=None,
                    initiated_by=initiated_by,
                    executed_as=executed_as,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    payload=normalized,
                    digest=digest,
                    occurred_at=now,
                ),
            )
            return self.repository.process(connection, process_id), event

    def claim_lease(self, *, process_id: str, owner: str, ttl_seconds: int = 30):
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise ValueError("project process lease ttl is invalid")
        now = self.clock()
        token = uuid4().hex
        expires = now + timedelta(seconds=ttl_seconds)
        with self.repository.transaction() as connection:
            updated = connection.execute(
                PROJECT_PROCESSES.update()
                .where(
                    and_(
                        PROJECT_PROCESSES.c.process_id == process_id,
                        or_(
                            PROJECT_PROCESSES.c.lease_expires_at.is_(None),
                            PROJECT_PROCESSES.c.lease_expires_at <= now,
                        ),
                    )
                )
                .values(lease_owner=owner, lease_token=token, lease_expires_at=expires)
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project process lease is unavailable")
            return token, expires

    def release_lease(self, *, process_id: str, owner: str, token: str) -> None:
        with self.repository.transaction() as connection:
            updated = connection.execute(
                PROJECT_PROCESSES.update()
                .where(
                    and_(
                        PROJECT_PROCESSES.c.process_id == process_id,
                        PROJECT_PROCESSES.c.lease_owner == owner,
                        PROJECT_PROCESSES.c.lease_token == token,
                    )
                )
                .values(lease_owner=None, lease_token=None, lease_expires_at=None)
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project process fencing token is stale")

    @staticmethod
    def _require_duplicate(
        event,
        *,
        event_id,
        process_id,
        event_type,
        idempotency_key,
        transition_key,
        digest,
        subject_type,
        subject_id,
        initiated_by,
        executed_as,
        correlation_id,
        causation_id,
        source_aggregate_version,
    ):
        expected = (
            event_id,
            process_id,
            event_type,
            idempotency_key,
            transition_key,
            digest,
            subject_type,
            subject_id,
            initiated_by,
            executed_as,
            correlation_id,
            causation_id,
            source_aggregate_version,
        )
        actual = (
            event.event_id,
            event.process_id,
            event.event_type,
            event.idempotency_key,
            event.transition_key,
            event.payload_sha256,
            event.subject_type,
            event.subject_id,
            event.initiated_by,
            event.executed_as,
            event.correlation_id,
            event.causation_id,
            event.source_aggregate_version,
        )
        if actual != expected:
            raise GovernanceConflictError(
                "project process event id was reused with different content"
            )

    @staticmethod
    def _event_values(
        *,
        process,
        event_id,
        event_type,
        idempotency_key,
        transition_key,
        schema_version,
        subject_type,
        subject_id,
        source_aggregate_version,
        version_after,
        initiated_by,
        executed_as,
        correlation_id,
        causation_id,
        payload,
        digest,
        occurred_at,
    ):
        return {
            "event_id": event_id,
            "process_id": process.process_id,
            "project_id": process.project_id,
            "sequence": process.last_event_sequence + 1,
            "event_type": event_type,
            "idempotency_key": idempotency_key,
            "transition_key": transition_key,
            "schema_version": schema_version,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "source_aggregate_version": source_aggregate_version,
            "process_version_before": process.version,
            "process_version_after": version_after,
            "initiated_by": initiated_by,
            "executed_as": executed_as,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "payload_json": payload,
            "payload_sha256": digest,
            "occurred_at": occurred_at,
        }

    @staticmethod
    def _event_retry(connection, process_id: str, event_id: str, idempotency_key: str):
        rows = (
            connection.execute(
                select(PROJECT_PROCESS_EVENTS).where(
                    (PROJECT_PROCESS_EVENTS.c.event_id == event_id)
                    | and_(
                        PROJECT_PROCESS_EVENTS.c.process_id == process_id,
                        PROJECT_PROCESS_EVENTS.c.idempotency_key == idempotency_key,
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 1:
            raise GovernanceConflictError(
                "project event id and idempotency key refer to different events"
            )
        return SQLAlchemyProjectProcessRepository._event(rows[0]) if rows else None
