import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.collaboration import (
    CollaborationRole,
    DiscussionKind,
    GovernanceService,
    SQLAlchemyGovernanceRepository,
)
from coifesp_harness.collaboration.governance_models import BoardMember
from coifesp_harness.errors import (
    GovernanceConflictError,
    GovernanceError,
    IdempotencyConflict,
    ResourceNotFound,
)
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal


def principal(
    principal_id,
    tenant_id,
    *,
    roles=frozenset(),
):
    return Principal(
        principal_id=principal_id,
        tenant_id=tenant_id,
        roles=roles,
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
    )


def service(*, allow_legacy_assignment_writes=True):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1",
            verification_keys={"audit-v1": b"a" * 32},
        ),
    )
    audit.create_schema()
    repository = SQLAlchemyGovernanceRepository(engine=engine, audit_log=audit)
    repository.create_schema()
    return GovernanceService(
        repository,
        allow_legacy_assignment_writes=allow_legacy_assignment_writes,
    ), audit


def test_legacy_assignment_writes_can_be_disabled_for_modern_runtime():
    governance, _audit = service(allow_legacy_assignment_writes=False)
    with pytest.raises(
        GovernanceConflictError,
        match="legacy assignment writes are disabled",
    ):
        governance.propose_assignment(
            principal=principal("lead-a", "team-a", roles=frozenset({"lead"})),
            idempotency_key="legacy-disabled",
            program_id="program-1",
            expected_version=0,
            assignment_id="assignment-1",
            plan_id="plan-1",
            assignee_id="worker-b",
            title="Legacy task",
            description="Should not be created",
            deliverable_contract="none",
            dependencies=(),
            visible_to_tenants=frozenset({"team-a"}),
        )


    governance, audit = service()
    lead = principal(
        "lead-a",
        "team-a",
        roles=frozenset({"collaboration_creator"}),
    )
    contributor = principal("contributor-b", "team-b")

    created = governance.create_program(
        principal=lead,
        idempotency_key="create-program",
        program_id="program-1",
        title="Cross-team release",
        objective="Deliver a jointly approved integration",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
    )
    duplicate = governance.create_program(
        principal=lead,
        idempotency_key="create-program",
        program_id="program-1",
        title="Cross-team release",
        objective="Deliver a jointly approved integration",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
    )
    assert created.aggregate_version == 1
    assert duplicate.duplicate is True

    added = governance.add_member(
        principal=lead,
        idempotency_key="add-contributor",
        program_id="program-1",
        expected_version=1,
        member=BoardMember(
            "contributor-b",
            "team-b",
            CollaborationRole.CONTRIBUTOR,
        ),
    )
    plan_created = governance.create_plan(
        principal=lead,
        idempotency_key="create-plan",
        program_id="program-1",
        expected_version=added.aggregate_version,
        plan_id="plan-1",
        version=1,
        title="Integration contract",
        objective="Freeze a mutually reviewed boundary",
        deliverables=("OpenAPI contract", "contract tests"),
        required_approvers=frozenset({"lead-a", "contributor-b"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    opened = governance.open_discussion(
        principal=lead,
        idempotency_key="open-plan",
        program_id="program-1",
        expected_version=plan_created.aggregate_version,
        plan_id="plan-1",
    )
    objected = governance.add_discussion_item(
        principal=contributor,
        idempotency_key="add-objection",
        program_id="program-1",
        expected_version=opened.aggregate_version,
        plan_id="plan-1",
        item_id="objection-1",
        kind=DiscussionKind.OBJECTION,
        content="The error model is incomplete.",
        blocking=True,
    )
    with pytest.raises(GovernanceError, match="unresolved blocking"):
        governance.approve_plan(
            principal=lead,
            idempotency_key="premature-approval",
            program_id="program-1",
            expected_version=objected.aggregate_version,
            plan_id="plan-1",
        )
    resolved = governance.resolve_discussion_item(
        principal=contributor,
        idempotency_key="resolve-objection",
        program_id="program-1",
        expected_version=objected.aggregate_version,
        plan_id="plan-1",
        item_id="objection-1",
        resolution="Versioned error schema added.",
    )
    contributor_approval = governance.approve_plan(
        principal=contributor,
        idempotency_key="approve-contributor",
        program_id="program-1",
        expected_version=resolved.aggregate_version,
        plan_id="plan-1",
    )
    lead_approval = governance.approve_plan(
        principal=lead,
        idempotency_key="approve-lead",
        program_id="program-1",
        expected_version=contributor_approval.aggregate_version,
        plan_id="plan-1",
    )
    proposed = governance.propose_assignment(
        principal=lead,
        idempotency_key="propose-assignment",
        program_id="program-1",
        expected_version=lead_approval.aggregate_version,
        assignment_id="task-1",
        plan_id="plan-1",
        assignee_id="contributor-b",
        title="Implement client",
        description="Implement against the approved contract",
        deliverable_contract="Signed commit and passing contract tests",
        dependencies=(),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    accepted = governance.respond_to_assignment(
        principal=contributor,
        idempotency_key="accept-assignment",
        program_id="program-1",
        expected_version=proposed.aggregate_version,
        assignment_id="task-1",
        accept=True,
        reason="",
    )
    started = governance.start_assignment(
        principal=contributor,
        idempotency_key="start-assignment",
        program_id="program-1",
        expected_version=accepted.aggregate_version,
        assignment_id="task-1",
    )
    submitted = governance.submit_assignment(
        principal=contributor,
        idempotency_key="submit-assignment",
        program_id="program-1",
        expected_version=started.aggregate_version,
        assignment_id="task-1",
        artifact_refs=("git:commit:abc123", "test-report:42"),
    )
    verified = governance.review_assignment(
        principal=lead,
        idempotency_key="verify-assignment",
        program_id="program-1",
        expected_version=submitted.aggregate_version,
        assignment_id="task-1",
        accept=True,
        note="Contract tests passed.",
    )

    view = governance.read_program(principal=contributor, program_id="program-1")
    assert verified.aggregate_version == 13
    assert view.plans["plan-1"].state.value == "approved"
    assert view.assignments["task-1"].state.value == "verified"
    assert audit.verify_tenant_chain("team-a") == 7
    assert audit.verify_tenant_chain("team-b") == 6


def test_service_rejects_idempotency_conflict_and_hides_nonmember_program() -> None:
    governance, _ = service()
    lead = principal(
        "lead-a",
        "team-a",
        roles=frozenset({"collaboration_creator"}),
    )
    governance.create_program(
        principal=lead,
        idempotency_key="create-program",
        program_id="program-1",
        title="Original title",
        objective="Original objective",
        classification=Classification.INTERNAL,
        compartments=frozenset(),
    )

    with pytest.raises(IdempotencyConflict):
        governance.create_program(
            principal=lead,
            idempotency_key="create-program",
            program_id="program-1",
            title="Changed title",
            objective="Original objective",
            classification=Classification.INTERNAL,
            compartments=frozenset(),
        )
    with pytest.raises(ResourceNotFound):
        governance.read_program(
            principal=principal("outsider", "team-x"),
            program_id="program-1",
        )
