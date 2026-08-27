from __future__ import annotations

import hashlib
import json

from ..audit import AuditEvent, AuditSink
from ..errors import GovernanceError
from ..security import Classification
from .governance_models import (
    AssignmentState,
    BoardMember,
    CollaborationRole,
    DiscussionItem,
    DiscussionKind,
    PlanRecord,
    PlanState,
    TaskAssignment,
)


def _plan_digest(
    *,
    program_id: str,
    version: int,
    title: str,
    objective: str,
    deliverables: tuple[str, ...],
) -> str:
    value = json.dumps(
        {
            "program_id": program_id,
            "version": version,
            "title": title,
            "objective": objective,
            "deliverables": deliverables,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GovernanceBoard:
    """Role-aware collaboration aggregate.

    This domain object protects decision semantics. Persistence adapters may
    reconstruct it from versioned events without changing the state machine.
    """

    def __init__(
        self,
        *,
        program_id: str,
        owner_tenant_id: str,
        title: str,
        objective: str,
        classification: Classification,
        compartments: frozenset[str],
        audit: AuditSink,
        aggregate_version: int = 0,
    ) -> None:
        if not program_id or not owner_tenant_id:
            raise ValueError("program_id and owner_tenant_id are required")
        if not title or not objective:
            raise ValueError("program title and objective are required")
        if aggregate_version < 0:
            raise ValueError("aggregate_version cannot be negative")
        self.program_id = program_id
        self.owner_tenant_id = owner_tenant_id
        self.title = title
        self.objective = objective
        self.classification = classification
        self.compartments = compartments
        self.aggregate_version = aggregate_version
        self.audit = audit
        self.members: dict[str, BoardMember] = {}
        self.plans: dict[str, PlanRecord] = {}
        self.assignments: dict[str, TaskAssignment] = {}

    @property
    def participant_tenant_ids(self) -> frozenset[str]:
        return frozenset(
            {self.owner_tenant_id, *(member.tenant_id for member in self.members.values())}
        )

    def add_member(
        self,
        member: BoardMember,
        *,
        actor_id: str | None = None,
    ) -> None:
        if member.principal_id in self.members:
            raise GovernanceError("member already exists")
        if not self.members:
            if (
                member.role is not CollaborationRole.LEAD
                or member.tenant_id != self.owner_tenant_id
                or actor_id not in {None, member.principal_id}
            ):
                raise GovernanceError("the initial member must be the owner-tenant lead")
            actor_id = member.principal_id
        else:
            if actor_id is None:
                raise GovernanceError("adding a member requires an acting lead")
            self._require_role(actor_id, CollaborationRole.LEAD)
        self.members[member.principal_id] = member
        actor = self._member(actor_id)
        self._audit(
            actor_id,
            actor.tenant_id,
            "member.added",
            member.principal_id,
            member_tenant_id=member.tenant_id,
            member_role=member.role.value,
        )

    def create_plan(
        self,
        *,
        actor_id: str,
        plan_id: str,
        version: int,
        title: str,
        objective: str,
        deliverables: tuple[str, ...],
        required_approvers: frozenset[str],
        visible_to_tenants: frozenset[str],
    ) -> PlanRecord:
        actor = self._require_role(actor_id, CollaborationRole.LEAD)
        if plan_id in self.plans:
            raise GovernanceError("plan already exists")
        if actor_id not in required_approvers:
            raise GovernanceError("lead must be a required plan approver")
        unknown = required_approvers.difference(self.members)
        if unknown:
            raise GovernanceError(f"unknown required approvers: {sorted(unknown)}")
        if not title or not objective or not deliverables:
            raise GovernanceError("plan requires title, objective, and deliverables")
        self._validate_visibility(
            visible_to_tenants,
            required_member_ids=required_approvers | {actor_id},
        )
        digest = _plan_digest(
            program_id=self.program_id,
            version=version,
            title=title,
            objective=objective,
            deliverables=deliverables,
        )
        plan = PlanRecord(
            plan_id=plan_id,
            program_id=self.program_id,
            version=version,
            title=title,
            objective=objective,
            deliverables=deliverables,
            lead_id=actor_id,
            required_approvers=required_approvers,
            visible_to_tenants=visible_to_tenants,
            content_digest=digest,
        )
        self.plans[plan_id] = plan
        self._audit(actor_id, actor.tenant_id, "plan.created", plan_id, digest=digest)
        return plan

    def open_discussion(self, *, actor_id: str, plan_id: str) -> None:
        actor = self._require_role(actor_id, CollaborationRole.LEAD)
        plan = self._plan(plan_id)
        self._require_plan_visibility(actor, plan)
        self._transition_plan(plan, PlanState.DRAFT, PlanState.DISCUSSION)
        self._audit(actor_id, actor.tenant_id, "plan.discussion_opened", plan_id)

    def add_discussion_item(
        self,
        *,
        actor_id: str,
        plan_id: str,
        item_id: str,
        kind: DiscussionKind,
        content: str,
        blocking: bool = False,
    ) -> DiscussionItem:
        actor = self._require_participant(actor_id)
        plan = self._plan(plan_id)
        self._require_plan_visibility(actor, plan)
        if plan.state is not PlanState.DISCUSSION:
            raise GovernanceError("discussion items require a plan in discussion")
        if any(item.item_id == item_id for item in plan.discussion_items):
            raise GovernanceError("discussion item already exists")
        if not content:
            raise GovernanceError("discussion content is required")
        item = DiscussionItem(
            item_id=item_id,
            plan_id=plan_id,
            author_id=actor_id,
            kind=kind,
            content=content,
            blocking=blocking,
        )
        plan.discussion_items.append(item)
        self._audit(
            actor_id,
            actor.tenant_id,
            "plan.discussion_item_added",
            item_id,
            kind=kind.value,
            blocking=blocking,
        )
        return item

    def resolve_discussion_item(
        self,
        *,
        actor_id: str,
        plan_id: str,
        item_id: str,
        resolution: str,
    ) -> None:
        actor = self._require_participant(actor_id)
        plan = self._plan(plan_id)
        self._require_plan_visibility(actor, plan)
        item = next(
            (candidate for candidate in plan.discussion_items if candidate.item_id == item_id),
            None,
        )
        if item is None:
            raise GovernanceError("discussion item not found")
        if actor_id != item.author_id and actor.role is not CollaborationRole.REVIEWER:
            raise GovernanceError("only the item author or a reviewer may resolve it")
        if not resolution:
            raise GovernanceError("resolution is required")
        item.resolved = True
        item.resolved_by = actor_id
        item.resolution = resolution
        self._audit(
            actor_id,
            actor.tenant_id,
            "plan.discussion_item_resolved",
            item_id,
        )

    def approve_plan(self, *, actor_id: str, plan_id: str) -> bool:
        actor = self._member(actor_id)
        plan = self._plan(plan_id)
        self._require_plan_visibility(actor, plan)
        if plan.state is not PlanState.DISCUSSION:
            raise GovernanceError("only a plan in discussion can be approved")
        if actor_id not in plan.required_approvers:
            raise GovernanceError("actor is not a required approver")
        blocking = [
            item.item_id for item in plan.discussion_items if item.blocking and not item.resolved
        ]
        if blocking:
            raise GovernanceError(f"unresolved blocking discussion items: {blocking}")
        plan.approvals[actor_id] = plan.content_digest
        complete = plan.required_approvers.issubset(plan.approvals)
        if complete:
            plan.state = PlanState.APPROVED
        self._audit(
            actor_id,
            actor.tenant_id,
            "plan.approved" if complete else "plan.approval_recorded",
            plan_id,
            digest=plan.content_digest,
        )
        return complete

    def propose_assignment(
        self,
        *,
        actor_id: str,
        assignment_id: str,
        plan_id: str,
        assignee_id: str,
        title: str,
        description: str,
        deliverable_contract: str,
        dependencies: tuple[str, ...] = (),
        visible_to_tenants: frozenset[str],
    ) -> TaskAssignment:
        actor = self._require_role(actor_id, CollaborationRole.LEAD)
        plan = self._plan(plan_id)
        if plan.state is not PlanState.APPROVED:
            raise GovernanceError("tasks may only be proposed from an approved plan")
        assignee = self._member(assignee_id)
        if assignee.role not in {CollaborationRole.CONTRIBUTOR, CollaborationRole.LEAD}:
            raise GovernanceError("assignee must be a lead or contributor")
        if assignment_id in self.assignments:
            raise GovernanceError("assignment already exists")
        missing = [item for item in dependencies if item not in self.assignments]
        if missing:
            raise GovernanceError(f"unknown assignment dependencies: {missing}")
        if not title or not deliverable_contract:
            raise GovernanceError("assignment title and deliverable contract are required")
        self._validate_visibility(
            visible_to_tenants,
            required_member_ids={actor_id, assignee_id},
        )
        if not visible_to_tenants.issubset(plan.visible_to_tenants):
            raise GovernanceError("assignment visibility cannot exceed plan visibility")
        for dependency_id in dependencies:
            dependency = self.assignments[dependency_id]
            if assignee.tenant_id not in dependency.visible_to_tenants:
                raise GovernanceError("assignee cannot see an assignment dependency")
        assignment = TaskAssignment(
            assignment_id=assignment_id,
            plan_id=plan_id,
            plan_digest=plan.content_digest,
            title=title,
            description=description,
            deliverable_contract=deliverable_contract,
            proposed_by=actor_id,
            assignee_id=assignee_id,
            dependencies=dependencies,
            visible_to_tenants=visible_to_tenants,
        )
        self.assignments[assignment_id] = assignment
        self._audit(
            actor_id,
            actor.tenant_id,
            "assignment.proposed",
            assignment_id,
            assignee_id=assignee_id,
        )
        return assignment

    def respond_to_assignment(
        self,
        *,
        actor_id: str,
        assignment_id: str,
        accept: bool,
        reason: str = "",
    ) -> None:
        actor = self._member(actor_id)
        assignment = self._assignment(assignment_id)
        self._require_assignment_visibility(actor, assignment)
        if actor_id != assignment.assignee_id:
            raise GovernanceError("only the assignee may respond")
        if assignment.state is not AssignmentState.PROPOSED:
            raise GovernanceError("assignment is no longer awaiting response")
        if not accept and not reason:
            raise GovernanceError("declining an assignment requires a reason")
        assignment.state = AssignmentState.ACCEPTED if accept else AssignmentState.DECLINED
        assignment.response_reason = reason or None
        self._audit(
            actor_id,
            actor.tenant_id,
            "assignment.accepted" if accept else "assignment.declined",
            assignment_id,
            reason=reason,
        )

    def start_assignment(self, *, actor_id: str, assignment_id: str) -> None:
        actor = self._member(actor_id)
        assignment = self._assignment(assignment_id)
        self._require_assignment_visibility(actor, assignment)
        if actor_id != assignment.assignee_id:
            raise GovernanceError("only the assignee may start the assignment")
        if assignment.state is not AssignmentState.ACCEPTED:
            raise GovernanceError("assignment must be accepted before it starts")
        incomplete = [
            dependency
            for dependency in assignment.dependencies
            if self.assignments[dependency].state is not AssignmentState.VERIFIED
        ]
        if incomplete:
            raise GovernanceError(f"assignment dependencies are not verified: {incomplete}")
        assignment.state = AssignmentState.IN_PROGRESS
        self._audit(actor_id, actor.tenant_id, "assignment.started", assignment_id)

    def submit_assignment(
        self,
        *,
        actor_id: str,
        assignment_id: str,
        artifact_refs: tuple[str, ...],
    ) -> None:
        actor = self._member(actor_id)
        assignment = self._assignment(assignment_id)
        self._require_assignment_visibility(actor, assignment)
        if actor_id != assignment.assignee_id:
            raise GovernanceError("only the assignee may submit")
        if assignment.state is not AssignmentState.IN_PROGRESS:
            raise GovernanceError("only an in-progress assignment may be submitted")
        if not artifact_refs:
            raise GovernanceError("at least one artifact reference is required")
        assignment.artifact_refs = artifact_refs
        assignment.state = AssignmentState.SUBMITTED
        self._audit(actor_id, actor.tenant_id, "assignment.submitted", assignment_id)

    def review_assignment(
        self,
        *,
        actor_id: str,
        assignment_id: str,
        accept: bool,
        note: str,
    ) -> None:
        actor = self._member(actor_id)
        if actor.role not in {CollaborationRole.LEAD, CollaborationRole.REVIEWER}:
            raise GovernanceError("only a lead or reviewer may review a submission")
        assignment = self._assignment(assignment_id)
        self._require_assignment_visibility(actor, assignment)
        if assignment.state is not AssignmentState.SUBMITTED:
            raise GovernanceError("assignment is not awaiting review")
        if not note:
            raise GovernanceError("review note is required")
        assignment.verification_note = note
        assignment.state = AssignmentState.VERIFIED if accept else AssignmentState.IN_PROGRESS
        self._audit(
            actor_id,
            actor.tenant_id,
            "assignment.verified" if accept else "assignment.changes_requested",
            assignment_id,
            note=note,
        )

    def _transition_plan(self, plan: PlanRecord, expected: PlanState, target: PlanState) -> None:
        if plan.state is not expected:
            raise GovernanceError(f"plan must be {expected.value} before {target.value}")
        plan.state = target

    def _member(self, principal_id: str) -> BoardMember:
        member = self.members.get(principal_id)
        if member is None:
            raise GovernanceError("actor is not a governance board member")
        return member

    def _require_role(self, principal_id: str, role: CollaborationRole) -> BoardMember:
        member = self._member(principal_id)
        if member.role is not role:
            raise GovernanceError(f"operation requires collaboration role: {role.value}")
        return member

    def _require_participant(self, principal_id: str) -> BoardMember:
        member = self._member(principal_id)
        if member.role is CollaborationRole.OBSERVER:
            raise GovernanceError("observers cannot change discussion state")
        return member

    def _validate_visibility(
        self,
        visible_to_tenants: frozenset[str],
        *,
        required_member_ids: set[str] | frozenset[str],
    ) -> None:
        if not visible_to_tenants:
            raise GovernanceError("explicit tenant visibility is required")
        unknown_tenants = visible_to_tenants.difference(self.participant_tenant_ids)
        if unknown_tenants:
            raise GovernanceError(f"unknown visible tenants: {sorted(unknown_tenants)}")
        required_tenants = {
            self._member(principal_id).tenant_id for principal_id in required_member_ids
        }
        missing = required_tenants.difference(visible_to_tenants)
        if missing:
            raise GovernanceError(
                f"required participant tenants are not visible: {sorted(missing)}"
            )
        if self.owner_tenant_id not in visible_to_tenants:
            raise GovernanceError("owner tenant must retain governance visibility")

    @staticmethod
    def _require_plan_visibility(actor: BoardMember, plan: PlanRecord) -> None:
        if actor.tenant_id not in plan.visible_to_tenants:
            raise GovernanceError("plan is not disclosed to the actor tenant")

    @staticmethod
    def _require_assignment_visibility(
        actor: BoardMember,
        assignment: TaskAssignment,
    ) -> None:
        if actor.tenant_id not in assignment.visible_to_tenants:
            raise GovernanceError("assignment is not disclosed to the actor tenant")

    def _plan(self, plan_id: str) -> PlanRecord:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise GovernanceError("plan not found")
        return plan

    def _assignment(self, assignment_id: str) -> TaskAssignment:
        assignment = self.assignments.get(assignment_id)
        if assignment is None:
            raise GovernanceError("assignment not found")
        return assignment

    def _audit(
        self,
        actor_id: str,
        tenant_id: str,
        event_type: str,
        subject_id: str,
        **details: object,
    ) -> None:
        self.audit.append(
            AuditEvent(
                tenant_id=tenant_id,
                event_type=f"governance.{event_type}",
                actor_id=actor_id,
                outcome="recorded",
                details={
                    "program_id": self.program_id,
                    "subject_id": subject_id,
                    **details,
                },
                correlation_id=self.program_id,
            )
        )
