from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet


class CollaborationRole(str, Enum):
    LEAD = "lead"
    CONTRIBUTOR = "contributor"
    REVIEWER = "reviewer"
    OBSERVER = "observer"


class PlanState(str, Enum):
    DRAFT = "draft"
    DISCUSSION = "discussion"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class DiscussionKind(str, Enum):
    COMMENT = "comment"
    PROPOSAL = "proposal"
    RISK = "risk"
    OBJECTION = "objection"


class AssignmentState(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class BoardMember:
    principal_id: str
    tenant_id: str
    role: CollaborationRole


@dataclass(slots=True)
class DiscussionItem:
    item_id: str
    plan_id: str
    author_id: str
    kind: DiscussionKind
    content: str
    blocking: bool
    resolved: bool = False
    resolved_by: str | None = None
    resolution: str | None = None


@dataclass(slots=True)
class PlanRecord:
    plan_id: str
    program_id: str
    version: int
    title: str
    objective: str
    deliverables: tuple[str, ...]
    lead_id: str
    required_approvers: FrozenSet[str]
    visible_to_tenants: FrozenSet[str]
    content_digest: str
    state: PlanState = PlanState.DRAFT
    approvals: dict[str, str] = field(default_factory=dict)
    discussion_items: list[DiscussionItem] = field(default_factory=list)


@dataclass(slots=True)
class TaskAssignment:
    assignment_id: str
    plan_id: str
    plan_digest: str
    title: str
    description: str
    deliverable_contract: str
    proposed_by: str
    assignee_id: str
    dependencies: tuple[str, ...]
    visible_to_tenants: FrozenSet[str]
    state: AssignmentState = AssignmentState.PROPOSED
    response_reason: str | None = None
    artifact_refs: tuple[str, ...] = ()
    verification_note: str | None = None
