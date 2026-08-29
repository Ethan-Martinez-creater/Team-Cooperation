from __future__ import annotations

from dataclasses import replace

import pytest

from coifesp_harness.project_process.readiness import (
    ActiveOperationSnapshot,
    CapabilityReadinessSnapshot,
    ContractReadinessSnapshot,
    DependencySnapshot,
    ProcessReadinessSnapshot,
    ProjectExecutionReadinessSnapshot,
    ProjectReadinessEvaluator,
    ProjectReadinessSnapshot,
    ReadinessBlockReason,
    TeamReadinessSnapshot,
    WorkItemSnapshot,
    WorkItemStatus,
    WorkRelationSnapshot,
    evaluate_readiness,
)


def task(
    work_id: str,
    *,
    status: str | WorkItemStatus = WorkItemStatus.ACCEPTED,
    team_id: str = "team-a",
    node_id: str | None = None,
    capabilities: tuple[str, ...] = (),
    slots: int = 1,
    contract_id: str | None = None,
    contract_accepted: bool | None = True,
    contract_required: bool = False,
    requester_team_id: str | None = None,
    team_available: bool = True,
    active_operation_ids: tuple[str, ...] = (),
) -> WorkItemSnapshot:
    return WorkItemSnapshot(
        work_id=work_id,
        status=status,
        team_id=team_id,
        node_id=node_id,
        required_capabilities=capabilities,
        required_slots=slots,
        contract_id=contract_id,
        contract_accepted=contract_accepted,
        contract_required=contract_required,
        requester_team_id=requester_team_id,
        team_available=team_available,
        active_operation_ids=active_operation_ids,
    )


def reasons(result, work_id: str) -> set[ReadinessBlockReason]:
    decision = result.for_work(work_id)
    assert decision is not None
    return set(decision.reasons)


def test_simple_dependency_chain_uses_graph_node_ids_and_requires_successful_prerequisite():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task("frontend", node_id="node-frontend"),
            task("backend", status=WorkItemStatus.VERIFIED, node_id="node-backend"),
        ),
        relations=(
            WorkRelationSnapshot(
                relation_id="rel-1",
                source_node_id="node-frontend",
                target_node_id="node-backend",
            ),
        ),
    )

    result = evaluate_readiness(snapshot)

    assert [item.work_id for item in result.ready_work] == ["frontend"]
    assert reasons(result, "backend") == {ReadinessBlockReason.TERMINAL}
    assert result.dependency_cycles == ()


def test_parallel_work_is_sorted_and_bounded_by_project_capacity():
    snapshot = ProjectReadinessSnapshot(
        tasks=(task("work-z"), task("work-a")),
        execution=ProjectExecutionReadinessSnapshot(max_active_operations=2),
    )

    result = ProjectReadinessEvaluator().evaluate(snapshot)

    assert [item.work_id for item in result.ready_work] == ["work-a", "work-z"]
    assert result.blocked_work == ()

    one_slot = replace(snapshot, execution=ProjectExecutionReadinessSnapshot(max_active_operations=1))
    limited = evaluate_readiness(one_slot)
    assert [item.work_id for item in limited.ready_work] == ["work-a"]
    assert reasons(limited, "work-z") == {ReadinessBlockReason.PROJECT_CONCURRENCY}


def test_contract_must_be_accepted_before_cross_team_work_is_ready():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task(
                "work-1",
                team_id="team-provider",
                requester_team_id="team-requester",
                contract_id="contract-1",
            ),
        ),
        contracts=(ContractReadinessSnapshot("contract-1", "work-1", accepted=False),),
    )

    result = evaluate_readiness(snapshot)

    assert not result.ready_work
    assert reasons(result, "work-1") == {ReadinessBlockReason.CONTRACT_NOT_ACCEPTED}

    accepted = replace(
        snapshot,
        contracts=(ContractReadinessSnapshot("contract-1", "work-1", accepted=True),),
    )
    assert [item.work_id for item in evaluate_readiness(accepted).ready_work] == ["work-1"]


def test_proposed_work_is_never_ready_even_when_contract_is_accepted():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task(
                "work-1",
                status=WorkItemStatus.PROPOSED,
                team_id="team-provider",
                requester_team_id="team-requester",
                contract_id="contract-1",
                contract_required=True,
            ),
        ),
        contracts=(ContractReadinessSnapshot("contract-1", "work-1", accepted=True),),
    )

    result = evaluate_readiness(snapshot)

    assert not result.ready_work
    assert reasons(result, "work-1") == {ReadinessBlockReason.NOT_DISPATCHABLE}


def test_internal_work_without_contract_requires_explicit_opt_out():
    explicit = ProjectReadinessSnapshot(
        tasks=(task("internal", contract_required=False, contract_accepted=None),),
    )
    assert [item.work_id for item in evaluate_readiness(explicit).ready_work] == ["internal"]

    fail_closed = ProjectReadinessSnapshot(
        tasks=(
            WorkItemSnapshot(
                work_id="internal-default",
                status=WorkItemStatus.ACCEPTED,
                team_id="team-a",
            ),
        ),
    )
    assert reasons(evaluate_readiness(fail_closed), "internal-default") == {
        ReadinessBlockReason.CONTRACT_NOT_ACCEPTED
    }


def test_unknown_contract_reference_fails_closed_even_for_internal_work():
    snapshot = ProjectReadinessSnapshot(
        tasks=(task("work-1", contract_id="missing", contract_required=False),),
    )

    assert reasons(evaluate_readiness(snapshot), "work-1") == {
        ReadinessBlockReason.CONTRACT_NOT_ACCEPTED
    }


def test_changes_requested_with_accepted_contract_is_dispatchable():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task(
                "work-1",
                status=WorkItemStatus.CHANGES_REQUESTED,
                team_id="team-provider",
                requester_team_id="team-requester",
                contract_id="contract-1",
                contract_required=True,
            ),
        ),
        contracts=(ContractReadinessSnapshot("contract-1", "work-1", accepted=True),),
    )

    assert [item.work_id for item in evaluate_readiness(snapshot).ready_work] == ["work-1"]


def test_missing_capability_and_zero_capacity_fail_closed():
    missing = ProjectReadinessSnapshot(
        tasks=(task("missing", capabilities=("backend-api",)),),
    )
    assert reasons(evaluate_readiness(missing), "missing") == {
        ReadinessBlockReason.CAPABILITY_MISSING
    }

    exhausted = ProjectReadinessSnapshot(
        tasks=(task("exhausted", capabilities=("backend-api",)),),
        capabilities=(
            CapabilityReadinessSnapshot(
                "backend-api", "team-a", available_slots=0
            ),
        ),
    )
    assert reasons(evaluate_readiness(exhausted), "exhausted") == {
        ReadinessBlockReason.CAPACITY_UNAVAILABLE
    }


def test_team_availability_and_shared_capability_slots_are_projected_deterministically():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task("second", capabilities=("api",)),
            task("first", capabilities=("api",)),
            task("unavailable", team_available=False),
        ),
        capabilities=(CapabilityReadinessSnapshot("api", "team-a", 1),),
        teams=(TeamReadinessSnapshot("team-a", available=True),),
    )

    result = evaluate_readiness(snapshot)

    assert [item.work_id for item in result.ready_work] == ["first"]
    assert reasons(result, "second") == {ReadinessBlockReason.CAPACITY_UNAVAILABLE}
    assert reasons(result, "unavailable") == {ReadinessBlockReason.TEAM_UNAVAILABLE}


def test_budget_and_project_concurrency_are_independent_blockers():
    budget = ProjectReadinessSnapshot(
        tasks=(task("work-1"),),
        execution=ProjectExecutionReadinessSnapshot(budget_available=False),
    )
    assert reasons(evaluate_readiness(budget), "work-1") == {
        ReadinessBlockReason.BUDGET_EXHAUSTED
    }

    concurrency = ProjectReadinessSnapshot(
        tasks=(task("work-1"),),
        execution=ProjectExecutionReadinessSnapshot(
            active_operations=2,
            max_active_operations=2,
        ),
    )
    assert reasons(evaluate_readiness(concurrency), "work-1") == {
        ReadinessBlockReason.PROJECT_CONCURRENCY
    }


def test_unsatisfied_missing_and_cyclic_dependencies_fail_closed():
    unsatisfied = ProjectReadinessSnapshot(
        tasks=(task("dependent"), task("prerequisite", status=WorkItemStatus.ACCEPTED)),
        dependencies=(DependencySnapshot("dependent", "prerequisite"),),
    )
    result = evaluate_readiness(unsatisfied)
    assert reasons(result, "dependent") == {ReadinessBlockReason.DEPENDENCY_UNSATISFIED}
    assert result.for_work("dependent").blocked_by == ("prerequisite",)

    missing = ProjectReadinessSnapshot(
        tasks=(task("dependent"),),
        dependencies=(DependencySnapshot("dependent", "deleted-work"),),
    )
    assert reasons(evaluate_readiness(missing), "dependent") == {
        ReadinessBlockReason.MISSING_DEPENDENCY
    }

    cycle = ProjectReadinessSnapshot(
        tasks=(task("a"), task("b")),
        dependencies=(DependencySnapshot("a", "b"), DependencySnapshot("b", "a")),
    )
    cycle_result = evaluate_readiness(cycle)
    assert cycle_result.dependency_cycles == (("a", "b"),)
    assert reasons(cycle_result, "a") == {ReadinessBlockReason.DEPENDENCY_CYCLE}
    assert reasons(cycle_result, "b") == {ReadinessBlockReason.DEPENDENCY_CYCLE}


def test_active_operation_is_not_redispatched_and_terminal_work_is_not_ready():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task("running", active_operation_ids=("run-1",)),
            task("terminal", status=WorkItemStatus.REJECTED),
        ),
        active_operations=(ActiveOperationSnapshot("run-1", "running", "team-a"),),
    )

    result = evaluate_readiness(snapshot)

    assert not result.ready_work
    assert reasons(result, "running") == {ReadinessBlockReason.ACTIVE_OPERATION}
    assert reasons(result, "terminal") == {ReadinessBlockReason.TERMINAL}


def test_human_wait_process_blocks_without_overwriting_work_reasons():
    snapshot = ProjectReadinessSnapshot(
        tasks=(task("work-1"),),
        process=ProcessReadinessSnapshot(
            phase="EXECUTION", status="WAITING", wait_reason="HUMAN_APPROVAL"
        ),
    )

    result = evaluate_readiness(snapshot)

    assert reasons(result, "work-1") == {ReadinessBlockReason.PROCESS_WAITING}


def test_all_successful_work_is_terminal_and_verification_ready():
    snapshot = ProjectReadinessSnapshot(
        # Reverse insertion order proves output does not depend on input order.
        tasks=(
            task("work-b", status=WorkItemStatus.VERIFIED),
            task("work-a", status=WorkItemStatus.VERIFIED),
        ),
        dependencies=(DependencySnapshot("work-b", "work-a"),),
    )

    result = evaluate_readiness(snapshot)

    assert result.ready_work == ()
    assert [item.work_id for item in result.blocked_work] == ["work-a", "work-b"]
    assert result.all_work_terminal is True
    assert result.verification_ready is True


def test_submitted_work_is_verification_ready_but_not_terminal():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task("work-b", status=WorkItemStatus.SUBMITTED),
            task("work-a", status=WorkItemStatus.SUBMITTED),
        ),
        dependencies=(DependencySnapshot("work-b", "work-a"),),
    )

    result = evaluate_readiness(snapshot)

    assert result.verification_ready is True
    assert result.all_work_terminal is False


def test_submitted_prerequisite_does_not_make_dependent_work_ready():
    snapshot = ProjectReadinessSnapshot(
        tasks=(
            task("dependent"),
            task("prerequisite", status=WorkItemStatus.SUBMITTED),
        ),
        dependencies=(DependencySnapshot("dependent", "prerequisite"),),
    )

    submitted_result = evaluate_readiness(snapshot)
    assert reasons(submitted_result, "dependent") == {
        ReadinessBlockReason.DEPENDENCY_UNSATISFIED
    }

    verified_result = evaluate_readiness(
        replace(
            snapshot,
            tasks=(
                task("dependent"),
                task("prerequisite", status=WorkItemStatus.VERIFIED),
            ),
        )
    )
    assert [item.work_id for item in verified_result.ready_work] == ["dependent"]


def test_verification_ready_accepts_submitted_and_verified_work_but_not_in_progress():
    submitted_and_verified = ProjectReadinessSnapshot(
        tasks=(
            task("submitted", status=WorkItemStatus.SUBMITTED),
            task("verified", status=WorkItemStatus.VERIFIED),
        ),
    )
    result = evaluate_readiness(submitted_and_verified)
    assert result.verification_ready is True
    assert result.all_work_terminal is False

    in_progress = ProjectReadinessSnapshot(
        tasks=(task("work-1", status=WorkItemStatus.IN_PROGRESS),),
    )
    assert evaluate_readiness(in_progress).verification_ready is False


def test_evaluation_is_stable_across_snapshot_order_and_rejects_duplicate_work_ids():
    first = ProjectReadinessSnapshot(
        tasks=(task("z"), task("a")),
        relations=(WorkRelationSnapshot.depends_on("z", "a"),),
    )
    second = ProjectReadinessSnapshot(
        tasks=(task("a"), task("z")),
        relations=(WorkRelationSnapshot.depends_on("z", "a"),),
    )
    assert evaluate_readiness(first) == evaluate_readiness(second)

    with pytest.raises(ValueError, match="duplicate work_id"):
        ProjectReadinessSnapshot(tasks=(task("same"), task("same")))


def test_process_terminal_and_non_execution_phase_fail_closed():
    terminal = ProjectReadinessSnapshot(
        tasks=(task("work-1"),),
        process=ProcessReadinessSnapshot(phase="TERMINAL", status="COMPLETED"),
    )
    result = evaluate_readiness(terminal)
    assert reasons(result, "work-1") == {
        ReadinessBlockReason.PROCESS_TERMINAL,
        ReadinessBlockReason.PROCESS_NOT_READY,
    }
