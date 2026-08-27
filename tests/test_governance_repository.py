import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.collaboration.governance import GovernanceBoard
from coifesp_harness.collaboration.governance_models import (
    BoardMember,
    CollaborationRole,
    DiscussionKind,
)
from coifesp_harness.collaboration.repository import (
    GOVERNANCE_EVENTS,
    GOVERNANCE_OUTBOX,
    SQLAlchemyGovernanceRepository,
)
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.idempotency import ClaimStatus
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification


def make_repository():
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
    value = SQLAlchemyGovernanceRepository(engine=engine, audit_log=audit)
    value.create_schema()
    return value, audit


def new_board(audit):
    board = GovernanceBoard(
        program_id="program-1",
        owner_tenant_id="team-a",
        title="Cross-team delivery",
        objective="Integrate explicitly shared deliverables",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
        audit=audit,
    )
    board.add_member(BoardMember("lead-a", "team-a", CollaborationRole.LEAD))
    board.add_member(
        BoardMember("contributor-b", "team-b", CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    board.add_member(
        BoardMember("contributor-c", "team-c", CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    return board


def persist_members(repository):
    sink = InMemoryAuditSink()
    board = new_board(sink)
    repository.create(
        board=board,
        actor_id="lead-a",
        events=tuple(sink.events),
    )
    return board


def test_repository_persists_normalized_state_events_outbox_and_audit() -> None:
    repository, audit = make_repository()
    persist_members(repository)

    sink = InMemoryAuditSink()
    board = repository.load(
        tenant_id="team-a",
        program_id="program-1",
        audit=sink,
    )
    assert board is not None
    plan = board.create_plan(
        actor_id="lead-a",
        plan_id="plan-1",
        version=1,
        title="Restricted contract",
        objective="Expose implementation contract only to team B",
        deliverables=("OpenAPI contract", "contract tests"),
        required_approvers=frozenset({"lead-a", "contributor-b"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    repository.save(
        board=board,
        expected_version=1,
        actor_id="lead-a",
        events=tuple(sink.events),
    )

    visible = repository.load(
        tenant_id="team-b",
        program_id="program-1",
        audit=InMemoryAuditSink(),
    )
    hidden = repository.load(
        tenant_id="team-c",
        program_id="program-1",
        audit=InMemoryAuditSink(),
    )
    outsider = repository.load(
        tenant_id="team-x",
        program_id="program-1",
        audit=InMemoryAuditSink(),
    )
    assert visible is not None and "plan-1" in visible.plans
    assert hidden is not None and "plan-1" not in hidden.plans
    assert outsider is None
    assert audit.verify_tenant_chain("team-a") == 5

    with repository.engine.connect() as connection:
        event_count = connection.execute(
            select(func.count()).select_from(GOVERNANCE_EVENTS)
        ).scalar_one()
        outbox_count = connection.execute(
            select(func.count()).select_from(GOVERNANCE_OUTBOX)
        ).scalar_one()
    assert event_count == 5
    assert outbox_count == 13


def test_repository_rejects_stale_aggregate_save() -> None:
    repository, _ = make_repository()
    persist_members(repository)
    first_sink = InMemoryAuditSink()
    second_sink = InMemoryAuditSink()
    first = repository.load(
        tenant_id="team-a",
        program_id="program-1",
        audit=first_sink,
    )
    second = repository.load(
        tenant_id="team-a",
        program_id="program-1",
        audit=second_sink,
    )
    assert first is not None and second is not None

    first.add_member(
        BoardMember("reviewer-a", "team-a", CollaborationRole.REVIEWER),
        actor_id="lead-a",
    )
    repository.save(
        board=first,
        expected_version=1,
        actor_id="lead-a",
        events=tuple(first_sink.events),
    )
    second.add_member(
        BoardMember("reviewer-2", "team-a", CollaborationRole.REVIEWER),
        actor_id="lead-a",
    )
    with pytest.raises(GovernanceConflictError, match="stale"):
        repository.save(
            board=second,
            expected_version=1,
            actor_id="lead-a",
            events=tuple(second_sink.events),
        )


def test_user_workspace_lists_only_memberships_and_relevant_assignments() -> None:
    repository, _ = make_repository()
    board = persist_members(repository)
    lead_sink = InMemoryAuditSink()
    board = repository.load(tenant_id="team-a", program_id="program-1", audit=lead_sink)
    plan = board.create_plan(actor_id="lead-a", plan_id="plan-1", version=1,
        title="Delivery", objective="Deliver", deliverables=("artifact",),
        required_approvers=frozenset({"lead-a"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}))
    board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    board.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    board.propose_assignment(actor_id="lead-a", assignment_id="task-1",
        plan_id=plan.plan_id, assignee_id="contributor-b", title="Implement",
        description="Do the work", deliverable_contract="artifact",
        visible_to_tenants=frozenset({"team-a", "team-b"}))
    repository.save(board=board, expected_version=1, actor_id="lead-a",
                    events=tuple(lead_sink.events))
    programs = repository.list_program_summaries(
        tenant_id="team-b", principal_id="contributor-b")
    assignments = repository.list_assignment_summaries(
        tenant_id="team-b", principal_id="contributor-b")
    hidden = repository.list_assignment_summaries(
        tenant_id="team-c", principal_id="contributor-c")
    assert [(item["program_id"], item["role"]) for item in programs] == [
        ("program-1", "contributor")]
    assert [item["assignment_id"] for item in assignments] == ["task-1"]
    assert hidden == ()


def test_audit_failure_rolls_back_governance_state_and_outbox() -> None:
    repository, _ = make_repository()
    persist_members(repository)
    sink = InMemoryAuditSink()
    board = repository.load(
        tenant_id="team-a",
        program_id="program-1",
        audit=sink,
    )
    assert board is not None
    board.add_member(
        BoardMember("reviewer-a", "team-a", CollaborationRole.REVIEWER),
        actor_id="lead-a",
    )

    class FailingAudit:
        engine = repository.engine

        @staticmethod
        def append_in_transaction(connection, event):
            raise RuntimeError("simulated audit outage")

    repository.audit_log = FailingAudit()
    with pytest.raises(RuntimeError, match="audit outage"):
        repository.save(
            board=board,
            expected_version=1,
            actor_id="lead-a",
            events=tuple(sink.events),
        )

    reloaded = repository.load(
        tenant_id="team-a",
        program_id="program-1",
        audit=InMemoryAuditSink(),
    )
    assert reloaded is not None
    assert reloaded.aggregate_version == 1
    assert "reviewer-a" not in reloaded.members


def test_hidden_tenant_cannot_join_plan_discussion_after_reload() -> None:
    repository, _ = make_repository()
    persist_members(repository)
    lead_sink = InMemoryAuditSink()
    lead_view = repository.load(
        tenant_id="team-a",
        program_id="program-1",
        audit=lead_sink,
    )
    assert lead_view is not None
    plan = lead_view.create_plan(
        actor_id="lead-a",
        plan_id="plan-hidden",
        version=1,
        title="Need-to-know plan",
        objective="Avoid leaking unrelated implementation details",
        deliverables=("explicit contract",),
        required_approvers=frozenset({"lead-a", "contributor-b"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    lead_view.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    repository.save(
        board=lead_view,
        expected_version=1,
        actor_id="lead-a",
        events=tuple(lead_sink.events),
    )
    team_c_sink = InMemoryAuditSink()
    team_c = repository.load(
        tenant_id="team-c",
        program_id="program-1",
        audit=team_c_sink,
    )
    assert team_c is not None
    assert "plan-hidden" not in team_c.plans
    with pytest.raises(Exception, match="plan not found"):
        team_c.add_discussion_item(
            actor_id="contributor-c",
            plan_id="plan-hidden",
            item_id="should-not-exist",
            kind=DiscussionKind.COMMENT,
            content="hidden",
        )


def test_command_claim_is_atomic_idempotent_and_conflict_detecting() -> None:
    repository, _ = make_repository()
    with repository.engine.begin() as connection:
        claimed = repository.claim_command_in_transaction(
            connection,
            tenant_id="team-a",
            idempotency_key="command-1",
            program_id="program-1",
            command_type="program.create",
            request_digest="a" * 64,
        )
        assert claimed.status is ClaimStatus.CLAIMED
        repository.complete_command_in_transaction(
            connection,
            tenant_id="team-a",
            idempotency_key="command-1",
            result_version=1,
        )

    with repository.engine.begin() as connection:
        duplicate = repository.claim_command_in_transaction(
            connection,
            tenant_id="team-a",
            idempotency_key="command-1",
            program_id="program-1",
            command_type="program.create",
            request_digest="a" * 64,
        )
        conflict = repository.claim_command_in_transaction(
            connection,
            tenant_id="team-a",
            idempotency_key="command-1",
            program_id="program-1",
            command_type="program.create",
            request_digest="b" * 64,
        )
    assert duplicate.status is ClaimStatus.DUPLICATE
    assert duplicate.result_version == 1
    assert conflict.status is ClaimStatus.CONFLICT


def test_rolled_back_command_claim_can_be_retried() -> None:
    repository, _ = make_repository()
    connection = repository.engine.connect()
    transaction = connection.begin()
    first = repository.claim_command_in_transaction(
        connection,
        tenant_id="team-a",
        idempotency_key="command-rollback",
        program_id="program-1",
        command_type="member.add",
        request_digest="c" * 64,
    )
    transaction.rollback()
    connection.close()

    with repository.engine.begin() as retry_connection:
        retry = repository.claim_command_in_transaction(
            retry_connection,
            tenant_id="team-a",
            idempotency_key="command-rollback",
            program_id="program-1",
            command_type="member.add",
            request_digest="c" * 64,
        )
    assert first.status is ClaimStatus.CLAIMED
    assert retry.status is ClaimStatus.CLAIMED
