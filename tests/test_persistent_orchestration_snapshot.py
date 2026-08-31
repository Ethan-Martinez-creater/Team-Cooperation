from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, update
from test_task_verification_service import setup as verification_setup
from test_task_verification_service import verify as verify_task
from test_team_agent_dispatcher import stack

from coifesp_harness.capabilities.repository import CAPABILITY_CAPACITY
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    TEAM_TASKS,
)
from coifesp_harness.project_process.orchestrator import VerificationOutcome
from coifesp_harness.project_process.persistent_snapshot import (
    PersistentProjectOrchestrationSnapshotLoader,
)
from coifesp_harness.project_process.repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_USAGE,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PLANNER_INTENTS,
)
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.task_contracts import TeamTaskContractService
from coifesp_harness.verification.repository import AGENT_REVIEWS, VERIFICATION_METADATA


def _install_verification_schema(value):
    VERIFICATION_METADATA.create_all(value.engine)


def _accept_contract(value, *, input_manifest=None):
    capability = value.facts.requirement
    TeamTaskContractService(value.engine).propose(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-a",
        expected_version=0,
        process_id="process-a",
        work_node_id="node:task:task-a",
        requested_capability={
            "tags": list(capability.tags),
            "protocol": capability.protocol,
            "input_contract_ref": value.facts.contract.input_contract_ref,
            "output_contract_ref": value.facts.contract.output_contract_ref,
            "verification_policy_ref": "verify:review:v1",
        },
        input_manifest=input_manifest or {"resources": [], "work_nodes": []},
        output_contract={
            "artifact_types": ["text/plain"],
            "required": True,
            "max_count": 2,
        },
        verification_policy={
            "criteria": [
                {"criterion_id": "review", "type": "agent_review", "required": True}
            ]
        },
        autonomy_requirement="supervised",
    )
    TeamCollaborationService(value.engine).respond_task(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-b",
        accept=True,
        expected_contract_version=1,
    )


def _load(value, *, artifact_content=None):
    loader = PersistentProjectOrchestrationSnapshotLoader(
        repository=value.repository,
        work_graph_repository=value.dispatcher.work_graph,
        capability_adapter=value.dispatcher.capabilities,
        artifact_content=artifact_content,
        clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
    return loader(process)


def test_accepted_contract_and_live_capacity_produce_ready_snapshot():
    value = stack(accepted=False)
    _install_verification_schema(value)
    _accept_contract(value)

    snapshot = _load(value)

    assert [item.work_id for item in snapshot.readiness.ready_work] == ["task-a"]
    assert not snapshot.readiness.blocked_work
    assert snapshot.readiness.verification_ready is False
    assert snapshot.has_open_input is False
    assert snapshot.has_open_gate is False
    assert snapshot.verification_outcome is None
    assert snapshot.integration_outcome is None
    assert snapshot.delivery_outcome is None


@pytest.mark.parametrize("slots,ready_count", [(1, 1), (2, 2)])
def test_multiple_tasks_share_one_capacity_pool_without_duplicate_facts(slots, ready_count):
    value = stack(accepted=False, slots=slots)
    _install_verification_schema(value)
    _accept_contract(value)
    with value.engine.begin() as connection:
        task = dict(connection.execute(select(TEAM_TASKS)).mappings().one())
        task.update(task_id="task-b", work_node_id="node:task:task-b")
        connection.execute(insert(TEAM_TASKS).values(**task))
    value.graph.register_existing_subject(node_id="node:task:task-b", project_id="project-a",
                                          node_type="task", subject_id="task-b")
    snapshot = _load(value)
    assert len(snapshot.readiness.ready_work) == ready_count
    assert len(snapshot.readiness.blocked_work) == 2 - ready_count


def test_diagnostic_fallback_cannot_override_directory_denial():
    value = stack(accepted=False)
    _install_verification_schema(value)
    _accept_contract(value)
    adapter = value.dispatcher.capabilities
    original = adapter.using_connection

    def deny(connection):
        bound = original(connection)
        bound.match = lambda **_: ()
        return bound

    adapter.using_connection = deny
    assert not _load(value).readiness.ready_work


@pytest.mark.parametrize("limit", ["max_replans", "max_generated_tasks"])
def test_snapshot_preserves_frozen_project_limit_admission_semantics(limit):
    value = stack(accepted=False)
    _install_verification_schema(value)
    _accept_contract(value)
    with value.engine.begin() as connection:
        connection.execute(update(PROJECT_EXECUTION_POLICIES).values(**{limit: 0}))
    snapshot = _load(value)
    assert not snapshot.readiness.ready_work
    assert any(reason.value == "budget_exhausted" for item in snapshot.readiness.blocked_work
               for reason in item.reasons)


@pytest.mark.parametrize("case", ["missing_contract", "unavailable_capacity", "exhausted_budget"])
def test_loader_never_fabricates_readiness_for_missing_runtime_facts(case):
    value = stack(accepted=False)
    _install_verification_schema(value)
    if case != "missing_contract":
        _accept_contract(value)
    if case == "unavailable_capacity":
        with value.engine.begin() as connection:
            connection.execute(
                update(CAPABILITY_CAPACITY)
                .where(CAPABILITY_CAPACITY.c.provider_tenant_id == "team-b")
                .values(status="unavailable", available_slots=0)
            )
    if case == "exhausted_budget":
        with value.engine.begin() as connection:
            connection.execute(
                update(PROJECT_EXECUTION_USAGE)
                .where(PROJECT_EXECUTION_USAGE.c.process_id == "process-a")
                .values(total_tokens=200_000)
            )

    snapshot = _load(value)
    reasons = {
        reason.value
        for item in snapshot.readiness.blocked_work
        for reason in item.reasons
    }
    assert not snapshot.readiness.ready_work
    if case == "missing_contract":
        assert "contract_not_accepted" in reasons
    elif case == "unavailable_capacity":
        assert reasons.intersection({"capability_unavailable", "capacity_unavailable"})
    else:
        assert "budget_exhausted" in reasons


def test_required_input_withdrawal_blocks_a_previously_accepted_contract():
    value = stack(accepted=False)
    _install_verification_schema(value)
    with value.engine.begin() as connection:
        connection.execute(
            insert(PROJECT_RESOURCES).values(
                resource_id="resource-input",
                project_id="project-a",
                owner_team_id="team-a",
                created_by="lead-a",
                title="Input",
                artifact_owner_team_id="team-a",
                artifact_id="artifact-input",
                artifact_sha256="a" * 64,
                media_type="text/plain",
                propagation="project_readonly",
                created_at=datetime.now(UTC),
            )
        )
    _accept_contract(
        value,
        input_manifest={
            "resources": [
                {
                    "resource_id": "resource-input",
                    "required": True,
                    "mode": "project_readonly",
                }
            ],
            "work_nodes": [],
        },
    )
    with value.engine.begin() as connection:
        connection.execute(
            update(PROJECT_RESOURCES)
            .where(PROJECT_RESOURCES.c.resource_id == "resource-input")
            .values(propagation="team_private")
        )

    snapshot = _load(value)

    assert not snapshot.readiness.ready_work
    assert any(
        reason.value == "contract_not_accepted"
        for item in snapshot.readiness.blocked_work
        for reason in item.reasons
    )


def test_open_input_stops_dispatch_even_when_process_row_is_ready():
    value = stack(accepted=False)
    _install_verification_schema(value)
    _accept_contract(value)
    with value.engine.begin() as connection:
        connection.execute(
            update(PROJECT_INPUT_REQUESTS)
            .where(PROJECT_INPUT_REQUESTS.c.process_id == "process-a")
            .values(
                status="OPEN",
                answered_at=None,
                answered_by=None,
                resolution_idempotency_key=None,
                resolution_event_id=None,
                resolution_sha256=None,
            )
        )

    snapshot = _load(value)

    assert snapshot.has_open_input
    assert not snapshot.readiness.ready_work
    assert any(
        reason.value == "process_not_ready"
        for item in snapshot.readiness.blocked_work
        for reason in item.reasons
    )


def test_active_task_planner_and_reviewer_operations_block_verification():
    value = stack(accepted=False)
    _install_verification_schema(value)
    _accept_contract(value)
    now = datetime.now(UTC)
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        graph = value.dispatcher.work_graph.snapshot(connection, project_id="project-a")
        connection.execute(
            update(TEAM_TASKS)
            .where(TEAM_TASKS.c.task_id == "task-a")
            .values(status="submitted", updated_at=now)
        )
        connection.execute(
            insert(PROJECT_AGENT_RUNS).values(
                project_id="project-a",
                run_id="task-run-active",
                team_id="team-b",
                created_by="lead-b",
                mode="delivery_review",
                conversation_id=None,
                turn_id=None,
                process_id="process-a",
                team_agent_id=value.agent.agent_id,
                work_node_id="node:task:task-a",
                team_task_id="task-a",
                parent_run_id=None,
                orchestration_decision_id=None,
                run_kind="verification",
                initiated_by_principal_id="service:project-orchestrator",
                executed_as_principal_id="service:project-verifier",
                delegation_scope_digest=None,
                execution_attempt=None,
                capacity_reservation_id=None,
                project_budget_reservation_id=None,
                created_at=now,
                task_contract_version=None,
                task_result_status=None,
                task_result_json=None,
                task_result_at=None,
            )
        )
        connection.execute(
            insert(PROJECT_PLANNER_INTENTS).values(
                planner_intent_id="planner-active",
                process_id="process-a",
                project_id="project-a",
                owner_team_id="team-a",
                reason="reconcile",
                based_on_process_version=process.version,
                based_on_event_sequence=process.last_event_sequence,
                graph_snapshot_digest=graph.digest,
                status="PENDING",
                run_id="planner-run-active",
                decision_id=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                projected_at=None,
            )
        )
        connection.execute(
            insert(AGENT_REVIEWS).values(
                review_id="review-active",
                verification_id="verification-active",
                source_run_id="task-run-active",
                run_id="review-run-active",
                project_id="project-a",
                process_id="process-a",
                task_id="task-a",
                owner_team_id="team-b",
                criterion_id="review",
                criterion_key="r" * 64,
                subject_digest="s" * 64,
                contract_version=1,
                attempt=1,
                budget_reservation_id="review-budget-active",
                status="QUEUED",
                result_json=None,
                error_code=None,
                initiated_by="service:project-orchestrator",
                executed_as="service:project-reviewer",
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )

    snapshot = _load(value)

    assert snapshot.has_active_operation
    assert snapshot.readiness.verification_ready is False


def test_stale_process_cursor_is_rejected_before_snapshot_reads():
    value = stack(accepted=False)
    _install_verification_schema(value)
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")

    loader = PersistentProjectOrchestrationSnapshotLoader(
        repository=value.repository,
        work_graph_repository=value.dispatcher.work_graph,
        capability_adapter=value.dispatcher.capabilities,
    )
    with pytest.raises(GovernanceConflictError, match="stale"):
        loader(replace(process, version=process.version + 1))


def test_cross_team_contract_scope_cannot_become_a_ready_task():
    value = stack(accepted=False)
    _install_verification_schema(value)
    _accept_contract(value)
    with value.engine.begin() as connection:
        connection.execute(
            update(TEAM_TASKS)
            .where(TEAM_TASKS.c.task_id == "task-a")
            .values(target_team_id="team-c")
        )

    snapshot = _load(value)

    assert not snapshot.readiness.ready_work


def test_real_pass_and_fail_evidence_is_exposed_without_inventing_integration(tmp_path):
    value = verification_setup(tmp_path)
    assert verify_task(value)["status"] == "PASS"
    # The verification fixture deliberately leaves the terminal project
    # reservation for the durable accounting replay.  Settle that persisted
    # fact before asserting the loader's verification projection.
    TeamTaskRunAccounting(
        repository=value.repository,
        run_repository=value.runs,
        capability_repository=value.capabilities,
    ).replay_pending()
    loader = PersistentProjectOrchestrationSnapshotLoader(
        repository=value.repository,
        work_graph_repository=value.dispatcher.work_graph,
        capability_adapter=value.dispatcher.capabilities,
        artifact_content=value.content,
    )
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
    passed = loader(process)
    assert passed.verification_outcome is VerificationOutcome.PASSED
    assert passed.integration_outcome is None and passed.delivery_outcome is None

    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    failed_value = verification_setup(failed_root)
    with failed_value.engine.begin() as connection:
        connection.execute(update(PROJECT_RESOURCES).values(propagation="team_private"))
    assert verify_task(failed_value)["status"] == "FAIL"
    failed_loader = PersistentProjectOrchestrationSnapshotLoader(
        repository=failed_value.repository,
        work_graph_repository=failed_value.dispatcher.work_graph,
        capability_adapter=failed_value.dispatcher.capabilities,
        artifact_content=failed_value.content,
    )
    with failed_value.repository.transaction() as connection:
        changed_process = failed_value.repository.process(connection, "process-a")
    failed = failed_loader(changed_process)
    assert failed.verification_outcome is VerificationOutcome.FAILED
