import pytest

from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.collaboration import (
    AssignmentState,
    CollaborationRole,
    DiscussionKind,
    GovernanceBoard,
    PlanState,
)
from coifesp_harness.collaboration.governance_models import BoardMember
from coifesp_harness.errors import GovernanceError
from coifesp_harness.security import Classification


def board() -> GovernanceBoard:
    value = GovernanceBoard(
        program_id="program-1",
        owner_tenant_id="team-a",
        title="Cross-team integration",
        objective="Deliver a verified integration",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
        audit=InMemoryAuditSink(),
    )
    value.add_member(BoardMember("lead-a", "team-a", CollaborationRole.LEAD))
    value.add_member(
        BoardMember("contributor-b", "team-b", CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    value.add_member(
        BoardMember("reviewer-a", "team-a", CollaborationRole.REVIEWER),
        actor_id="lead-a",
    )
    return value


def test_contributor_participates_and_blocking_objection_prevents_approval() -> None:
    value = board()
    plan = value.create_plan(
        actor_id="lead-a",
        plan_id="plan-1",
        version=1,
        title="Integration",
        objective="Integrate both team deliverables",
        deliverables=("API contract", "client implementation"),
        required_approvers=frozenset({"lead-a", "contributor-b"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    value.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    value.add_discussion_item(
        actor_id="contributor-b",
        plan_id=plan.plan_id,
        item_id="item-1",
        kind=DiscussionKind.OBJECTION,
        content="The API contract lacks an error model.",
        blocking=True,
    )
    with pytest.raises(GovernanceError, match="unresolved blocking"):
        value.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)

    with pytest.raises(GovernanceError, match="author or a reviewer"):
        value.resolve_discussion_item(
            actor_id="lead-a",
            plan_id=plan.plan_id,
            item_id="item-1",
            resolution="Unilaterally ignored",
        )

    value.resolve_discussion_item(
        actor_id="contributor-b",
        plan_id=plan.plan_id,
        item_id="item-1",
        resolution="Error schema added to deliverable contract.",
    )
    assert not value.approve_plan(actor_id="contributor-b", plan_id=plan.plan_id)
    assert value.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    assert plan.state is PlanState.APPROVED


def test_lead_proposes_but_contributor_controls_acceptance_and_delivery() -> None:
    value = board()
    plan = value.create_plan(
        actor_id="lead-a",
        plan_id="plan-1",
        version=1,
        title="Integration",
        objective="Integrate deliverables",
        deliverables=("client",),
        required_approvers=frozenset({"lead-a"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    value.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    value.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    assignment = value.propose_assignment(
        actor_id="lead-a",
        assignment_id="task-1",
        plan_id=plan.plan_id,
        assignee_id="contributor-b",
        title="Implement client",
        description="Implement against the frozen contract",
        deliverable_contract="Signed commit plus contract tests",
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    assert assignment.state is AssignmentState.PROPOSED

    with pytest.raises(GovernanceError, match="only the assignee"):
        value.respond_to_assignment(
            actor_id="lead-a", assignment_id=assignment.assignment_id, accept=True
        )
    value.respond_to_assignment(
        actor_id="contributor-b", assignment_id=assignment.assignment_id, accept=True
    )
    value.start_assignment(actor_id="contributor-b", assignment_id=assignment.assignment_id)
    value.submit_assignment(
        actor_id="contributor-b",
        assignment_id=assignment.assignment_id,
        artifact_refs=("git:commit:abc123", "test-report:42"),
    )
    value.review_assignment(
        actor_id="reviewer-a",
        assignment_id=assignment.assignment_id,
        accept=True,
        note="Contract tests passed.",
    )
    assert assignment.state is AssignmentState.VERIFIED


def test_hidden_plan_and_assignment_are_not_operable_by_undisclosed_tenant() -> None:
    value = board()
    value.add_member(
        BoardMember("contributor-c", "team-c", CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    plan = value.create_plan(
        actor_id="lead-a",
        plan_id="private-plan",
        version=1,
        title="Restricted integration",
        objective="Share only with the implementing team",
        deliverables=("restricted contract",),
        required_approvers=frozenset({"lead-a", "contributor-b"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    value.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)

    with pytest.raises(GovernanceError, match="not disclosed"):
        value.add_discussion_item(
            actor_id="contributor-c",
            plan_id=plan.plan_id,
            item_id="hidden-comment",
            kind=DiscussionKind.COMMENT,
            content="I should not see this plan.",
        )
