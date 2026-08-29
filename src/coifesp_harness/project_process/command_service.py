from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Connection

from ..errors import GovernanceConflictError
from .commands import (
    ProjectOrchestrationDecision,
    ProjectOrchestrationDecisionStatus,
    ProjectProcessCommandStatus,
    ProjectProcessCommandType,
    ProjectProcessOutboxStatus,
)
from .repository import (
    PROJECT_ORCHESTRATION_DECISIONS,
    PROJECT_PROCESS_COMMANDS,
    PROJECT_PROCESS_OUTBOX,
    PROJECT_PROCESSES,
    SQLAlchemyProjectProcessRepository,
)


class ProjectProcessCommandService:
    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def record_decision(
        self,
        *,
        decision_id: str,
        process_id: str,
        reason: str,
        based_on_process_version: int,
        based_on_event_sequence: int,
        graph_snapshot_digest: str,
        commands: Iterable[Mapping | object] = (),
        project_id: str | None = None,
        decision_json: Mapping | None = None,
        decision: Mapping | None = None,
        decision_digest: str | None = None,
        command_batch_digest: str | None = None,
        commands_digest: str | None = None,
        current_graph_snapshot_digest: str | None = None,
        current_graph_digest: str | None = None,
        mutation_fence=None,
    ) -> ProjectOrchestrationDecision:
        """Atomically record one snapshot-guarded decision and its commands.

        The method deliberately accepts only already-structured command input.
        It does not interpret free text or call a model.  Every command is
        normalized and checked before the transaction mutates either the
        decision table or the process orchestration cursor.
        """

        decision_id = self._identifier(decision_id, "decision_id", 128)
        reason = self._reason(reason)
        if not isinstance(based_on_process_version, int) or isinstance(
            based_on_process_version, bool
        ) or based_on_process_version < 1:
            raise ValueError("based_on_process_version must be a positive integer")
        if not isinstance(based_on_event_sequence, int) or isinstance(
            based_on_event_sequence, bool
        ) or based_on_event_sequence < 0:
            raise ValueError("based_on_event_sequence must be a non-negative integer")
        graph_snapshot_digest = self._digest_text(graph_snapshot_digest, "graph_snapshot_digest")
        if decision_json is not None and decision is not None and dict(decision_json) != dict(decision):
            raise GovernanceConflictError("decision JSON aliases disagree")
        raw_decision_json = decision_json if decision_json is not None else decision
        if raw_decision_json is None:
            raw_decision_json = {
                "action": "record",
                "reason": reason,
                "work_id": None,
                "transition_key": None,
            }
        elif not isinstance(raw_decision_json, Mapping):
            raise TypeError("decision_json must be an object")
        else:
            raw_decision_json = dict(raw_decision_json)
            if "reason" in raw_decision_json and raw_decision_json["reason"] != reason:
                raise GovernanceConflictError("decision JSON reason does not match decision reason")
            raw_decision_json.setdefault("reason", reason)
        normalized_decision_json, calculated_decision_digest = self.repository.canonical_payload(
            raw_decision_json
        )
        if decision_digest is not None and decision_digest != calculated_decision_digest:
            raise GovernanceConflictError("decision digest does not match decision JSON")
        current_graph_snapshot_digest = current_graph_snapshot_digest or current_graph_digest
        if current_graph_snapshot_digest is not None:
            current_graph_snapshot_digest = self._digest_text(
                current_graph_snapshot_digest, "current_graph_snapshot_digest"
            )
        command_specs = self._normalize_commands(
            commands,
            decision_id=decision_id,
            process_id=process_id,
            project_id=project_id,
            based_on_process_version=based_on_process_version,
            based_on_event_sequence=based_on_event_sequence,
            graph_snapshot_digest=graph_snapshot_digest,
        )
        calculated_batch_digest = self.compute_command_batch_digest(command_specs)
        supplied_batch_digests = {
            value
            for value in (command_batch_digest, commands_digest)
            if value is not None
        }
        if len(supplied_batch_digests) > 1:
            raise GovernanceConflictError("command batch digest aliases disagree")
        if supplied_batch_digests:
            supplied = next(iter(supplied_batch_digests))
            if supplied != calculated_batch_digest:
                raise GovernanceConflictError("command batch digest does not match commands")

        now = self.clock()
        with self.repository.transaction() as connection:
            if mutation_fence is not None:
                mutation_fence(connection)
            process = self.repository.process(connection, process_id)
            if project_id is not None and project_id != process.project_id:
                raise GovernanceConflictError("project process belongs to a different project")
            if project_id is None:
                command_specs = tuple(
                    {**spec, "project_id": process.project_id} for spec in command_specs
                )

            existing_decision = self.repository.decision(connection, decision_id)
            if existing_decision is not None:
                self._assert_decision_content(
                    existing_decision,
                    process_id=process_id,
                    project_id=process.project_id,
                    reason=reason,
                    based_on_process_version=based_on_process_version,
                    based_on_event_sequence=based_on_event_sequence,
                    graph_snapshot_digest=graph_snapshot_digest,
                    command_batch_digest=calculated_batch_digest,
                    decision_json=normalized_decision_json,
                    decision_digest=calculated_decision_digest,
                )
                self._assert_existing_commands(
                    connection,
                    command_specs,
                    decision_id=decision_id,
                    allow_missing=False,
                )
                return existing_decision

            # Validate all command ids and payloads before any write.  A
            # command id is globally idempotent, so a match under another
            # decision is a hard conflict rather than an opportunity to reuse
            # a row.
            existing_commands = self._existing_commands(connection, command_specs)
            self._assert_existing_commands(
                connection,
                command_specs,
                decision_id=decision_id,
                allow_missing=True,
                existing=existing_commands,
            )

            stale = (
                process.version != based_on_process_version
                or process.last_event_sequence != based_on_event_sequence
                or (
                    current_graph_snapshot_digest is not None
                    and current_graph_snapshot_digest != graph_snapshot_digest
                )
            )
            if stale:
                # A stale batch has no command rows.  Existing rows with these
                # ids would mean a prior partial write or a reused id, both of
                # which are fail-closed conflicts.
                if existing_commands:
                    raise GovernanceConflictError(
                        "stale decision command ids already exist"
                    )
                row = self._decision_values(
                    decision_id=decision_id,
                    process_id=process_id,
                    project_id=process.project_id,
                    reason=reason,
                    based_on_process_version=based_on_process_version,
                    based_on_event_sequence=based_on_event_sequence,
                    graph_snapshot_digest=graph_snapshot_digest,
                    command_batch_digest=calculated_batch_digest,
                    decision_json=normalized_decision_json,
                    decision_digest=calculated_decision_digest,
                    status=ProjectOrchestrationDecisionStatus.STALE,
                    now=now,
                )
                connection.execute(PROJECT_ORCHESTRATION_DECISIONS.insert().values(**row))
                return self.repository.decision(connection, decision_id)

            self._advance_orchestration_cursor(
                connection,
                process=process,
                target=based_on_event_sequence,
            )
            decision_row = self._decision_values(
                decision_id=decision_id,
                process_id=process_id,
                project_id=process.project_id,
                reason=reason,
                based_on_process_version=based_on_process_version,
                based_on_event_sequence=based_on_event_sequence,
                graph_snapshot_digest=graph_snapshot_digest,
                command_batch_digest=calculated_batch_digest,
                decision_json=normalized_decision_json,
                decision_digest=calculated_decision_digest,
                status=ProjectOrchestrationDecisionStatus.PENDING,
                now=now,
            )
            connection.execute(PROJECT_ORCHESTRATION_DECISIONS.insert().values(**decision_row))
            for spec in command_specs:
                if spec["command_id"] in existing_commands:
                    continue
                connection.execute(
                    PROJECT_PROCESS_COMMANDS.insert().values(
                        **self._command_values(spec, status=ProjectProcessCommandStatus.PENDING, now=now)
                    )
                )
            return self.repository.decision(connection, decision_id)

    # A short alias is convenient for orchestration code and keeps the batch
    # operation discoverable without weakening the explicit record_decision API.
    record_batch = record_decision

    def finish_decision(
        self,
        *,
        decision_id: str,
        status: ProjectOrchestrationDecisionStatus,
    ) -> ProjectOrchestrationDecision:
        """Idempotently close a pending decision batch."""

        try:
            status = ProjectOrchestrationDecisionStatus(status)
        except (TypeError, ValueError) as exc:
            raise ValueError("project orchestration decision terminal status is invalid") from exc
        if status not in {
            ProjectOrchestrationDecisionStatus.APPLIED,
            ProjectOrchestrationDecisionStatus.REJECTED,
        }:
            raise ValueError("project orchestration decision terminal status is invalid")
        decision_id = self._identifier(decision_id, "decision_id", 128)
        now = self.clock()
        with self.repository.transaction() as connection:
            decision = self.repository.decision(connection, decision_id)
            if decision is None:
                raise GovernanceConflictError("project orchestration decision is unavailable")
            if decision.status is status:
                return decision
            if decision.status is not ProjectOrchestrationDecisionStatus.PENDING:
                raise GovernanceConflictError("project orchestration decision is already terminal")
            updated = connection.execute(
                PROJECT_ORCHESTRATION_DECISIONS.update()
                .where(
                    and_(
                        PROJECT_ORCHESTRATION_DECISIONS.c.decision_id == decision_id,
                        PROJECT_ORCHESTRATION_DECISIONS.c.status
                        == ProjectOrchestrationDecisionStatus.PENDING.value,
                    )
                )
                .values(status=status.value, applied_at=now)
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError(
                    "project orchestration decision state changed concurrently"
                )
            return self.repository.decision(connection, decision_id)

    finish_orchestration_decision = finish_decision

    @staticmethod
    def compute_command_batch_digest(command_specs: Iterable[Mapping]) -> str:
        """Return a stable digest for a normalized command batch.

        Command order is intentionally ignored: command ids define identity,
        while the request digest captures the complete replay payload.
        """

        material = []
        for spec in sorted(command_specs, key=lambda item: item["command_id"]):
            material.append(
                {
                    "command_id": spec["command_id"],
                    "command_type": spec["command_type"],
                    "request_digest": spec["request_digest"],
                    "request_json": spec["request_json"],
                }
            )
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _normalize_commands(
        self,
        commands: Iterable[Mapping | object],
        *,
        decision_id: str,
        process_id: str,
        project_id: str | None,
        based_on_process_version: int,
        based_on_event_sequence: int,
        graph_snapshot_digest: str,
    ) -> tuple[dict, ...]:
        if commands is None:
            raise TypeError("commands must be an iterable, not None")
        try:
            iterator = iter(commands)
        except TypeError as exc:
            raise TypeError("commands must be an iterable") from exc
        normalized: dict[str, dict] = {}
        for item in iterator:
            source = dict(item) if isinstance(item, Mapping) else self._command_object(item)
            command_id = self._identifier(source.get("command_id"), "command_id", 128)
            raw_type = source.get("command_type")
            try:
                command_type = ProjectProcessCommandType(raw_type).value
            except (TypeError, ValueError) as exc:
                raise ValueError("project process command type is invalid") from exc
            request = source.get("request_json", source.get("request", source.get("payload")))
            if request is None:
                raise ValueError("project process command request is required")
            request_json, request_digest = self.repository.canonical_payload(request)
            for key, expected in (
                ("process_id", process_id),
                ("project_id", project_id),
                ("decision_id", decision_id),
                ("based_on_process_version", based_on_process_version),
                ("based_on_event_sequence", based_on_event_sequence),
                ("graph_snapshot_digest", graph_snapshot_digest),
            ):
                if key in source and source[key] is not None and source[key] != expected:
                    raise GovernanceConflictError(f"command {key} does not match decision snapshot")
            if "request_digest" in source and source["request_digest"] != request_digest:
                raise GovernanceConflictError("command request digest does not match payload")
            if "status" in source and source["status"] not in {
                None,
                ProjectProcessCommandStatus.PENDING,
                ProjectProcessCommandStatus.PENDING.value,
            }:
                raise ValueError("new decision commands must be PENDING")
            if source.get("result_subject_id") is not None or source.get("applied_at") is not None:
                raise ValueError("new decision commands cannot contain terminal fields")
            spec = {
                "command_id": command_id,
                "process_id": process_id,
                "project_id": project_id,
                "decision_id": decision_id,
                "command_type": command_type,
                "request_digest": request_digest,
                "request_json": request_json,
                "based_on_process_version": based_on_process_version,
                "based_on_event_sequence": based_on_event_sequence,
                "graph_snapshot_digest": graph_snapshot_digest,
            }
            previous = normalized.get(command_id)
            if previous is not None and previous != spec:
                raise GovernanceConflictError("command id was repeated with different content")
            normalized[command_id] = spec
        return tuple(normalized[key] for key in sorted(normalized))

    @staticmethod
    def _command_object(item: object) -> dict:
        fields = (
            "command_id",
            "process_id",
            "project_id",
            "decision_id",
            "command_type",
            "request_json",
            "request_digest",
            "based_on_process_version",
            "based_on_event_sequence",
            "graph_snapshot_digest",
            "status",
            "result_subject_id",
            "applied_at",
        )
        if item is None:
            raise TypeError("command must be an object")
        return {field: getattr(item, field) for field in fields if hasattr(item, field)}

    def _existing_commands(self, connection: Connection, specs: Iterable[Mapping]) -> dict:
        return {
            spec["command_id"]: command
            for spec in specs
            if (command := self.repository.command(connection, spec["command_id"])) is not None
        }

    def _assert_existing_commands(
        self,
        connection: Connection,
        specs: Iterable[Mapping],
        *,
        decision_id: str,
        allow_missing: bool,
        existing: dict | None = None,
    ) -> None:
        existing = existing if existing is not None else self._existing_commands(connection, specs)
        expected_specs = {spec["command_id"]: spec for spec in specs}
        for command_id, command in existing.items():
            spec = expected_specs.get(command_id)
            if spec is None or command.decision_id != decision_id:
                raise GovernanceConflictError("project command id was reused with different content")
            if any(
                getattr(command, key) != spec[key]
                for key in (
                    "process_id",
                    "project_id",
                    "decision_id",
                    "command_type",
                    "request_digest",
                    "based_on_process_version",
                    "based_on_event_sequence",
                    "graph_snapshot_digest",
                )
            ) or command.request_json != spec["request_json"]:
                raise GovernanceConflictError("project command id was reused with different content")
            if command.status is ProjectProcessCommandStatus.STALE:
                raise GovernanceConflictError("stale command cannot be reused in a fresh decision")
        if not allow_missing and len(existing) != len(expected_specs):
            raise GovernanceConflictError("decision command batch is incomplete")

    @staticmethod
    def _assert_decision_content(
        decision: ProjectOrchestrationDecision,
        *,
        process_id: str,
        project_id: str,
        reason: str,
        based_on_process_version: int,
        based_on_event_sequence: int,
        graph_snapshot_digest: str,
        command_batch_digest: str,
        decision_json: Mapping,
        decision_digest: str,
    ) -> None:
        expected = {
            "process_id": process_id,
            "project_id": project_id,
            "reason": reason,
            "based_on_process_version": based_on_process_version,
            "based_on_event_sequence": based_on_event_sequence,
            "graph_snapshot_digest": graph_snapshot_digest,
            "command_batch_digest": command_batch_digest,
            "decision_json": decision_json,
            "decision_digest": decision_digest,
        }
        if any(getattr(decision, key) != value for key, value in expected.items()):
            raise GovernanceConflictError("decision id was reused with different content")

    @staticmethod
    def _decision_values(
        *,
        decision_id: str,
        process_id: str,
        project_id: str,
        reason: str,
        based_on_process_version: int,
        based_on_event_sequence: int,
        graph_snapshot_digest: str,
        command_batch_digest: str,
        decision_json: Mapping,
        decision_digest: str,
        status: ProjectOrchestrationDecisionStatus,
        now: datetime,
    ) -> dict:
        return {
            "decision_id": decision_id,
            "process_id": process_id,
            "project_id": project_id,
            "reason": reason,
            "based_on_process_version": based_on_process_version,
            "based_on_event_sequence": based_on_event_sequence,
            "graph_snapshot_digest": graph_snapshot_digest,
            "command_batch_digest": command_batch_digest,
            "decision_json": decision_json,
            "decision_digest": decision_digest,
            "status": status.value,
            "created_at": now,
            "applied_at": now if status is not ProjectOrchestrationDecisionStatus.PENDING else None,
        }

    @staticmethod
    def _command_values(spec: Mapping, *, status: ProjectProcessCommandStatus, now: datetime) -> dict:
        return {
            **spec,
            "status": status.value,
            "result_subject_id": None,
            "created_at": now,
            "applied_at": None if status is ProjectProcessCommandStatus.PENDING else now,
        }

    @staticmethod
    def _advance_orchestration_cursor(connection: Connection, *, process, target: int) -> None:
        if target > process.last_event_sequence:
            raise GovernanceConflictError("orchestration cursor cannot exceed process events")
        if target < process.last_orchestration_sequence:
            raise GovernanceConflictError("orchestration cursor cannot move backwards")
        if target == process.last_orchestration_sequence:
            return
        updated = connection.execute(
            PROJECT_PROCESSES.update()
            .where(
                and_(
                    PROJECT_PROCESSES.c.process_id == process.process_id,
                    PROJECT_PROCESSES.c.version == process.version,
                    PROJECT_PROCESSES.c.last_event_sequence == process.last_event_sequence,
                    PROJECT_PROCESSES.c.last_orchestration_sequence
                    == process.last_orchestration_sequence,
                    PROJECT_PROCESSES.c.last_orchestration_sequence < target,
                    PROJECT_PROCESSES.c.last_event_sequence >= target,
                )
            )
            .values(last_orchestration_sequence=target)
        ).rowcount
        if updated != 1:
            raise GovernanceConflictError("project orchestration cursor is stale")

    @staticmethod
    def _identifier(value, name: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} is invalid")
        return value

    @staticmethod
    def _reason(value) -> str:
        if hasattr(value, "value"):
            value = value.value
        if not isinstance(value, str):
            raise TypeError("decision reason must be text")
        value = value.strip()
        if not value or len(value) > 512:
            raise ValueError("decision reason is invalid")
        return value

    @staticmethod
    def _digest_text(value, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 71:
            raise ValueError(f"{name} is invalid")
        return value

    def record(
        self,
        *,
        command_id: str,
        process_id: str,
        decision_id: str,
        command_type: ProjectProcessCommandType,
        request: dict,
        based_on_process_version: int,
        based_on_event_sequence: int,
        graph_snapshot_digest: str,
    ):
        normalized, request_digest = self.repository.canonical_payload(request)
        now = self.clock()
        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
            existing = self.repository.command(connection, command_id)
            expected = {
                "process_id": process_id,
                "project_id": process.project_id,
                "decision_id": decision_id,
                "command_type": ProjectProcessCommandType(command_type).value,
                "request_digest": request_digest,
                "request_json": normalized,
                "based_on_process_version": based_on_process_version,
                "based_on_event_sequence": based_on_event_sequence,
                "graph_snapshot_digest": graph_snapshot_digest,
            }
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in expected.items()):
                    raise GovernanceConflictError("project command id was reused with different content")
                if existing.request_json != normalized:
                    raise GovernanceConflictError("project command id was reused with different content")
                return existing
            stale = (
                process.version != based_on_process_version
                or process.last_event_sequence != based_on_event_sequence
            )
            row = {
                "command_id": command_id,
                **expected,
                "status": (
                    ProjectProcessCommandStatus.STALE.value
                    if stale
                    else ProjectProcessCommandStatus.PENDING.value
                ),
                "result_subject_id": None,
                "created_at": now,
                "applied_at": now if stale else None,
            }
            connection.execute(PROJECT_PROCESS_COMMANDS.insert().values(**row))
            return self.repository._command(row)

    def finish(
        self,
        *,
        command_id: str,
        status: ProjectProcessCommandStatus,
        result_subject_id: str | None,
    ):
        if status not in {
            ProjectProcessCommandStatus.APPLIED,
            ProjectProcessCommandStatus.REJECTED,
        }:
            raise ValueError("project command terminal status is invalid")
        now = self.clock()
        with self.repository.transaction() as connection:
            command = self.repository.command(connection, command_id)
            if command is None:
                raise GovernanceConflictError("project command is unavailable")
            if command.status is not ProjectProcessCommandStatus.PENDING:
                if command.status is status and command.result_subject_id == result_subject_id:
                    return command
                raise GovernanceConflictError("project command is already terminal")
            updated = connection.execute(
                PROJECT_PROCESS_COMMANDS.update()
                .where(
                    and_(
                        PROJECT_PROCESS_COMMANDS.c.command_id == command_id,
                        PROJECT_PROCESS_COMMANDS.c.status == ProjectProcessCommandStatus.PENDING.value,
                    )
                )
                .values(status=status.value, result_subject_id=result_subject_id, applied_at=now)
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project command state changed concurrently")
            return self.repository.command(connection, command_id)


class ProjectProcessOutboxService:
    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def claim(self, *, owner: str, ttl_seconds: int = 30):
        if not owner or ttl_seconds < 1 or ttl_seconds > 300:
            raise ValueError("project outbox claim is invalid")
        now = self.clock()
        token = uuid4().hex
        expires = now + timedelta(seconds=ttl_seconds)
        with self.repository.transaction() as connection:
            row = (
                connection.execute(
                    select(PROJECT_PROCESS_OUTBOX)
                    .where(
                        and_(
                            PROJECT_PROCESS_OUTBOX.c.available_at <= now,
                            or_(
                                PROJECT_PROCESS_OUTBOX.c.status.in_(
                                    [
                                        ProjectProcessOutboxStatus.PENDING.value,
                                        ProjectProcessOutboxStatus.FAILED.value,
                                    ]
                                ),
                                and_(
                                    PROJECT_PROCESS_OUTBOX.c.status
                                    == ProjectProcessOutboxStatus.PUBLISHING.value,
                                    PROJECT_PROCESS_OUTBOX.c.lease_expires_at <= now,
                                ),
                            ),
                        )
                    )
                    .order_by(PROJECT_PROCESS_OUTBOX.c.available_at, PROJECT_PROCESS_OUTBOX.c.outbox_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            connection.execute(
                PROJECT_PROCESS_OUTBOX.update()
                .where(PROJECT_PROCESS_OUTBOX.c.outbox_id == row["outbox_id"])
                .values(
                    status=ProjectProcessOutboxStatus.PUBLISHING.value,
                    attempt_count=row["attempt_count"] + 1,
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=expires,
                    last_error=None,
                )
            )
            return self.repository.outbox_entry(connection, row["outbox_id"])

    def publish_succeeded(self, *, outbox_id: str, owner: str, token: str):
        now = self.clock()
        with self.repository.transaction() as connection:
            updated = connection.execute(
                PROJECT_PROCESS_OUTBOX.update()
                .where(
                    and_(
                        PROJECT_PROCESS_OUTBOX.c.outbox_id == outbox_id,
                        PROJECT_PROCESS_OUTBOX.c.status == ProjectProcessOutboxStatus.PUBLISHING.value,
                        PROJECT_PROCESS_OUTBOX.c.lease_owner == owner,
                        PROJECT_PROCESS_OUTBOX.c.lease_token == token,
                    )
                )
                .values(
                    status=ProjectProcessOutboxStatus.PUBLISHED.value,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    published_at=now,
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project outbox fencing token is stale")
            return self.repository.outbox_entry(connection, outbox_id)

    def publish_failed(self, *, outbox_id: str, owner: str, token: str, error: str, retry_after_seconds: int = 5):
        now = self.clock()
        with self.repository.transaction() as connection:
            updated = connection.execute(
                PROJECT_PROCESS_OUTBOX.update()
                .where(
                    and_(
                        PROJECT_PROCESS_OUTBOX.c.outbox_id == outbox_id,
                        PROJECT_PROCESS_OUTBOX.c.status == ProjectProcessOutboxStatus.PUBLISHING.value,
                        PROJECT_PROCESS_OUTBOX.c.lease_owner == owner,
                        PROJECT_PROCESS_OUTBOX.c.lease_token == token,
                    )
                )
                .values(
                    status=ProjectProcessOutboxStatus.FAILED.value,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=now + timedelta(seconds=max(1, retry_after_seconds)),
                    last_error=error[:2000],
                )
            ).rowcount
            if updated != 1:
                raise GovernanceConflictError("project outbox fencing token is stale")
            return self.repository.outbox_entry(connection, outbox_id)
