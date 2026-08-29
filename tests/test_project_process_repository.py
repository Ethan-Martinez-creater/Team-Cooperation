from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.project_process import (
    HumanGateService,
    ProjectExecutionBudgetService,
    ProjectExecutionReservationStatus,
    ProjectGateStatus,
    ProjectInputRequestStatus,
    ProjectProcessCommandService,
    ProjectProcessCommandStatus,
    ProjectProcessCommandType,
    ProjectProcessOutboxService,
    ProjectProcessOutboxStatus,
    ProjectProcessService,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.project_process import (
    ProjectProcessPhase as Phase,
)
from coifesp_harness.project_process import (
    ProjectProcessStatus as Status,
)
from coifesp_harness.project_process import (
    ProjectProcessWaitReason as WaitReason,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    accounts.register_team(team_id="team-a", team_handle="team-a", team_name="Team A")
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    accounts.ensure_active_account(
        account_id="member-a",
        username="member-a",
        display_name="Member A",
        email="member-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.MEMBER,
    )
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="Process project",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    repository = SQLAlchemyProjectProcessRepository(engine)
    repository.create_schema()
    service = ProjectProcessService(repository, clock=lambda: NOW)
    service.create_policy(
        policy_id="policy-a",
        project_id="project-a",
        max_agent_runs=10,
        max_total_tokens=10000,
        max_model_cost_microusd=100000,
        max_replans=3,
        max_generated_tasks=20,
        max_active_agent_runs=4,
        max_active_runs_per_team=2,
        max_specialist_depth=2,
        max_specialist_runs_per_task=2,
        deadline_at=NOW + timedelta(days=30),
        version=1,
    )
    process = service.start_process(
        process_id="process-a",
        project_id="project-a",
        execution_policy_id="policy-a",
        started_by="lead-a",
    )
    return repository, service, process


def _event_args(**overrides):
    values = {
        "process_id": "process-a",
        "event_id": "event-goal",
        "event_type": "project.goal.confirmed",
        "transition_key": "goal.confirmed",
        "expected_version": 1,
        "subject_type": "goal",
        "subject_id": "goal-a",
        "initiated_by": "lead-a",
        "executed_as": "lead-a",
        "correlation_id": "corr-a",
        "payload": {"goal_id": "goal-a"},
        "source_aggregate_version": 1,
    }
    values.update(overrides)
    return values


def _advance_to_execution_ready(service):
    steps = (
        ("event-goal", "project.goal.confirmed", "goal.confirmed", 1),
        ("event-analysis-start", "project.analysis.started", "analysis.started", 2),
        ("event-analysis-done", "project.analysis.completed", "analysis.completed", 3),
        ("event-plan", "project.plan.approved", "plan.approved", 4),
    )
    for event_id, event_type, transition_key, expected_version in steps:
        service.apply_transition(
            process_id="process-a",
            event_id=event_id,
            event_type=event_type,
            transition_key=transition_key,
            expected_version=expected_version,
            subject_type="project",
            subject_id="project-a",
            initiated_by="lead-a",
            executed_as="service:project-orchestrator",
            correlation_id="corr-advance",
            payload={"step": transition_key},
        )


def test_transition_and_event_commit_together_and_duplicate_converges():
    repository, service, _ = _stack()
    changed, event = service.apply_transition(**_event_args())
    assert (changed.phase, changed.status, changed.wait_reason) == (
        Phase.ANALYSIS,
        Status.READY,
        WaitReason.NONE,
    )
    assert changed.version == 2
    assert event.sequence == 2
    assert event.process_version_after == 2
    assert event.transition_key == "goal.confirmed"
    replayed, same = service.apply_transition(**_event_args())
    assert replayed.version == 2
    assert same.event_id == event.event_id
    with repository.transaction() as connection:
        assert len(repository.events(connection, "process-a")) == 2


def test_conflicting_event_retry_and_stale_transition_fail_closed():
    _, service, _ = _stack()
    service.apply_transition(**_event_args())
    with pytest.raises(GovernanceConflictError, match="different content"):
        service.apply_transition(**_event_args(payload={"goal_id": "different"}))
    with pytest.raises(GovernanceConflictError, match="different content"):
        service.apply_transition(**_event_args(executed_as="someone-else"))
    with pytest.raises(GovernanceConflictError, match="stale"):
        service.apply_transition(**_event_args(event_id="event-other", expected_version=1))


def test_event_catalog_rejects_unknown_fact_selector_mismatch_and_wrong_v2_schema():
    _, service, _ = _stack()
    with pytest.raises(ValueError, match="event catalog"):
        service.append_fact(
            process_id="process-a",
            event_id="event-unknown",
            event_type="invented.fact",
            expected_version=1,
            expected_event_sequence=1,
            subject_type="project",
            subject_id="project-a",
            initiated_by="lead-a",
            executed_as="lead-a",
            correlation_id="corr-unknown",
            payload={},
        )
    with pytest.raises(ValueError, match="do not match"):
        service.apply_transition(
            **_event_args(
                event_id="event-mismatch",
                event_type="project.goal.confirmed",
                transition_key="analysis.started",
            )
        )
    with pytest.raises(ValueError, match="require schema v2"):
        service.append_fact(
            process_id="process-a",
            event_id="event-close-v1",
            event_type="project.gate.closed",
            expected_version=1,
            expected_event_sequence=1,
            subject_type="project_gate",
            subject_id="gate-a",
            initiated_by="lead-a",
            executed_as="lead-a",
            correlation_id="corr-close",
            payload={"status": "CANCELLED"},
            schema_version="v1",
        )


def test_non_transition_fact_advances_sequence_not_version():
    _, service, _ = _stack()
    process, event = service.append_fact(
        process_id="process-a",
        event_id="event-risk",
        event_type="risk.created",
        expected_version=1,
        expected_event_sequence=1,
        subject_type="risk",
        subject_id="risk-a",
        initiated_by="lead-a",
        executed_as="lead-a",
        correlation_id="corr-risk",
        payload={"severity": "high"},
    )
    assert process.version == 1
    assert process.last_event_sequence == 2
    assert event.process_version_after is None
    with pytest.raises(GovernanceConflictError, match="cursor is stale"):
        service.append_fact(
            process_id="process-a",
            event_id="event-risk-2",
            event_type="risk.created",
            expected_version=1,
            expected_event_sequence=1,
            subject_type="risk",
            subject_id="risk-b",
            initiated_by="lead-a",
            executed_as="lead-a",
            correlation_id="corr-risk-2",
            payload={"severity": "low"},
        )


def test_only_one_active_process_per_project():
    _, service, _ = _stack()
    with pytest.raises(GovernanceConflictError, match="active process"):
        service.start_process(
            process_id="process-b",
            project_id="project-a",
            execution_policy_id="policy-a",
            started_by="lead-a",
        )


def test_lease_fencing_rejects_parallel_and_stale_owner():
    _, service, _ = _stack()
    token, expires = service.claim_lease(process_id="process-a", owner="worker-a")
    assert expires == NOW + timedelta(seconds=30)
    with pytest.raises(GovernanceConflictError, match="unavailable"):
        service.claim_lease(process_id="process-a", owner="worker-b")
    with pytest.raises(GovernanceConflictError, match="stale"):
        service.release_lease(process_id="process-a", owner="worker-a", token="old-token")
    service.release_lease(process_id="process-a", owner="worker-a", token=token)
    replacement, _ = service.claim_lease(process_id="process-a", owner="worker-b")
    assert replacement != token


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "raw model prompt"},
        {"nested": {"secret": "credential"}},
        {"items": [{"raw_tool_arguments": {"path": "private"}}]},
    ],
)
def test_event_payload_rejects_sensitive_or_raw_execution_data(payload):
    _, service, _ = _stack()
    with pytest.raises(ValueError, match="forbidden data"):
        service.append_fact(
            process_id="process-a",
            event_id="event-sensitive",
            event_type="risk.created",
            expected_version=1,
            expected_event_sequence=0,
            subject_type="risk",
            subject_id="risk-a",
            initiated_by="lead-a",
            executed_as="lead-a",
            correlation_id="corr-sensitive",
            payload=payload,
        )


def test_atomic_budget_reservation_settlement_release_and_retries():
    repository, process_service, _ = _stack()
    _advance_to_execution_ready(process_service)
    budget = ProjectExecutionBudgetService(repository, clock=lambda: NOW)

    first, usage = budget.reserve(
        reservation_id="reservation-1",
        reservation_key="process-a:work-1:team-a:policy-a:1:1",
        process_id="process-a",
        work_node_id="work-1",
        team_id="team-a",
        execution_attempt=1,
        expected_usage_version=1,
        reserved_tokens=500,
        reserved_model_cost_microusd=1000,
    )
    assert first.status is ProjectExecutionReservationStatus.RESERVED
    assert (usage.agent_runs_started, usage.active_agent_runs, usage.version) == (1, 1, 2)
    repeated, repeated_usage = budget.reserve(
        reservation_id="reservation-1",
        reservation_key="process-a:work-1:team-a:policy-a:1:1",
        process_id="process-a",
        work_node_id="work-1",
        team_id="team-a",
        execution_attempt=1,
        expected_usage_version=1,
        reserved_tokens=500,
        reserved_model_cost_microusd=1000,
    )
    assert repeated == first
    assert repeated_usage.version == 2
    with pytest.raises(GovernanceConflictError, match="different content"):
        budget.reserve(
            reservation_id="reservation-other",
            reservation_key="process-a:work-1:team-a:policy-a:1:1",
            process_id="process-a",
            work_node_id="different-work",
            team_id="team-a",
            execution_attempt=1,
            expected_usage_version=2,
            reserved_tokens=500,
            reserved_model_cost_microusd=1000,
        )

    second, usage = budget.reserve(
        reservation_id="reservation-2",
        reservation_key="process-a:work-2:team-a:policy-a:1:1",
        process_id="process-a",
        work_node_id="work-2",
        team_id="team-a",
        execution_attempt=1,
        expected_usage_version=2,
        reserved_tokens=500,
        reserved_model_cost_microusd=1000,
    )
    assert usage.active_agent_runs == 2
    with pytest.raises(GovernanceConflictError, match="team run limit"):
        budget.reserve(
            reservation_id="reservation-3",
            reservation_key="process-a:work-3:team-a:policy-a:1:1",
            process_id="process-a",
            work_node_id="work-3",
            team_id="team-a",
            execution_attempt=1,
            expected_usage_version=3,
            reserved_tokens=500,
            reserved_model_cost_microusd=1000,
        )

    bound = budget.bind_agent_run(reservation_id=first.reservation_id, agent_run_id="run-1")
    assert bound.agent_run_id == "run-1"
    settled, usage = budget.settle(
        reservation_id=first.reservation_id,
        terminal_event_id="run-1-completed",
        agent_run_id="run-1",
        total_tokens=250,
        model_cost_microusd=500,
        expected_usage_version=3,
    )
    assert settled.status is ProjectExecutionReservationStatus.SETTLED
    assert (usage.agent_runs_completed, usage.active_agent_runs, usage.total_tokens) == (1, 1, 250)
    _, retried_usage = budget.settle(
        reservation_id=first.reservation_id,
        terminal_event_id="run-1-completed",
        agent_run_id="run-1",
        total_tokens=250,
        model_cost_microusd=500,
        expected_usage_version=3,
    )
    assert retried_usage.version == usage.version
    with pytest.raises(GovernanceConflictError, match="conflicts"):
        budget.settle(
            reservation_id=first.reservation_id,
            terminal_event_id="run-1-other",
            agent_run_id="run-1",
            total_tokens=250,
            model_cost_microusd=500,
            expected_usage_version=usage.version,
        )

    released, usage = budget.release(
        reservation_id=second.reservation_id,
        release_event_id="dispatch-2-failed",
        expected_usage_version=usage.version,
    )
    assert released.status is ProjectExecutionReservationStatus.RELEASED
    assert usage.active_agent_runs == 0
    assert usage.agent_runs_started == 2
    assert usage.agent_runs_completed == 1


def test_budget_admission_rejects_waiting_process_and_stale_usage():
    repository, process_service, _ = _stack()
    budget = ProjectExecutionBudgetService(repository, clock=lambda: NOW)
    with pytest.raises(GovernanceConflictError, match="cannot admit"):
        budget.reserve(
            reservation_id="reservation-1",
            reservation_key="reservation-key-1",
            process_id="process-a",
            work_node_id="work-1",
            team_id="team-a",
            execution_attempt=1,
            expected_usage_version=1,
        )
    _advance_to_execution_ready(process_service)
    with pytest.raises(GovernanceConflictError, match="usage version is stale"):
        budget.reserve(
            reservation_id="reservation-1",
            reservation_key="reservation-key-1",
            process_id="process-a",
            work_node_id="work-1",
            team_id="team-a",
            execution_attempt=1,
            expected_usage_version=9,
        )


def test_budget_exhaustion_atomically_opens_human_approval_gate():
    repository, process_service, _ = _stack()
    _advance_to_execution_ready(process_service)
    budget = ProjectExecutionBudgetService(repository, clock=lambda: NOW)
    human = HumanGateService(repository, clock=lambda: NOW)
    reservation, usage, gate = budget.reserve_or_open_budget_gate(
        human_gate_service=human,
        created_by="service:project-orchestrator",
        correlation_id="corr-budget-exhausted",
        reservation_id="reservation-too-large",
        reservation_key="reservation-key-too-large",
        process_id="process-a",
        work_node_id="work-large",
        team_id="team-a",
        execution_attempt=1,
        expected_usage_version=1,
        reserved_tokens=10001,
        reserved_model_cost_microusd=100,
    )
    assert reservation is None and usage is None
    assert gate.status is ProjectGateStatus.OPEN
    with repository.transaction() as connection:
        process = repository.process(connection, "process-a")
        events = repository.events(connection, "process-a")
    assert (process.status, process.wait_reason, process.version) == (
        Status.WAITING,
        WaitReason.HUMAN_APPROVAL,
        6,
    )
    assert [event.event_type for event in events[-2:]] == [
        "project.budget.exhausted",
        "project.gate.opened",
    ]
    assert events[-1].transition_key == "human_approval.opened"


def test_input_request_is_durable_idempotent_and_does_not_directly_resume():
    repository, _, _ = _stack()
    human = HumanGateService(repository, clock=lambda: NOW)
    request, process, opened = human.create_input_request(
        request_id="input-1",
        process_id="process-a",
        work_node_id="work-1",
        requested_by_run_id="run-1",
        requested_by_agent_id="team-agent:team-a",
        question="Which deployment region is required?",
        input_schema={"type": "object", "required": ["region"]},
        context_projection={"project": "Project A"},
        created_by="service:project-orchestrator",
        event_id="event-input-open",
        expected_process_version=1,
        expected_event_sequence=1,
        correlation_id="corr-input",
    )
    assert request.status is ProjectInputRequestStatus.OPEN
    assert process.version == 1
    assert process.last_event_sequence == 2
    assert opened.transition_key is None

    answered, waiting, event = human.answer_input(
        request_id="input-1",
        response={"region": "cn-north"},
        actor_id="lead-a",
        idempotency_key="answer-input-1",
        event_id="event-input-answer",
        expected_object_version=1,
        expected_process_version=1,
        correlation_id="corr-input",
    )
    assert answered.status is ProjectInputRequestStatus.ANSWERED
    assert answered.version == 2
    assert waiting.status is Status.WAITING
    assert waiting.wait_reason is WaitReason.HUMAN_INPUT
    assert waiting.version == 1
    assert waiting.last_event_sequence == 3
    assert event.event_type == "human.input.provided"
    same, same_process, same_event = human.answer_input(
        request_id="input-1",
        response={"region": "cn-north"},
        actor_id="lead-a",
        idempotency_key="answer-input-1",
        event_id="event-input-answer",
        expected_object_version=1,
        expected_process_version=1,
        correlation_id="corr-input",
    )
    assert same == answered
    assert same_process.last_event_sequence == 3
    assert same_event.event_id == event.event_id
    with pytest.raises(GovernanceConflictError, match="conflicts"):
        human.answer_input(
            request_id="input-1",
            response={"region": "different"},
            actor_id="lead-a",
            idempotency_key="answer-input-1",
            event_id="event-input-answer",
            expected_object_version=1,
            expected_process_version=1,
            correlation_id="corr-input",
        )


def test_gate_priority_authorization_decision_and_close_are_persisted():
    repository, _, _ = _stack()
    human = HumanGateService(repository, clock=lambda: NOW)
    gate, process, opened = human.create_gate(
        gate_id="gate-budget",
        process_id="process-a",
        gate_type="BUDGET",
        subject_type="project_execution_policy",
        subject_id="policy-a",
        required_roles=("owner", "admin"),
        allowed_decisions=("INCREASE_BUDGET", "REDUCE_SCOPE", "TERMINATE"),
        reason="Project run budget exhausted",
        created_by="service:project-orchestrator",
        event_id="event-gate-open",
        expected_process_version=1,
        expected_event_sequence=1,
        correlation_id="corr-gate",
    )
    assert gate.status is ProjectGateStatus.OPEN
    assert (process.status, process.wait_reason, process.version) == (
        Status.WAITING,
        WaitReason.HUMAN_APPROVAL,
        2,
    )
    assert opened.transition_key == "human_approval.opened"
    with pytest.raises(GovernanceConflictError, match="human principal"):
        human.decide_gate(
            gate_id="gate-budget",
            decision="REDUCE_SCOPE",
            reason="agent cannot approve",
            actor_id="team-agent:team-a",
            idempotency_key="gate-decision-1",
            event_id="event-gate-decision",
            expected_object_version=1,
            expected_process_version=2,
            correlation_id="corr-gate",
        )
    with pytest.raises(GovernanceConflictError, match="not authorized"):
        human.decide_gate(
            gate_id="gate-budget",
            decision="REDUCE_SCOPE",
            reason="not authorized",
            actor_id="member-a",
            idempotency_key="gate-decision-1",
            event_id="event-gate-decision",
            expected_object_version=1,
            expected_process_version=2,
            correlation_id="corr-gate",
        )
    decided, waiting, event = human.decide_gate(
        gate_id="gate-budget",
        decision="REDUCE_SCOPE",
        reason="Reduce optional work",
        actor_id="lead-a",
        idempotency_key="gate-decision-1",
        event_id="event-gate-decision",
        expected_object_version=1,
        expected_process_version=2,
        correlation_id="corr-gate",
    )
    assert decided.status is ProjectGateStatus.DECIDED
    assert waiting.wait_reason is WaitReason.HUMAN_APPROVAL
    assert waiting.version == 2
    assert waiting.last_event_sequence == 3
    assert event.event_type == "project.gate.decided"

    second, process, _ = human.create_gate(
        gate_id="gate-close",
        process_id="process-a",
        gate_type="BUDGET",
        subject_type="project_execution_policy",
        subject_id="policy-a",
        required_roles=("admin",),
        allowed_decisions=("INCREASE_BUDGET", "REDUCE_SCOPE", "TERMINATE"),
        reason="Obsolete request",
        created_by="service:project-orchestrator",
        event_id="event-gate-close-open",
        expected_process_version=2,
        expected_event_sequence=3,
        correlation_id="corr-close",
    )
    assert second.status is ProjectGateStatus.OPEN
    closed, _, closed_event = human.close_gate(
        gate_id="gate-close",
        status=ProjectGateStatus.CANCELLED,
        actor_id="lead-a",
        idempotency_key="gate-close-1",
        event_id="event-gate-close",
        expected_object_version=1,
        expected_process_version=2,
        correlation_id="corr-close",
    )
    assert closed.status is ProjectGateStatus.CANCELLED
    assert closed_event.event_type == "project.gate.closed"
    assert closed_event.schema_version == "v2"


def test_process_command_snapshot_guard_and_outbox_recovery():
    repository, _, process = _stack()
    commands = ProjectProcessCommandService(repository, clock=lambda: NOW)
    command = commands.record(
        command_id="command-1",
        process_id=process.process_id,
        decision_id="decision-1",
        command_type=ProjectProcessCommandType.PROPOSE_RISK,
        request={"severity": "high", "summary": "Dependency risk"},
        based_on_process_version=1,
        based_on_event_sequence=1,
        graph_snapshot_digest="sha256:" + "a" * 64,
    )
    assert command.status is ProjectProcessCommandStatus.PENDING
    same = commands.record(
        command_id="command-1",
        process_id=process.process_id,
        decision_id="decision-1",
        command_type=ProjectProcessCommandType.PROPOSE_RISK,
        request={"severity": "high", "summary": "Dependency risk"},
        based_on_process_version=1,
        based_on_event_sequence=1,
        graph_snapshot_digest="sha256:" + "a" * 64,
    )
    assert same == command
    applied = commands.finish(
        command_id="command-1",
        status=ProjectProcessCommandStatus.APPLIED,
        result_subject_id="risk-1",
    )
    assert applied.status is ProjectProcessCommandStatus.APPLIED
    stale = commands.record(
        command_id="command-stale",
        process_id=process.process_id,
        decision_id="decision-old",
        command_type=ProjectProcessCommandType.REQUEST_REPLAN,
        request={"reason": "old snapshot"},
        based_on_process_version=1,
        based_on_event_sequence=0,
        graph_snapshot_digest="sha256:" + "b" * 64,
    )
    assert stale.status is ProjectProcessCommandStatus.STALE

    outbox = ProjectProcessOutboxService(repository, clock=lambda: NOW)
    claimed = outbox.claim(owner="publisher-a")
    assert claimed is not None
    assert claimed.event_id == "process-a:goal-input-requested"
    assert claimed.status is ProjectProcessOutboxStatus.PUBLISHING
    with pytest.raises(GovernanceConflictError, match="fencing token"):
        outbox.publish_succeeded(
            outbox_id=claimed.outbox_id,
            owner="publisher-a",
            token="stale-token",
        )
    published = outbox.publish_succeeded(
        outbox_id=claimed.outbox_id,
        owner="publisher-a",
        token=claimed.lease_token,
    )
    assert published.status is ProjectProcessOutboxStatus.PUBLISHED
    assert outbox.claim(owner="publisher-a") is None
