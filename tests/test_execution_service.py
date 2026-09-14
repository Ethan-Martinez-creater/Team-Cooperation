import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.collaboration import CollaborationRole, GovernanceBoard
from coifesp_harness.collaboration.governance_models import BoardMember
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.execution import SQLAlchemyTaskRepository, TaskExecutionService
from coifesp_harness.security import Classification, Principal


class GovernanceView:
    def __init__(self, board: GovernanceBoard) -> None:
        self.board = board

    def read_program(self, *, principal: Principal, program_id: str) -> GovernanceBoard:
        assert program_id == self.board.program_id
        return self.board


def in_progress_board() -> GovernanceBoard:
    board = GovernanceBoard(
        program_id="program-1",
        owner_tenant_id="team-a",
        title="Program",
        objective="Objective",
        classification=Classification.INTERNAL,
        compartments=frozenset(),
        audit=InMemoryAuditSink(),
    )
    board.add_member(BoardMember("lead-a", "team-a", CollaborationRole.LEAD))
    board.add_member(
        BoardMember("worker-b", "team-b", CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    plan = board.create_plan(
        actor_id="lead-a",
        plan_id="plan-1",
        version=1,
        title="Plan",
        objective="Objective",
        deliverables=("deliverable",),
        required_approvers=frozenset({"lead-a"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    board.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    assignment = board.propose_assignment(
        actor_id="lead-a",
        assignment_id="assignment-1",
        plan_id=plan.plan_id,
        assignee_id="worker-b",
        title="Implement",
        description="Private implementation details",
        deliverable_contract="Signed artifact",
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.respond_to_assignment(
        actor_id="worker-b", assignment_id=assignment.assignment_id, accept=True
    )
    board.start_assignment(actor_id="worker-b", assignment_id=assignment.assignment_id)
    return board


def service() -> TaskExecutionService:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyTaskRepository(engine=engine)
    repository.create_schema()
    return TaskExecutionService(
        repository=repository,
        governance=GovernanceView(in_progress_board()),  # type: ignore[arg-type]
    )


def test_legacy_assignment_enqueue_can_be_disabled_for_project_runtime() -> None:
    value = service()
    value.allow_legacy_assignments = False
    with pytest.raises(GovernanceConflictError, match="project work execution"):
        value.enqueue_assignment(
            principal=Principal("worker-b", "team-b"),
            idempotency_key="enqueue-legacy-disabled",
            task_id="execution-legacy-disabled",
            program_id="program-1",
            assignment_id="assignment-1",
            queue="coding",
            payload={"tool": "contract-tests"},
        )


    value = service()
    contributor = Principal("worker-b", "team-b")
    task = value.enqueue_assignment(
        principal=contributor,
        idempotency_key="enqueue-1",
        task_id="execution-1",
        program_id="program-1",
        assignment_id="assignment-1",
        queue="coding",
        payload={"tool": "contract-tests"},
    )
    assert task.created_by == "worker-b"

    with pytest.raises(PolicyDenied, match="worker service identity"):
        value.claim(worker=contributor, queue="coding")

    worker = Principal(
        "worker-service",
        "team-b",
        roles=frozenset({"execution_worker"}),
        is_service=True,
    )
    lease = value.claim(worker=worker, queue="coding")
    assert lease is not None
    value.start(worker=worker, task_id=task.task_id, lease_token=lease.lease_token)
    value.succeed(
        worker=worker,
        task_id=task.task_id,
        lease_token=lease.lease_token,
        result={"artifact_ref": "git:commit:abc"},
    )


def test_non_assignee_cannot_enqueue_governed_execution() -> None:
    value = service()
    with pytest.raises(PolicyDenied, match="only the governance assignee"):
        value.enqueue_assignment(
            principal=Principal("lead-a", "team-a"),
            idempotency_key="enqueue-1",
            task_id="execution-1",
            program_id="program-1",
            assignment_id="assignment-1",
            queue="coding",
            payload={"tool": "contract-tests"},
        )
