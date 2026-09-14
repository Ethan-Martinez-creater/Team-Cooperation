from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..audit import InMemoryAuditSink
from ..errors import (
    GovernanceConflictError,
    IdempotencyConflict,
    PolicyDenied,
    ResourceNotFound,
)
from ..idempotency import ClaimStatus
from ..security import Classification, Principal
from .governance import GovernanceBoard
from .governance_models import BoardMember, CollaborationRole, DiscussionKind
from .repository import SQLAlchemyGovernanceRepository


@dataclass(frozen=True, slots=True)
class GovernanceCommandResult:
    program_id: str
    aggregate_version: int
    duplicate: bool


class AssignmentImpactGuard(Protocol):
    def assert_assignment_verifiable(
        self, connection, *, program_id: str, assignment_id: str
    ) -> None: ...


class AssignmentArtifactGuard(Protocol):
    def assert_assignment_artifacts(self, connection, *, principal: Principal,
        board: GovernanceBoard, assignment_id: str,
        artifact_refs: tuple[str, ...]) -> None: ...


class GovernanceService:
    """Authenticated command boundary for the durable governance aggregate."""

    def __init__(
        self,
        repository: SQLAlchemyGovernanceRepository,
        *,
        impact_guard: AssignmentImpactGuard | None = None,
        artifact_guard: AssignmentArtifactGuard | None = None,
        allow_legacy_assignment_writes: bool = True,
    ) -> None:
        self.repository = repository
        self.impact_guard = impact_guard
        self.artifact_guard = artifact_guard
        self.allow_legacy_assignment_writes = allow_legacy_assignment_writes

    def list_programs(
        self, *, principal: Principal, limit: int = 100
    ) -> tuple[dict[str, object], ...]:
        return self.repository.list_program_summaries(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            limit=limit,
        )

    def list_assignments(
        self, *, principal: Principal, limit: int = 200
    ) -> tuple[dict[str, object], ...]:
        return self.repository.list_assignment_summaries(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            limit=limit,
        )

    def create_program(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        title: str,
        objective: str,
        classification: Classification,
        compartments: frozenset[str],
    ) -> GovernanceCommandResult:
        if not principal.roles.intersection({"collaboration_creator", "platform_administrator"}):
            raise PolicyDenied("creating a collaboration program requires a platform role")
        if principal.clearance < classification or not compartments.issubset(
            principal.compartments
        ):
            raise PolicyDenied("principal cannot create a program above its information access")
        command_type = "program.create"
        digest = _command_digest(
            command_type,
            {
                "program_id": program_id,
                "title": title,
                "objective": objective,
                "classification": int(classification),
                "compartments": sorted(compartments),
            },
        )
        with self.repository.engine.begin() as connection:
            claim = self.repository.claim_command_in_transaction(
                connection,
                tenant_id=principal.tenant_id,
                idempotency_key=idempotency_key,
                program_id=program_id,
                command_type=command_type,
                request_digest=digest,
            )
            duplicate = self._duplicate_result(claim, program_id)
            if duplicate is not None:
                return duplicate
            sink = InMemoryAuditSink()
            board = GovernanceBoard(
                program_id=program_id,
                owner_tenant_id=principal.tenant_id,
                title=title,
                objective=objective,
                classification=classification,
                compartments=compartments,
                audit=sink,
            )
            board.add_member(
                BoardMember(
                    principal_id=principal.principal_id,
                    tenant_id=principal.tenant_id,
                    role=CollaborationRole.LEAD,
                )
            )
            version = self.repository.create_in_transaction(
                connection,
                board=board,
                actor_id=principal.principal_id,
                events=tuple(sink.events),
            )
            self.repository.complete_command_in_transaction(
                connection,
                tenant_id=principal.tenant_id,
                idempotency_key=idempotency_key,
                result_version=version,
            )
        return GovernanceCommandResult(program_id, version, False)

    def read_program(
        self,
        *,
        principal: Principal,
        program_id: str,
    ) -> GovernanceBoard:
        board = self.repository.load(
            tenant_id=principal.tenant_id,
            program_id=program_id,
            audit=InMemoryAuditSink(),
        )
        if board is None:
            raise ResourceNotFound("governance program is absent or hidden")
        self._require_actor(board, principal)
        return board

    def add_member(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        member: BoardMember,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="member.add",
            payload={
                "principal_id": member.principal_id,
                "tenant_id": member.tenant_id,
                "role": member.role.value,
            },
            operation=lambda board: board.add_member(
                member,
                actor_id=principal.principal_id,
            ),
        )

    def create_plan(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        plan_id: str,
        version: int,
        title: str,
        objective: str,
        deliverables: tuple[str, ...],
        required_approvers: frozenset[str],
        visible_to_tenants: frozenset[str],
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="plan.create",
            payload={
                "plan_id": plan_id,
                "version": version,
                "title": title,
                "objective": objective,
                "deliverables": list(deliverables),
                "required_approvers": sorted(required_approvers),
                "visible_to_tenants": sorted(visible_to_tenants),
            },
            operation=lambda board: board.create_plan(
                actor_id=principal.principal_id,
                plan_id=plan_id,
                version=version,
                title=title,
                objective=objective,
                deliverables=deliverables,
                required_approvers=required_approvers,
                visible_to_tenants=visible_to_tenants,
            ),
        )

    def open_discussion(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        plan_id: str,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="plan.open_discussion",
            payload={"plan_id": plan_id},
            operation=lambda board: board.open_discussion(
                actor_id=principal.principal_id,
                plan_id=plan_id,
            ),
        )

    def add_discussion_item(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        plan_id: str,
        item_id: str,
        kind: DiscussionKind,
        content: str,
        blocking: bool,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="plan.discussion_item.add",
            payload={
                "plan_id": plan_id,
                "item_id": item_id,
                "kind": kind.value,
                "content": content,
                "blocking": blocking,
            },
            operation=lambda board: board.add_discussion_item(
                actor_id=principal.principal_id,
                plan_id=plan_id,
                item_id=item_id,
                kind=kind,
                content=content,
                blocking=blocking,
            ),
        )

    def resolve_discussion_item(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        plan_id: str,
        item_id: str,
        resolution: str,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="plan.discussion_item.resolve",
            payload={
                "plan_id": plan_id,
                "item_id": item_id,
                "resolution": resolution,
            },
            operation=lambda board: board.resolve_discussion_item(
                actor_id=principal.principal_id,
                plan_id=plan_id,
                item_id=item_id,
                resolution=resolution,
            ),
        )

    def approve_plan(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        plan_id: str,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="plan.approve",
            payload={"plan_id": plan_id},
            operation=lambda board: board.approve_plan(
                actor_id=principal.principal_id,
                plan_id=plan_id,
            ),
        )

    def propose_assignment(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        assignment_id: str,
        plan_id: str,
        assignee_id: str,
        title: str,
        description: str,
        deliverable_contract: str,
        dependencies: tuple[str, ...],
        visible_to_tenants: frozenset[str],
    ) -> GovernanceCommandResult:
        if not self.allow_legacy_assignment_writes:
            raise GovernanceConflictError(
                "legacy assignment writes are disabled; use project task contracts"
            )
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="assignment.propose",
            payload={
                "assignment_id": assignment_id,
                "plan_id": plan_id,
                "assignee_id": assignee_id,
                "title": title,
                "description": description,
                "deliverable_contract": deliverable_contract,
                "dependencies": list(dependencies),
                "visible_to_tenants": sorted(visible_to_tenants),
            },
            operation=lambda board: board.propose_assignment(
                actor_id=principal.principal_id,
                assignment_id=assignment_id,
                plan_id=plan_id,
                assignee_id=assignee_id,
                title=title,
                description=description,
                deliverable_contract=deliverable_contract,
                dependencies=dependencies,
                visible_to_tenants=visible_to_tenants,
            ),
        )

    def respond_to_assignment(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        assignment_id: str,
        accept: bool,
        reason: str,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="assignment.respond",
            payload={
                "assignment_id": assignment_id,
                "accept": accept,
                "reason": reason,
            },
            operation=lambda board: board.respond_to_assignment(
                actor_id=principal.principal_id,
                assignment_id=assignment_id,
                accept=accept,
                reason=reason,
            ),
        )

    def start_assignment(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        assignment_id: str,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="assignment.start",
            payload={"assignment_id": assignment_id},
            operation=lambda board: board.start_assignment(
                actor_id=principal.principal_id,
                assignment_id=assignment_id,
            ),
        )

    def submit_assignment(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        assignment_id: str,
        artifact_refs: tuple[str, ...],
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="assignment.submit",
            payload={
                "assignment_id": assignment_id,
                "artifact_refs": list(artifact_refs),
            },
            operation=lambda board: board.submit_assignment(
                actor_id=principal.principal_id,
                assignment_id=assignment_id,
                artifact_refs=artifact_refs,
            ),
        )

    def review_assignment(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        assignment_id: str,
        accept: bool,
        note: str,
    ) -> GovernanceCommandResult:
        return self._execute(
            principal=principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=expected_version,
            command_type="assignment.review",
            payload={
                "assignment_id": assignment_id,
                "accept": accept,
                "note": note,
            },
            operation=lambda board: board.review_assignment(
                actor_id=principal.principal_id,
                assignment_id=assignment_id,
                accept=accept,
                note=note,
            ),
        )

    def _execute(
        self,
        *,
        principal: Principal,
        idempotency_key: str,
        program_id: str,
        expected_version: int,
        command_type: str,
        payload: dict,
        operation: Callable[[GovernanceBoard], object],
    ) -> GovernanceCommandResult:
        digest = _command_digest(
            command_type,
            {
                "program_id": program_id,
                "expected_version": expected_version,
                **payload,
            },
        )
        with self.repository.engine.begin() as connection:
            claim = self.repository.claim_command_in_transaction(
                connection,
                tenant_id=principal.tenant_id,
                idempotency_key=idempotency_key,
                program_id=program_id,
                command_type=command_type,
                request_digest=digest,
            )
            duplicate = self._duplicate_result(claim, program_id)
            if duplicate is not None:
                return duplicate
            sink = InMemoryAuditSink()
            board = self.repository.load(
                tenant_id=principal.tenant_id,
                program_id=program_id,
                audit=sink,
                connection=connection,
            )
            if board is None:
                raise ResourceNotFound("governance program is absent or hidden")
            self._require_actor(board, principal)
            if command_type == "assignment.submit" and self.artifact_guard is not None:
                self.artifact_guard.assert_assignment_artifacts(connection,
                    principal=principal, board=board,
                    assignment_id=str(payload["assignment_id"]),
                    artifact_refs=tuple(payload["artifact_refs"]))
            if (
                command_type == "assignment.review"
                and payload.get("accept") is True
                and self.impact_guard is not None
            ):
                self.impact_guard.assert_assignment_verifiable(
                    connection,
                    program_id=program_id,
                    assignment_id=str(payload["assignment_id"]),
                )
            if board.aggregate_version != expected_version:
                raise GovernanceConflictError("governance aggregate version is stale")
            operation(board)
            version = self.repository.save_in_transaction(
                connection,
                board=board,
                expected_version=expected_version,
                actor_id=principal.principal_id,
                events=tuple(sink.events),
            )
            self.repository.complete_command_in_transaction(
                connection,
                tenant_id=principal.tenant_id,
                idempotency_key=idempotency_key,
                result_version=version,
            )
        return GovernanceCommandResult(program_id, version, False)

    @staticmethod
    def _duplicate_result(claim, program_id: str) -> GovernanceCommandResult | None:
        if claim.status is ClaimStatus.CONFLICT:
            raise IdempotencyConflict(
                "governance idempotency key was reused with different content"
            )
        if claim.status is ClaimStatus.DUPLICATE:
            assert claim.result_version is not None
            return GovernanceCommandResult(
                program_id=program_id,
                aggregate_version=claim.result_version,
                duplicate=True,
            )
        return None

    @staticmethod
    def _require_actor(board: GovernanceBoard, principal: Principal) -> None:
        member = board.members.get(principal.principal_id)
        if member is None or member.tenant_id != principal.tenant_id:
            raise ResourceNotFound("governance program is absent or hidden")
        if principal.clearance < board.classification or not board.compartments.issubset(
            principal.compartments
        ):
            raise ResourceNotFound("governance program is absent or hidden")


def _command_digest(command_type: str, payload: dict) -> str:
    canonical = json.dumps(
        {
            "command_type": command_type,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
