"""Real SQL, verified TaskRun and byte-store delivery regressions."""

import pytest
from sqlalchemy import func, select
from test_integration_service import execute, prepare

from coifesp_harness.delivery.repository import (
    PROJECT_COMPLETION_EVALUATIONS,
    PROJECT_DELIVERIES,
    PROJECT_DELIVERY_APPROVALS,
)
from coifesp_harness.delivery.service import DeliveryService
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.product.repository import ACCOUNTS, PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.project_process.repository import PROJECT_PROCESSES
from coifesp_harness.verification.repository import TASK_VERIFICATIONS
from coifesp_harness.work_graph.service import ProjectWorkGraphService


def process(value):
    with value.repository.transaction() as connection:
        return value.repository.process(connection, "process-a")


def delivery(value):
    with value.engine.connect() as connection:
        return dict(connection.execute(select(PROJECT_DELIVERIES).order_by(
            PROJECT_DELIVERIES.c.created_at.desc()).limit(1)).mappings().one())


def setup_delivery(tmp_path, *, goal=True, approvers=("lead-a",), before_integration=None):
    value = prepare(tmp_path)
    value.graph_service = ProjectWorkGraphService(value.integration.work_graph)
    if goal:
        value.graph_service.create_goal(goal_id="goal-delivery", project_id="project-a",
            title="Deliver the contracted result", description="A verified shared artifact",
            success_criteria=("Verified output is delivered and accepted",), created_by="lead-a")
    if before_integration:
        before_integration(value)
    with value.repository.transaction() as connection:
        value.arguments["expected_graph_digest"] = value.integration.work_graph.snapshot(
            connection, project_id="project-a").digest
    assert execute(value)["status"] == "PASS"
    value.delivery_service = DeliveryService(repository=value.repository,
        work_graph_repository=value.integration.work_graph, artifact_content=value.content)
    value.contract = contract(value, approvers=approvers)
    return value


def contract(value, *, approvers=("lead-a",), previous=0, key="contract-1"):
    service = value.delivery_service
    row = service.propose_contract(project_id="project-a", process_id="process-a", actor_id="lead-a",
        payload={"expected_process_version": process(value).version, "expected_contract_version": previous,
                 "idempotency_key": key, "required_human_approvers": list(approvers), "root_goal_id": "goal-delivery"})
    return service.approve_contract(project_id="project-a", process_id="process-a", actor_id="lead-a",
        contract_id=row["contract_id"], payload={"expected_process_version": process(value).version,
                                              "idempotency_key": key + "-approve"})


def decide(value, *, actor="lead-a", decision="ACCEPT", payload=None):
    row = delivery(value)
    payload = payload or {"expected_process_version": process(value).version, "expected_version": row["version"],
        "decision": decision, "reason": "Shared project delivery decision", "idempotency_key": actor + "-decision"}
    return value.delivery_service.decide(project_id="project-a", process_id="process-a",
        delivery_id=row["delivery_id"], actor_id=actor, payload=payload)


def counts(value):
    with value.engine.connect() as connection:
        return tuple(connection.execute(select(func.count()).select_from(table)).scalar_one()
                     for table in (PROJECT_DELIVERY_APPROVALS, PROJECT_COMPLETION_EVALUATIONS))


def test_real_delivery_acceptance_completes_once_with_all_fourteen_checks(tmp_path):
    value = setup_delivery(tmp_path)
    row, before = delivery(value), process(value)
    assert row["status"] == "READY" and before.phase.value == "DELIVERY"
    payload = {"expected_process_version": before.version, "expected_version": row["version"],
        "decision": "ACCEPT", "reason": "Delivery checked", "idempotency_key": "accept-1"}
    accepted = decide(value, payload=payload)
    assert accepted["status"] == "ACCEPTED"
    after = process(value)
    assert (after.phase.value, after.status.value) == ("TERMINAL", "COMPLETED")
    assert after.version == before.version + 1
    assert after.last_event_sequence == before.last_event_sequence + 3
    replayed = decide(value, payload=payload)
    assert replayed["status"] == accepted["status"] and replayed["version"] == accepted["version"]
    assert process(value) == after and counts(value) == (1, 1)
    with value.engine.connect() as connection:
        evaluation = connection.execute(select(PROJECT_COMPLETION_EVALUATIONS)).mappings().one()
        assert evaluation["passed"] and len(evaluation["checks_json"]) == 14
        assert all(check["status"] == "PASS" for check in evaluation["checks_json"])
        assert evaluation["contract_id"] == value.contract["contract_id"]
        assert evaluation["delivery_version"] == accepted["version"]


def test_multi_approver_requires_current_version_and_all_people(tmp_path):
    value = setup_delivery(tmp_path, approvers=("lead-a", "lead-b"))
    original = delivery(value)
    partial = decide(value)
    assert partial["status"] == "READY" and partial["version"] == original["version"] + 1
    assert counts(value) == (1, 0) and process(value).phase.value == "DELIVERY"
    with pytest.raises(GovernanceConflictError, match="version is stale"):
        decide(value, actor="lead-b", payload={"expected_process_version": process(value).version,
            "expected_version": original["version"], "decision": "ACCEPT", "reason": "Reviewed",
            "idempotency_key": "lead-b-decision"})
    assert counts(value) == (1, 0)
    assert decide(value, actor="lead-b")["status"] == "ACCEPTED"
    assert counts(value) == (2, 1)


def test_partial_approval_pins_contract_and_disabled_approver_cannot_finish(tmp_path):
    value = setup_delivery(tmp_path, approvers=("lead-a", "lead-b"))
    decide(value)
    with pytest.raises(GovernanceConflictError, match="pins its completion contract"):
        contract(value, approvers=("lead-b",), previous=1, key="contract-2")
    with value.engine.begin() as connection:
        connection.execute(ACCOUNTS.update().where(ACCOUNTS.c.account_id == "lead-a").values(enabled=False))
    with pytest.raises(GovernanceConflictError, match="required_human_approvals_obtained"):
        decide(value, actor="lead-b")
    assert counts(value) == (1, 0) and delivery(value)["status"] == "READY"


def test_unlisted_approver_cannot_accept_or_reject(tmp_path):
    value = setup_delivery(tmp_path)
    for decision in ("ACCEPT", "REJECT"):
        with pytest.raises(PolicyDenied, match="not a required delivery approver"):
            decide(value, actor="lead-b", decision=decision)
    assert counts(value) == (0, 0)


def test_missing_goal_rolls_back_last_approval(tmp_path):
    value = setup_delivery(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_PROCESSES.update().values(root_goal_id=None))
    before = process(value)
    with pytest.raises(GovernanceConflictError, match="goal binding changed"):
        decide(value)
    assert process(value) == before and counts(value) == (0, 0)
    assert delivery(value)["status"] == "READY"


@pytest.mark.parametrize("fault", ["bytes", "withdrawal", "source_withdrawal"])
def test_changed_artifacts_cannot_complete(tmp_path, fault):
    value = setup_delivery(tmp_path)
    if fault == "bytes":
        value.content.open_policy_authorized = lambda **_: iter([b"corrupt"])
    else:
        with value.engine.begin() as connection:
            condition = (PROJECT_RESOURCES.c.source_integration_id.is_(None) if fault == "source_withdrawal"
                         else PROJECT_RESOURCES.c.source_integration_id.is_not(None))
            connection.execute(PROJECT_RESOURCES.update().where(condition).values(propagation="team_private"))
    with pytest.raises(GovernanceConflictError):
        decide(value)
    assert counts(value) == (0, 0) and delivery(value)["status"] == "READY"


@pytest.mark.parametrize("event_type", ["project.delivery.approval_decided", "project.completion.evaluated",
                                       "project.delivery.accepted"])
def test_outbox_failure_rolls_back_acceptance_evaluation_and_transition(tmp_path, event_type):
    value = setup_delivery(tmp_path)
    before = process(value)

    def fail(connection, event):
        if event.event_type == event_type:
            raise OSError("outbox unavailable")

    value.repository.set_event_listener(fail)
    with pytest.raises(OSError, match="outbox unavailable"):
        decide(value)
    assert counts(value) == (0, 0) and process(value) == before
    assert delivery(value)["status"] == "READY"


def test_rejection_reopens_work_but_preserves_historical_pass(tmp_path):
    value = setup_delivery(tmp_path)
    with value.engine.connect() as connection:
        verification = dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one())
    assert decide(value, decision="REJECT")["status"] == "REJECTED"
    assert (process(value).phase.value, process(value).status.value) == ("EXECUTION", "READY")
    assert counts(value) == (1, 0)
    with value.engine.connect() as connection:
        task = connection.execute(select(TEAM_TASKS)).mappings().one()
        assert task["status"] == "changes_requested" and task["completed_at"] is None
        assert dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one()) == verification


@pytest.mark.parametrize("field", ["contract_version", "contract_digest"])
def test_partial_acceptance_rejects_changed_contract_binding(tmp_path, field):
    value = setup_delivery(tmp_path, approvers=("lead-a", "lead-b"))
    decide(value)
    row = delivery(value)
    requirements = dict(row["acceptance_requirements_json"])
    requirements[field] = 99 if field == "contract_version" else "0" * 64
    with value.engine.begin() as connection:
        connection.execute(PROJECT_DELIVERIES.update().values(acceptance_requirements_json=requirements))
    with pytest.raises(GovernanceConflictError, match="contract changed"):
        decide(value, actor="lead-b")
    assert counts(value) == (1, 0)


@pytest.mark.parametrize("fault", ["source_bytes", "media_type", "uncovered_artifact"])
def test_completion_checks_source_bytes_metadata_and_graph_artifact_coverage(tmp_path, fault):
    def add_uncovered(value):
        from coifesp_harness.artifacts.repository import ARTIFACT_MANIFESTS

        with value.engine.begin() as connection:
            resource = dict(connection.execute(select(PROJECT_RESOURCES)).mappings().one())
            resource["resource_id"] = "uncovered-resource"
            resource["artifact_id"] = "artifact-uncovered"
            connection.execute(PROJECT_RESOURCES.insert().values(**resource))
            assert connection.execute(select(func.count()).select_from(ARTIFACT_MANIFESTS)).scalar_one()
        value.graph_service.register_existing_subject(node_id="node:uncovered", project_id="project-a",
            node_type="artifact", subject_id="uncovered-resource")

    value = setup_delivery(tmp_path, before_integration=add_uncovered if fault == "uncovered_artifact" else None)
    if fault == "source_bytes":
        read = value.content.open_policy_authorized
        output_sha = delivery(value)["artifact_refs_json"][0]["sha256"]
        value.content.open_policy_authorized = lambda **kwargs: (
            read(**kwargs) if kwargs["sha256"] == output_sha else iter([b"corrupt original"]))
    elif fault == "media_type":
        with value.engine.begin() as connection:
            connection.execute(PROJECT_RESOURCES.update().where(
                PROJECT_RESOURCES.c.source_integration_id.is_not(None)).values(media_type="text/plain"))
    with pytest.raises(GovernanceConflictError):
        decide(value)
    assert counts(value) == (0, 0)


def add_scope(value, *, covered=True, milestone_status="planned", policy=None):
    _, requirement = value.graph_service.create_requirement(requirement_id="requirement-delivery",
        project_id="project-a", goal_id="goal-delivery", title="Deliver a shared artifact", description="Required",
        requirement_type="functional", priority="high", source_type="human", source_id="lead-a")
    _, milestone = value.graph_service.create_milestone(milestone_id="milestone-delivery", project_id="project-a",
        title="Verified delivery", description="Required", target_at=None,
        completion_policy={"type": "all_tasks_verified"} if policy is None else policy, status=milestone_status)
    with value.repository.transaction() as connection:
        graph = value.integration.work_graph.snapshot(connection, project_id="project-a")
        task = next(node for node in graph.nodes if node.node_type.value == "task")
    for target, relation in ((milestone, "part_of"), (requirement, "implements")):
        if target == requirement and not covered:
            continue
        value.graph_service.add_relation(relation_id="edge:" + relation, project_id="project-a",
            source_node_id=task.node_id, target_node_id=target.node_id, relation_type=relation,
            created_by_type="human", created_by_id="lead-a")


def test_explicit_requirement_coverage_and_policy_derived_milestone_completion(tmp_path):
    value = setup_delivery(tmp_path, before_integration=add_scope)
    assert decide(value)["status"] == "ACCEPTED"


@pytest.mark.parametrize("fault,criterion", [
    ("uncovered", "all_required_requirements_satisfied"),
    ("blocked", "all_required_milestones_completed"),
    ("opaque_policy", "all_required_milestones_completed"),
])
def test_scope_without_deterministic_completion_evidence_fails(tmp_path, fault, criterion):
    value = setup_delivery(tmp_path, before_integration=lambda value: add_scope(value,
        covered=fault != "uncovered", milestone_status="blocked" if fault == "blocked" else "planned",
        policy={"manual_magic": True} if fault == "opaque_policy" else None))
    with pytest.raises(GovernanceConflictError, match=criterion):
        decide(value)
    assert counts(value) == (0, 0)


def test_rejected_delivery_reexecutes_new_attempt_then_completes(tmp_path):
    from types import SimpleNamespace

    from test_integration_orchestration import worker
    from test_team_task_result_projection import finish

    from coifesp_harness.agent_runs import AgentRunCheckpointCodec
    from coifesp_harness.product.repository import PROJECT_AGENT_RUNS
    from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting

    value = setup_delivery(tmp_path)
    rejected = decide(value, decision="REJECT")
    runner = worker(value)
    dispatched = runner.process_once(worker_id="delivery-rework")
    assert dispatched.status.value == "APPLIED", dispatched
    assert dispatched.action.value == "dispatch_work", dispatched
    with value.engine.connect() as connection:
        binding = connection.execute(select(PROJECT_AGENT_RUNS).order_by(
            PROJECT_AGENT_RUNS.c.execution_attempt.desc()).limit(1)).mappings().one()
        assert binding["execution_attempt"] == 2
    run_id = binding["run_id"]
    raw = value.runs.load_checkpoint(tenant_id="team-b", run_id=run_id)
    checkpoint = AgentRunCheckpointCodec().decode(raw)
    feedback = [item.content for item in checkpoint["context_items"] if "task-delivery-rework.v1" in str(item.content)]
    assert feedback and "Shared project delivery decision" in str(feedback)
    value.dispatched = SimpleNamespace(run_id=run_id)
    finish(value)
    TeamTaskRunAccounting(repository=value.repository, run_repository=value.runs,
        capability_repository=value.capabilities).settle(run_id=run_id)
    value.projection.project(run_id=run_id)
    value.verifier.verify_run(run_id=run_id)
    for index, expected in enumerate(("enter_verification", "enter_integration", "assemble_integration")):
        runner.scheduler.enqueue(process_id="process-a", project_id="project-a",
            source_event_id=f"delivery-progress:{index}", source_event_type="team_task.verified", payload={})
        result = runner.process_once(worker_id="delivery-rework")
        assert result.status.value == "APPLIED" and result.action.value == expected, result
    current = delivery(value)
    assert current["delivery_id"] != rejected["delivery_id"]
    assert current["verification_refs_json"][0]["source_run_id"] == run_id
    assert decide(value)["status"] == "ACCEPTED"
    assert process(value).status.value == "COMPLETED" and counts(value) == (2, 1)


def test_contract_binds_selected_goal_through_service_and_proposal_retry_pins_cursor(tmp_path):
    value = setup_delivery(tmp_path)
    # Emulate a legacy process whose goal existed before root binding was introduced.
    with value.engine.begin() as connection:
        connection.execute(PROJECT_PROCESSES.update().values(root_goal_id=None))
    payload = {"expected_process_version": process(value).version, "expected_contract_version": 1,
        "root_goal_id": "goal-delivery", "required_human_approvers": ["lead-a"], "idempotency_key": "bound-goal"}
    args = {"project_id": "project-a", "process_id": "process-a", "actor_id": "lead-a"}
    proposal = value.delivery_service.propose_contract(**args, payload=payload)
    assert process(value).root_goal_id is None
    retry = value.delivery_service.propose_contract(**args, payload=payload)
    assert retry["contract_id"] == proposal["contract_id"]
    with pytest.raises(GovernanceConflictError, match="idempotency key"):
        value.delivery_service.propose_contract(**args,
            payload={**payload, "expected_process_version": payload["expected_process_version"] + 1})
    value.delivery_service.approve_contract(**args, contract_id=proposal["contract_id"],
        payload={"expected_process_version": process(value).version, "idempotency_key": "approve-bound-goal"})
    assert process(value).root_goal_id == "goal-delivery"
    assert decide(value)["status"] == "ACCEPTED"


def test_contract_cannot_disable_required_checks_or_select_foreign_goal(tmp_path):
    from coifesp_harness.delivery.completion import default_completion_criteria

    value = setup_delivery(tmp_path)
    payload = {"expected_process_version": process(value).version, "expected_contract_version": 1,
        "required_human_approvers": ["lead-a"], "idempotency_key": "invalid-contract"}
    criteria = default_completion_criteria()
    criteria["integration_passed"] = False
    with pytest.raises(ValueError, match="literal True"):
        value.delivery_service.propose_contract(project_id="project-a", process_id="process-a", actor_id="lead-a",
            payload={**payload, "criteria": criteria})
    with pytest.raises(GovernanceConflictError, match="different root goal"):
        value.delivery_service.propose_contract(project_id="project-a", process_id="process-a", actor_id="lead-a",
            payload={**payload, "root_goal_id": "foreign-goal"})


@pytest.mark.parametrize("fault", ["event_subject", "event_payload", "contract_version", "process_version",
                                  "delivery_version", "reason", "integration_refs", "task_stamp"])
def test_rework_requires_bound_rejection_approval_and_current_task_evidence(tmp_path, fault):
    from datetime import timedelta

    from coifesp_harness.delivery.repository import INTEGRATION_RUNS
    from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
    from coifesp_harness.team_agents.delivery_rework import load_delivery_rework

    value = setup_delivery(tmp_path)
    rejected = decide(value, decision="REJECT")

    def evidence():
        with value.repository.transaction() as connection:
            return load_delivery_rework(connection, process=value.repository.process(connection, "process-a"),
                graph=value.integration.work_graph.snapshot(connection, project_id="project-a"), task_id="task-a")

    assert evidence() is not None
    with value.engine.begin() as connection:
        if fault == "event_subject":
            connection.execute(PROJECT_PROCESS_EVENTS.update().where(
                PROJECT_PROCESS_EVENTS.c.event_id == rejected["delivery_id"] + ":rejected").values(subject_id="other"))
        elif fault == "event_payload":
            connection.execute(PROJECT_PROCESS_EVENTS.update().where(
                PROJECT_PROCESS_EVENTS.c.event_id == rejected["delivery_id"] + ":rejected").values(
                    payload_json={"delivery_id": "other", "impacted_task_ids": ["task-a"]}))
        elif fault == "integration_refs":
            connection.execute(INTEGRATION_RUNS.update().values(verification_refs_json=[]))
        elif fault == "task_stamp":
            row = connection.execute(select(TEAM_TASKS)).mappings().one()
            connection.execute(TEAM_TASKS.update().values(updated_at=row["updated_at"] + timedelta(seconds=1)))
        else:
            key = {"process_version": "expected_process_version", "delivery_version": "expected_delivery_version"}.get(fault, fault)
            connection.execute(PROJECT_DELIVERY_APPROVALS.update().values(**{key: "changed" if fault == "reason" else 99}))
    assert evidence() is None
