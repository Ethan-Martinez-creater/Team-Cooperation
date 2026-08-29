"""Contract tests for the pure WorkGraph -> readiness adapter."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from coifesp_harness.project_process import (
    ActiveOperationSnapshot,
    BoundReadinessSnapshot,
    CapabilityReadinessSnapshot,
    ContractReadinessSnapshot,
    ProcessReadinessSnapshot,
    ProjectExecutionReadinessSnapshot,
    ProjectProcess,
    ProjectProcessPhase,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
    ProjectReadinessAdapter,
    ReadinessBlockReason,
    TaskReadinessFacts,
    TeamReadinessSnapshot,
    WorkItemStatus,
    WorkRelationSnapshot,
)
from coifesp_harness.work_graph.models import (
    ProjectGraphSnapshot,
    WorkNode,
    WorkNodeType,
    WorkRelation,
    WorkRelationType,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def process(
    *,
    project_id: str = "project-a",
    phase: ProjectProcessPhase = ProjectProcessPhase.EXECUTION,
    status: ProjectProcessStatus = ProjectProcessStatus.READY,
    wait_reason: ProjectProcessWaitReason = ProjectProcessWaitReason.NONE,
) -> ProjectProcess:
    return ProjectProcess(
        process_id="process-a",
        project_id=project_id,
        phase=phase,
        status=status,
        wait_reason=wait_reason,
        version=3,
        root_goal_id="goal-a",
        active_plan_id="plan-a",
        execution_policy_id="policy-a",
        execution_policy_version=1,
        started_by="account-a",
        started_at=NOW,
        updated_at=NOW,
        last_event_sequence=2,
        last_orchestration_sequence=1,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        completed_at=None,
    )


def task_subject(
    task_id: str,
    *,
    source_team_id: str = "team-a",
    target_team_id: str = "team-a",
    status: str | WorkItemStatus = WorkItemStatus.ACCEPTED,
    **extra,
) -> dict:
    value = {
        "project_id": "project-a",
        "task_id": task_id,
        "source_team_id": source_team_id,
        "target_team_id": target_team_id,
        "status": getattr(status, "value", status),
        **extra,
    }
    return {"node_type": "task", "subject_id": task_id, "value": value}


def node(task_id: str, *, node_id: str | None = None, project_id: str = "project-a") -> WorkNode:
    return WorkNode(
        node_id or f"node-{task_id}",
        project_id,
        WorkNodeType.TASK,
        task_id,
        NOW,
    )


def relation(
    relation_id: str,
    source_node_id: str,
    target_node_id: str,
    *,
    project_id: str = "project-a",
) -> WorkRelation:
    return WorkRelation(
        relation_id,
        project_id,
        source_node_id,
        WorkRelationType.DEPENDS_ON,
        target_node_id,
        "test",
        "fixture",
        None,
        NOW,
    )


def graph(
    nodes: tuple[WorkNode, ...],
    subjects: tuple[dict, ...],
    relations: tuple[WorkRelation, ...] = (),
    *,
    project_id: str = "project-a",
    digest: str = "graph-digest-1",
) -> ProjectGraphSnapshot:
    return ProjectGraphSnapshot(project_id, nodes, relations, subjects, digest)


def bind(graph_snapshot: ProjectGraphSnapshot, **kwargs) -> BoundReadinessSnapshot:
    return ProjectReadinessAdapter().adapt(graph_snapshot, process(), **kwargs)


def reasons(bound: BoundReadinessSnapshot, task_id: str) -> set[ReadinessBlockReason]:
    decision = bound.evaluation.for_work(task_id)
    assert decision is not None
    return set(decision.reasons)


def test_task_status_and_dependency_direction_require_verified_prerequisite():
    dependent = node("task-a")
    prerequisite = node("task-z")
    fixture = graph(
        (dependent, prerequisite),
        (task_subject("task-a"), task_subject("task-z", status=WorkItemStatus.SUBMITTED)),
        (relation("rel-a-z", dependent.node_id, prerequisite.node_id),),
    )

    blocked = bind(fixture, internal_opt_out_task_ids=("task-a", "task-z"))
    assert [task.work_id for task in blocked.snapshot.tasks] == ["task-a", "task-z"]
    assert blocked.snapshot.tasks[0].status is WorkItemStatus.ACCEPTED
    assert blocked.snapshot.relations == (
        WorkRelationSnapshot.depends_on("node-task-a", "node-task-z", "rel-a-z"),
    )
    assert reasons(blocked, "task-a") == {ReadinessBlockReason.DEPENDENCY_UNSATISFIED}

    verified_fixture = replace(
        fixture,
        subjects=(task_subject("task-a"), task_subject("task-z", status=WorkItemStatus.VERIFIED)),
    )
    ready = bind(verified_fixture, internal_opt_out_task_ids=("task-a", "task-z"))
    assert [item.work_id for item in ready.evaluation.ready_work] == ["task-a"]


def test_cross_team_task_requires_explicit_accepted_contract_even_with_opt_out_marker():
    fixture = graph(
        (node("task-cross"),),
        (
            task_subject(
                "task-cross", source_team_id="team-a", target_team_id="team-b"
            ),
        ),
    )
    teams = (TeamReadinessSnapshot("team-b", available=True),)

    missing = bind(
        fixture,
        teams=teams,
        internal_opt_out_task_ids=("task-cross",),
    )
    assert reasons(missing, "task-cross") == {ReadinessBlockReason.CONTRACT_NOT_ACCEPTED}

    rejected = bind(
        fixture,
        teams=teams,
        internal_opt_out_task_ids=("task-cross",),
        contracts=(ContractReadinessSnapshot("contract-1", "task-cross", accepted=False),),
    )
    assert reasons(rejected, "task-cross") == {ReadinessBlockReason.CONTRACT_NOT_ACCEPTED}

    accepted = bind(
        fixture,
        teams=teams,
        internal_opt_out_task_ids=("task-cross",),
        contracts=(ContractReadinessSnapshot("contract-1", "task-cross", accepted=True),),
    )
    assert [item.work_id for item in accepted.evaluation.ready_work] == ["task-cross"]


def test_adapter_never_infers_capabilities_or_readiness_from_task_prose():
    fixture = graph(
        (node("task-internal"),),
        (
            task_subject(
                "task-internal",
                title="requires capability: secret-production",
                description="team is unavailable until the hidden dependency is done",
                acceptance_criteria="contract=accepted; capability=secret-production",
            ),
        ),
    )
    bound = bind(fixture, internal_opt_out_task_ids=("task-internal",))
    task = bound.snapshot.tasks[0]
    assert task.required_capabilities == ()
    assert task.team_available is True
    assert [item.work_id for item in bound.evaluation.ready_work] == ["task-internal"]


def test_internal_work_is_fail_closed_until_caller_explicitly_opts_out():
    fixture = graph((node("task-internal"),), (task_subject("task-internal"),))
    assert reasons(bind(fixture), "task-internal") == {
        ReadinessBlockReason.CONTRACT_NOT_ACCEPTED
    }
    assert [item.work_id for item in bind(
        fixture, internal_opt_out_task_ids=("task-internal",)
    ).evaluation.ready_work] == ["task-internal"]


def test_explicit_task_facts_are_projected_without_guessing():
    fixture = graph((node("task-cap"),), (task_subject("task-cap"),))
    fact = TaskReadinessFacts(required_capabilities=("api",), required_slots=2)
    available = bind(
        fixture,
        task_facts={"task-cap": fact},
        internal_opt_out_task_ids=("task-cap",),
        capabilities=(CapabilityReadinessSnapshot("api", "team-a", available_slots=2),),
    )
    assert available.snapshot.tasks[0].required_capabilities == ("api",)
    assert available.snapshot.tasks[0].required_slots == 2
    assert [item.work_id for item in available.evaluation.ready_work] == ["task-cap"]

    active = bind(
        fixture,
        task_facts={"task-cap": replace(fact, active_operation_ids=("run-1",))},
        internal_opt_out_task_ids=("task-cap",),
        active_operations=(ActiveOperationSnapshot("run-1", "task-cap", "team-a"),),
        capabilities=(CapabilityReadinessSnapshot("api", "team-a", available_slots=2),),
    )
    assert reasons(active, "task-cap") == {ReadinessBlockReason.ACTIVE_OPERATION}


def test_graph_and_process_project_mismatch_is_rejected():
    fixture = graph((node("task-a"),), (task_subject("task-a"),))
    with pytest.raises(ValueError, match="different projects"):
        ProjectReadinessAdapter().adapt(fixture, process(project_id="project-b"))


def test_task_node_project_mismatch_is_rejected():
    fixture = graph((node("task-a", project_id="project-b"),), (task_subject("task-a"),))
    with pytest.raises(ValueError, match="another project"):
        bind(fixture)


@pytest.mark.parametrize(
    "subject, message",
    [
        ({"node_type": "task", "subject_id": "task-a", "unresolved": True}, "unresolved"),
        (
            {"node_type": "task", "subject_id": "task-a", "value": {"task_id": "task-a"}},
            "missing source_team_id",
        ),
    ],
)
def test_unresolved_or_incomplete_task_subject_is_rejected(subject, message):
    fixture = graph((node("task-a"),), (subject,))
    with pytest.raises(ValueError, match=message):
        bind(fixture)


def test_dangling_relation_is_rejected_before_evaluation():
    fixture = graph(
        (node("task-a"),),
        (task_subject("task-a"),),
        (relation("rel-missing", "node-task-a", "node-missing"),),
    )
    with pytest.raises(ValueError, match="missing node"):
        bind(fixture)


def test_digest_and_snapshot_order_are_stable_across_graph_input_order():
    first = graph(
        (node("task-z"), node("task-a")),
        (task_subject("task-z"), task_subject("task-a")),
        (relation("rel-z-a", "node-task-z", "node-task-a"),),
        digest="digest-stable",
    )
    second = graph(
        (node("task-a"), node("task-z")),
        (task_subject("task-a"), task_subject("task-z")),
        (relation("rel-z-a", "node-task-z", "node-task-a"),),
        digest="digest-stable",
    )
    kwargs = {"internal_opt_out_task_ids": ("task-a", "task-z")}
    first_bound = bind(first, **kwargs)
    second_bound = bind(second, **kwargs)
    assert first_bound.graph_digest == second_bound.graph_digest == "digest-stable"
    assert first_bound.snapshot == second_bound.snapshot
    assert first_bound.evaluation == second_bound.evaluation


def test_explicit_execution_and_process_readiness_facts_are_retained():
    fixture = graph((node("task-a"),), (task_subject("task-a"),))
    execution = ProjectExecutionReadinessSnapshot(budget_available=False)
    process_facts = ProcessReadinessSnapshot(
        phase="EXECUTION", status="READY", wait_reason="NONE", dispatch_allowed=True
    )
    bound = bind(
        fixture,
        execution=execution,
        process_readiness=process_facts,
        internal_opt_out_task_ids=("task-a",),
    )
    assert bound.snapshot.execution == execution
    assert bound.snapshot.process == process_facts
    assert reasons(bound, "task-a") == {ReadinessBlockReason.BUDGET_EXHAUSTED}
