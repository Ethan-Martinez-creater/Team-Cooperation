from __future__ import annotations

from pydantic import Field, field_validator

from ..collaboration import CollaborationRole, DiscussionKind
from .models import ClassificationName, StrictModel

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class ProgramCreateBody(StrictModel):
    program_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=20_000)
    classification: ClassificationName
    compartments: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("compartments")
    @classmethod
    def validate_compartments(cls, value: list[str]) -> list[str]:
        return _unique_identifiers(value, "compartments")


class MemberAddBody(StrictModel):
    principal_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    tenant_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    role: CollaborationRole


class PlanCreateBody(StrictModel):
    plan_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    version: int = Field(ge=1, le=2_147_483_647)
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=20_000)
    deliverables: list[str] = Field(min_length=1, max_length=256)
    required_approvers: list[str] = Field(min_length=1, max_length=256)
    visible_to_tenants: list[str] = Field(min_length=1, max_length=256)

    @field_validator("deliverables")
    @classmethod
    def validate_deliverables(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 10_000 for item in value):
            raise ValueError("a deliverable is invalid")
        if len(value) != len(set(value)):
            raise ValueError("deliverables must be unique")
        return value

    @field_validator("required_approvers", "visible_to_tenants")
    @classmethod
    def validate_identifier_lists(cls, value: list[str], info) -> list[str]:
        return _unique_identifiers(value, info.field_name)


class DiscussionItemCreateBody(StrictModel):
    item_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    kind: DiscussionKind
    content: str = Field(min_length=1, max_length=20_000)
    blocking: bool = False


class DiscussionResolutionBody(StrictModel):
    resolution: str = Field(min_length=1, max_length=20_000)


class AssignmentCreateBody(StrictModel):
    assignment_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    plan_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    assignee_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=20_000)
    deliverable_contract: str = Field(min_length=1, max_length=20_000)
    dependencies: list[str] = Field(default_factory=list, max_length=256)
    visible_to_tenants: list[str] = Field(min_length=1, max_length=256)

    @field_validator("dependencies", "visible_to_tenants")
    @classmethod
    def validate_identifier_lists(cls, value: list[str], info) -> list[str]:
        return _unique_identifiers(value, info.field_name)


class AssignmentResponseBody(StrictModel):
    accept: bool
    reason: str = Field(default="", max_length=2_000)


class AssignmentSubmissionBody(StrictModel):
    artifact_refs: list[str] = Field(min_length=1, max_length=256)

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifacts(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 1_024 for item in value):
            raise ValueError("an artifact reference is invalid")
        if len(value) != len(set(value)):
            raise ValueError("artifact references must be unique")
        return value


class AssignmentReviewBody(StrictModel):
    accept: bool
    note: str = Field(min_length=1, max_length=10_000)


class GovernanceCommandResponse(StrictModel):
    program_id: str
    aggregate_version: int
    duplicate: bool


class MemberView(StrictModel):
    principal_id: str
    tenant_id: str
    role: CollaborationRole


class DiscussionItemView(StrictModel):
    item_id: str
    author_id: str
    kind: DiscussionKind
    content: str
    blocking: bool
    resolved: bool
    resolved_by: str | None
    resolution: str | None


class PlanView(StrictModel):
    plan_id: str
    version: int
    title: str
    objective: str
    deliverables: list[str]
    lead_id: str
    required_approvers: list[str]
    approvals: list[str]
    visible_to_tenants: list[str]
    content_digest: str
    state: str
    discussion_items: list[DiscussionItemView]


class AssignmentView(StrictModel):
    assignment_id: str
    plan_id: str
    plan_digest: str
    title: str
    description: str
    deliverable_contract: str
    proposed_by: str
    assignee_id: str
    dependencies: list[str]
    visible_to_tenants: list[str]
    state: str
    response_reason: str | None
    artifact_refs: list[str]
    verification_note: str | None


class ProgramView(StrictModel):
    program_id: str
    owner_tenant_id: str
    title: str
    objective: str
    classification: ClassificationName
    compartments: list[str]
    aggregate_version: int
    members: list[MemberView]
    plans: list[PlanView]
    assignments: list[AssignmentView]


class ProgramSummaryView(StrictModel):
    program_id: str
    owner_tenant_id: str
    title: str
    classification: ClassificationName
    aggregate_version: int
    role: CollaborationRole
    updated_at: str


class AssignmentSummaryView(StrictModel):
    program_id: str
    program_title: str
    aggregate_version: int
    assignment_id: str
    plan_id: str
    title: str
    description: str
    deliverable_contract: str
    proposed_by: str
    assignee_id: str
    state: str
    updated_at: str


def _unique_identifiers(value: list[str], field_name: str) -> list[str]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
    if any(
        not item
        or len(item) > 128
        or not item[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in item
        )
        for item in value
    ):
        raise ValueError(f"{field_name} contains an invalid identifier")
    return value
