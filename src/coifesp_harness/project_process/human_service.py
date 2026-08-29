from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, select

from ..errors import GovernanceConflictError
from ..product.repository import ACCOUNTS, PROJECT_MEMBERSHIPS, PROJECT_TEAMS
from .event_catalog import validate_event_contract
from .gates import (
    PROJECT_GATE_DECISIONS,
    ProjectGateStatus,
    ProjectGateType,
    ProjectInputRequestStatus,
)
from .models import TERMINAL_STATUSES, ProjectProcessWaitReason
from .repository import (
    PROJECT_GATES,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESSES,
    SQLAlchemyProjectProcessRepository,
)
from .service import ProjectProcessService
from .transitions import ProjectTransitionGuard


class HumanGateService:
    """Durable project InputRequest/Gate lifecycle with atomic process events."""

    _MACHINE_PREFIXES = ("agent:", "team-agent:", "service:")

    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None, guard=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))
        self.guard = guard or ProjectTransitionGuard(clock=self.clock)

    def create_input_request(
        self,
        *,
        request_id: str,
        process_id: str,
        work_node_id: str | None,
        requested_by_run_id: str | None,
        requested_by_agent_id: str,
        question: str,
        input_schema: dict,
        context_projection: dict,
        created_by: str,
        event_id: str,
        expected_process_version: int,
        expected_event_sequence: int,
        correlation_id: str,
        causation_id: str | None = None,
    ):
        if not question.strip():
            raise ValueError("project input question is required")
        schema, _ = self.repository.canonical_payload(input_schema)
        context, _ = self.repository.canonical_payload(context_projection)
        now = self.clock()
        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
            existing = (
                connection.execute(
                    select(PROJECT_INPUT_REQUESTS).where(
                        PROJECT_INPUT_REQUESTS.c.request_id == request_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            stable = {
                "process_id": process_id,
                "project_id": process.project_id,
                "work_node_id": work_node_id,
                "requested_by_run_id": requested_by_run_id,
                "requested_by_agent_id": requested_by_agent_id,
                "question": question.strip(),
                "input_schema_json": schema,
                "context_projection_json": context,
                "created_by": created_by,
            }
            if existing is not None:
                self._require_same(existing, stable, "project input request id was reused")
                event = self.repository.event(connection, event_id)
                if event is None or event.subject_id != request_id:
                    raise GovernanceConflictError("project input request retry is incomplete")
                return self.repository._input_request(existing), process, event
            process, transition_key, version_after = self._enter_wait(
                connection,
                process=process,
                expected_process_version=expected_process_version,
                expected_event_sequence=expected_event_sequence,
                reason=ProjectProcessWaitReason.HUMAN_INPUT,
                now=now,
            )
            row = {
                "request_id": request_id,
                **stable,
                "status": ProjectInputRequestStatus.OPEN.value,
                "response_json": None,
                "version": 1,
                "created_at": now,
                "answered_by": None,
                "answered_at": None,
                "resolution_idempotency_key": None,
                "resolution_event_id": None,
                "resolution_sha256": None,
            }
            connection.execute(PROJECT_INPUT_REQUESTS.insert().values(**row))
            event = self._append_event(
                connection,
                process_before_version=expected_process_version,
                process=process,
                event_id=event_id,
                event_type="project.input.requested",
                transition_key=transition_key,
                subject_type="project_input_request",
                subject_id=request_id,
                source_aggregate_version=1,
                version_after=version_after,
                initiated_by=created_by,
                executed_as="service:project-orchestrator",
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload={"request_id": request_id, "work_node_id": work_node_id},
                occurred_at=now,
            )
            return self.repository._input_request(row), process, event

    def create_gate(
        self,
        *,
        gate_id: str,
        process_id: str,
        gate_type: str,
        subject_type: str,
        subject_id: str,
        required_roles: tuple[str, ...],
        allowed_decisions: tuple[str, ...],
        reason: str,
        created_by: str,
        event_id: str,
        expected_process_version: int,
        expected_event_sequence: int,
        correlation_id: str,
        causation_id: str | None = None,
        cause_event_type: str | None = None,
        cause_subject_type: str | None = None,
        cause_subject_id: str | None = None,
        cause_payload: dict | None = None,
    ):
        roles = tuple(sorted({item.strip() for item in required_roles if item.strip()}))
        decisions = tuple(sorted({item.strip() for item in allowed_decisions if item.strip()}))
        if not gate_type or not subject_type or not subject_id or not roles or not decisions:
            raise ValueError("project gate contract is incomplete")
        try:
            typed_gate = ProjectGateType(gate_type)
        except ValueError as error:
            raise ValueError("project gate type is unsupported") from error
        expected_decisions = tuple(sorted(PROJECT_GATE_DECISIONS[typed_gate]))
        if decisions != expected_decisions:
            raise ValueError("project gate decisions do not match its type")
        now = self.clock()
        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
            existing = (
                connection.execute(select(PROJECT_GATES).where(PROJECT_GATES.c.gate_id == gate_id))
                .mappings()
                .one_or_none()
            )
            stable = {
                "process_id": process_id,
                "project_id": process.project_id,
                "gate_type": typed_gate.value,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "required_roles_json": list(roles),
                "allowed_decisions_json": list(decisions),
                "reason": reason,
                "created_by": created_by,
            }
            if existing is not None:
                self._require_same(existing, stable, "project gate id was reused")
                event = self.repository.event(connection, event_id)
                if event is None or event.subject_id != gate_id:
                    raise GovernanceConflictError("project gate retry is incomplete")
                return self.repository._gate(existing), process, event
            if cause_event_type is not None:
                if not cause_subject_type or not cause_subject_id or cause_payload is None:
                    raise ValueError("project gate cause event is incomplete")
                cause_event_id = f"{event_id}:cause"
                validate_event_contract(
                    event_type=cause_event_type,
                    transition_key=None,
                    schema_version="v1",
                )
                cause_payload, cause_digest = self.repository.canonical_payload(cause_payload)
                updated = connection.execute(
                    PROJECT_PROCESSES.update()
                    .where(
                        and_(
                            PROJECT_PROCESSES.c.process_id == process_id,
                            PROJECT_PROCESSES.c.version == expected_process_version,
                            PROJECT_PROCESSES.c.last_event_sequence == expected_event_sequence,
                        )
                    )
                    .values(
                        last_event_sequence=expected_event_sequence + 1,
                        updated_at=now,
                    )
                ).rowcount
                if updated != 1:
                    raise GovernanceConflictError("project process cursor is stale")
                self.repository.append_event(
                    connection,
                    ProjectProcessService._event_values(
                        process=process,
                        event_id=cause_event_id,
                        event_type=cause_event_type,
                        idempotency_key=cause_event_id,
                        transition_key=None,
                        schema_version="v1",
                        subject_type=cause_subject_type,
                        subject_id=cause_subject_id,
                        source_aggregate_version=None,
                        version_after=None,
                        initiated_by=created_by,
                        executed_as="service:project-orchestrator",
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                        payload=cause_payload,
                        digest=cause_digest,
                        occurred_at=now,
                    ),
                )
                process = self.repository.process(connection, process_id)
                expected_event_sequence += 1
            process, transition_key, version_after = self._enter_wait(
                connection,
                process=process,
                expected_process_version=expected_process_version,
                expected_event_sequence=expected_event_sequence,
                reason=ProjectProcessWaitReason.HUMAN_APPROVAL,
                now=now,
            )
            row = {
                "gate_id": gate_id,
                **stable,
                "status": ProjectGateStatus.OPEN.value,
                "decision": None,
                "version": 1,
                "created_at": now,
                "decided_by": None,
                "decided_at": None,
                "resolution_idempotency_key": None,
                "resolution_event_id": None,
                "resolution_sha256": None,
            }
            connection.execute(PROJECT_GATES.insert().values(**row))
            event = self._append_event(
                connection,
                process_before_version=expected_process_version,
                process=process,
                event_id=event_id,
                event_type="project.gate.opened",
                transition_key=transition_key,
                subject_type="project_gate",
                subject_id=gate_id,
                source_aggregate_version=1,
                version_after=version_after,
                initiated_by=created_by,
                executed_as="service:project-orchestrator",
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload={"gate_id": gate_id, "gate_type": typed_gate.value, "subject_id": subject_id},
                occurred_at=now,
            )
            return self.repository._gate(row), process, event

    def answer_input(
        self,
        *,
        request_id: str,
        response: dict,
        actor_id: str,
        idempotency_key: str,
        event_id: str,
        expected_object_version: int,
        expected_process_version: int,
        correlation_id: str,
    ):
        self._require_human(actor_id)
        response, digest = self.repository.canonical_payload(response)
        return self._resolve_input(
            request_id=request_id,
            status=ProjectInputRequestStatus.ANSWERED,
            response=response,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            digest=digest,
            event_id=event_id,
            expected_object_version=expected_object_version,
            expected_process_version=expected_process_version,
            correlation_id=correlation_id,
            event_type="human.input.provided",
            schema_version="v1",
        )

    def close_input(
        self,
        *,
        request_id: str,
        status: ProjectInputRequestStatus,
        actor_id: str,
        idempotency_key: str,
        event_id: str,
        expected_object_version: int,
        expected_process_version: int,
        correlation_id: str,
    ):
        if status not in {ProjectInputRequestStatus.CANCELLED, ProjectInputRequestStatus.EXPIRED}:
            raise ValueError("project input close status is invalid")
        self._require_human(actor_id)
        _, digest = self.repository.canonical_payload({"status": status.value})
        return self._resolve_input(
            request_id=request_id,
            status=status,
            response=None,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            digest=digest,
            event_id=event_id,
            expected_object_version=expected_object_version,
            expected_process_version=expected_process_version,
            correlation_id=correlation_id,
            event_type="project.input.closed",
            schema_version="v2",
        )

    def decide_gate(
        self,
        *,
        gate_id: str,
        decision: str,
        reason: str,
        actor_id: str,
        idempotency_key: str,
        event_id: str,
        expected_object_version: int,
        expected_process_version: int,
        correlation_id: str,
    ):
        self._require_human(actor_id)
        payload, digest = self.repository.canonical_payload(
            {"decision": decision, "reason": reason}
        )
        now = self.clock()
        with self.repository.transaction() as connection:
            row = self._gate_row(connection, gate_id)
            exact = self._resolved_retry(row, idempotency_key, event_id, digest)
            if exact:
                return self.repository._gate(row), self.repository.process(
                    connection, row["process_id"]
                ), self.repository.event(connection, event_id)
            if row["status"] != ProjectGateStatus.OPEN.value:
                raise GovernanceConflictError("project gate is already resolved")
            if row["version"] != expected_object_version:
                raise GovernanceConflictError("project gate version is stale")
            if decision not in row["allowed_decisions_json"]:
                raise GovernanceConflictError("project gate decision is not allowed")
            actor_roles = self._human_roles(
                connection, project_id=row["project_id"], actor_id=actor_id
            )
            if not set(row["required_roles_json"]).intersection(actor_roles):
                raise GovernanceConflictError("project gate decision is not authorized")
            process = self.repository.process(connection, row["process_id"])
            if process.version != expected_process_version:
                raise GovernanceConflictError("project process version is stale")
            updated = connection.execute(
                PROJECT_GATES.update()
                .where(
                    and_(
                        PROJECT_GATES.c.gate_id == gate_id,
                        PROJECT_GATES.c.version == expected_object_version,
                        PROJECT_GATES.c.status == ProjectGateStatus.OPEN.value,
                    )
                )
                .values(
                    status=ProjectGateStatus.DECIDED.value,
                    decision=decision,
                    reason=reason,
                    version=expected_object_version + 1,
                    decided_by=actor_id,
                    decided_at=now,
                    resolution_idempotency_key=idempotency_key,
                    resolution_event_id=event_id,
                    resolution_sha256=digest,
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project gate version is stale")
            event = self._append_resolution_event(
                connection,
                process=process,
                event_id=event_id,
                event_type="project.gate.decided",
                schema_version="v1",
                subject_type="project_gate",
                subject_id=gate_id,
                source_version=expected_object_version + 1,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=payload,
                now=now,
            )
            return self.repository.gate(connection, gate_id), self.repository.process(
                connection, process.process_id
            ), event

    def close_gate(
        self,
        *,
        gate_id: str,
        status: ProjectGateStatus,
        actor_id: str,
        idempotency_key: str,
        event_id: str,
        expected_object_version: int,
        expected_process_version: int,
        correlation_id: str,
    ):
        if status not in {ProjectGateStatus.CANCELLED, ProjectGateStatus.EXPIRED}:
            raise ValueError("project gate close status is invalid")
        self._require_human(actor_id)
        payload, digest = self.repository.canonical_payload({"status": status.value})
        now = self.clock()
        with self.repository.transaction() as connection:
            row = self._gate_row(connection, gate_id)
            if self._resolved_retry(row, idempotency_key, event_id, digest):
                return self.repository._gate(row), self.repository.process(
                    connection, row["process_id"]
                ), self.repository.event(connection, event_id)
            if row["status"] != ProjectGateStatus.OPEN.value:
                raise GovernanceConflictError("project gate is already resolved")
            if row["version"] != expected_object_version:
                raise GovernanceConflictError("project gate version is stale")
            actor_roles = self._human_roles(
                connection, project_id=row["project_id"], actor_id=actor_id
            )
            if not set(row["required_roles_json"]).intersection(actor_roles):
                raise GovernanceConflictError("project gate close is not authorized")
            process = self.repository.process(connection, row["process_id"])
            if process.version != expected_process_version:
                raise GovernanceConflictError("project process version is stale")
            updated = connection.execute(
                PROJECT_GATES.update()
                .where(
                    and_(
                        PROJECT_GATES.c.gate_id == gate_id,
                        PROJECT_GATES.c.version == expected_object_version,
                        PROJECT_GATES.c.status == ProjectGateStatus.OPEN.value,
                    )
                )
                .values(
                    status=status.value,
                    version=expected_object_version + 1,
                    decided_by=actor_id,
                    decided_at=now,
                    resolution_idempotency_key=idempotency_key,
                    resolution_event_id=event_id,
                    resolution_sha256=digest,
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project gate version is stale")
            event = self._append_resolution_event(
                connection,
                process=process,
                event_id=event_id,
                event_type="project.gate.closed",
                schema_version="v2",
                subject_type="project_gate",
                subject_id=gate_id,
                source_version=expected_object_version + 1,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=payload,
                now=now,
            )
            return self.repository.gate(connection, gate_id), self.repository.process(
                connection, process.process_id
            ), event

    def _resolve_input(
        self, *, request_id, status, response, actor_id, idempotency_key, digest,
        event_id, expected_object_version, expected_process_version, correlation_id,
        event_type, schema_version,
    ):
        now = self.clock()
        with self.repository.transaction() as connection:
            row = (
                connection.execute(
                    select(PROJECT_INPUT_REQUESTS).where(
                        PROJECT_INPUT_REQUESTS.c.request_id == request_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise GovernanceConflictError("project input request is unavailable")
            if self._resolved_retry(row, idempotency_key, event_id, digest):
                return self.repository._input_request(row), self.repository.process(
                    connection, row["process_id"]
                ), self.repository.event(connection, event_id)
            if row["status"] != ProjectInputRequestStatus.OPEN.value:
                raise GovernanceConflictError("project input request is already resolved")
            if row["version"] != expected_object_version:
                raise GovernanceConflictError("project input request version is stale")
            self._human_roles(connection, project_id=row["project_id"], actor_id=actor_id)
            process = self.repository.process(connection, row["process_id"])
            if process.version != expected_process_version:
                raise GovernanceConflictError("project process version is stale")
            updated = connection.execute(
                PROJECT_INPUT_REQUESTS.update()
                .where(
                    and_(
                        PROJECT_INPUT_REQUESTS.c.request_id == request_id,
                        PROJECT_INPUT_REQUESTS.c.version == expected_object_version,
                        PROJECT_INPUT_REQUESTS.c.status == ProjectInputRequestStatus.OPEN.value,
                    )
                )
                .values(
                    status=status.value,
                    response_json=response,
                    version=expected_object_version + 1,
                    answered_by=actor_id,
                    answered_at=now,
                    resolution_idempotency_key=idempotency_key,
                    resolution_event_id=event_id,
                    resolution_sha256=digest,
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project input request version is stale")
            payload = response if response is not None else {"status": status.value}
            event = self._append_resolution_event(
                connection,
                process=process,
                event_id=event_id,
                event_type=event_type,
                schema_version=schema_version,
                subject_type="project_input_request",
                subject_id=request_id,
                source_version=expected_object_version + 1,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=payload,
                now=now,
            )
            return self.repository.input_request(connection, request_id), self.repository.process(
                connection, process.process_id
            ), event

    def _enter_wait(self, connection, *, process, expected_process_version, expected_event_sequence, reason, now):
        if process.status in TERMINAL_STATUSES:
            raise GovernanceConflictError("terminal project process is immutable")
        if (
            process.version != expected_process_version
            or process.last_event_sequence != expected_event_sequence
        ):
            raise GovernanceConflictError("project process cursor is stale")
        guarded, transition_key = self.guard.enter_human_wait(
            process=process,
            reason=reason,
            expected_version=expected_process_version,
        )
        should_change = transition_key is not None
        next_version = guarded.version
        values = {"last_event_sequence": process.last_event_sequence + 1, "updated_at": now}
        version_after = None
        if should_change:
            values.update(
                status=guarded.status.value,
                wait_reason=guarded.wait_reason.value,
                version=guarded.version,
            )
            version_after = next_version
        updated = connection.execute(
            PROJECT_PROCESSES.update()
            .where(
                and_(
                    PROJECT_PROCESSES.c.process_id == process.process_id,
                    PROJECT_PROCESSES.c.version == expected_process_version,
                    PROJECT_PROCESSES.c.last_event_sequence == expected_event_sequence,
                )
            )
            .values(**values)
        ).rowcount
        if updated != 1:
            raise GovernanceConflictError("project process cursor is stale")
        return self.repository.process(connection, process.process_id), transition_key, version_after

    def _append_event(self, connection, *, process_before_version, process, event_id, event_type,
                      transition_key, subject_type, subject_id, source_aggregate_version,
                      version_after, initiated_by, executed_as, correlation_id, causation_id,
                      payload, occurred_at):
        validate_event_contract(
            event_type=event_type,
            transition_key=transition_key,
            schema_version="v1",
        )
        normalized, digest = self.repository.canonical_payload(payload)
        values = ProjectProcessService._event_values(
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
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=normalized,
            digest=digest,
            occurred_at=occurred_at,
        )
        values["process_version_before"] = process_before_version
        values["sequence"] = process.last_event_sequence
        return self.repository.append_event(connection, values)

    def _append_resolution_event(self, connection, *, process, event_id, event_type,
                                 schema_version, subject_type, subject_id, source_version,
                                 actor_id, correlation_id, payload, now):
        validate_event_contract(
            event_type=event_type,
            transition_key=None,
            schema_version=schema_version,
        )
        normalized, digest = self.repository.canonical_payload(payload)
        existing = self.repository.event(connection, event_id)
        if existing is not None:
            ProjectProcessService._require_duplicate(
                existing,
                event_id=event_id,
                process_id=process.process_id,
                event_type=event_type,
                idempotency_key=event_id,
                transition_key=None,
                digest=digest,
                subject_type=subject_type,
                subject_id=subject_id,
                initiated_by=actor_id,
                executed_as=actor_id,
                correlation_id=correlation_id,
                causation_id=None,
                source_aggregate_version=source_version,
            )
            return existing
        updated = connection.execute(
            PROJECT_PROCESSES.update()
            .where(
                and_(
                    PROJECT_PROCESSES.c.process_id == process.process_id,
                    PROJECT_PROCESSES.c.version == process.version,
                    PROJECT_PROCESSES.c.last_event_sequence == process.last_event_sequence,
                )
            )
            .values(last_event_sequence=process.last_event_sequence + 1, updated_at=now)
        ).rowcount
        if updated != 1:
            raise GovernanceConflictError("project process event cursor is stale")
        values = ProjectProcessService._event_values(
            process=process,
            event_id=event_id,
            event_type=event_type,
            idempotency_key=event_id,
            transition_key=None,
            schema_version=schema_version,
            subject_type=subject_type,
            subject_id=subject_id,
            source_aggregate_version=source_version,
            version_after=None,
            initiated_by=actor_id,
            executed_as=actor_id,
            correlation_id=correlation_id,
            causation_id=None,
            payload=normalized,
            digest=digest,
            occurred_at=now,
        )
        return self.repository.append_event(connection, values)

    @staticmethod
    def _require_same(row, expected, message):
        if any(row[key] != value for key, value in expected.items()):
            raise GovernanceConflictError(message)

    @staticmethod
    def _resolved_retry(row, idempotency_key, event_id, digest):
        stored = row["resolution_idempotency_key"]
        if stored is None:
            return False
        if (
            stored != idempotency_key
            or row["resolution_event_id"] != event_id
            or row["resolution_sha256"] != digest
        ):
            raise GovernanceConflictError("project human resolution retry conflicts")
        return True

    @staticmethod
    def _gate_row(connection, gate_id):
        row = (
            connection.execute(select(PROJECT_GATES).where(PROJECT_GATES.c.gate_id == gate_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise GovernanceConflictError("project gate is unavailable")
        return row

    @classmethod
    def _require_human(cls, actor_id: str) -> None:
        if not actor_id or actor_id.startswith(cls._MACHINE_PREFIXES):
            raise GovernanceConflictError("project human action requires a human principal")

    @staticmethod
    def _human_roles(connection, *, project_id: str, actor_id: str) -> frozenset[str]:
        membership = PROJECT_MEMBERSHIPS.alias("human_gate_membership")
        row = (
            connection.execute(
                select(ACCOUNTS.c.team_role, membership.c.role)
                .select_from(
                    ACCOUNTS.join(
                        PROJECT_TEAMS,
                        ACCOUNTS.c.team_id == PROJECT_TEAMS.c.team_id,
                    ).outerjoin(
                        membership,
                        and_(
                            ACCOUNTS.c.account_id == membership.c.account_id,
                            PROJECT_TEAMS.c.project_id == membership.c.project_id,
                        ),
                    )
                )
                .where(
                    and_(
                        ACCOUNTS.c.account_id == actor_id,
                        ACCOUNTS.c.enabled.is_(True),
                        ACCOUNTS.c.registration_status == "active",
                        PROJECT_TEAMS.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise GovernanceConflictError("project human action requires active membership")
        return frozenset(item for item in (row["team_role"], row["role"]) if item)
