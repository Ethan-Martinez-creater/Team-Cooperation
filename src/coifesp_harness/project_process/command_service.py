from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select

from ..errors import GovernanceConflictError
from .commands import (
    ProjectProcessCommandStatus,
    ProjectProcessCommandType,
    ProjectProcessOutboxStatus,
)
from .repository import (
    PROJECT_PROCESS_COMMANDS,
    PROJECT_PROCESS_OUTBOX,
    SQLAlchemyProjectProcessRepository,
)


class ProjectProcessCommandService:
    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

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
        normalized, _ = self.repository.canonical_payload(request)
        request_digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
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
                "based_on_process_version": based_on_process_version,
                "based_on_event_sequence": based_on_event_sequence,
                "graph_snapshot_digest": graph_snapshot_digest,
            }
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in expected.items()):
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
