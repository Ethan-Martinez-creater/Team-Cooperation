"""Read current task evidence for orchestration; never infer PASS from task counts."""

import json
from dataclasses import dataclass

from sqlalchemy import select

from ..product.repository import PROJECT_AGENT_RUNS, TEAM_TASKS
from ..project_process.orchestrator import VerificationOutcome
from ..team_agents.identity import ORCHESTRATOR_PRINCIPAL_ID
from ..work_graph.models import WorkNodeType
from .repository import TASK_VERIFICATIONS
from .service import TaskVerificationService, _digest, _time


@dataclass(frozen=True, slots=True)
class ProjectVerificationEvidence:
    graph_digest: str
    outcome: VerificationOutcome | None
    passed_task_ids: tuple[str, ...]
    failed_task_ids: tuple[str, ...]
    pending_task_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]


def load_project_verification_evidence(connection, *, process, graph):
    """Use the caller's fenced transaction and authoritative WorkGraph snapshot.

    All graph tasks are required, matching the current readiness contract (no
    optional-task flag exists). No tasks means no evidence, not vacuous success.
    This is an integration prerequisite, not a completion evaluator. Immutable
    historical PASS/FAIL rows remain untouched when their submission is obsolete.
    """
    if graph.project_id != process.project_id:
        raise ValueError("verification graph belongs to another project")
    nodes = sorted((node for node in graph.nodes if node.node_type == WorkNodeType.TASK),
                   key=lambda node: node.subject_id)
    if len({node.subject_id for node in nodes}) != len(nodes):
        raise ValueError("verification graph contains duplicate tasks")
    passed, failed, pending, evidence = [], [], [], []
    for node in nodes:
        if node.project_id != process.project_id:
            raise ValueError("verification node belongs to another project")
        task = connection.execute(select(TEAM_TASKS).where(
            TEAM_TASKS.c.project_id == process.project_id,
            TEAM_TASKS.c.task_id == node.subject_id,
        ).with_for_update()).mappings().one_or_none()
        row = _current_evidence(connection, process=process, node=node, task=task)
        if row is None:
            pending.append(node.subject_id)
        elif row["status"] == "FAIL":
            failed.append(node.subject_id)
            evidence.append(row["verification_id"])
        else:
            passed.append(node.subject_id)
            evidence.append(row["verification_id"])
    outcome = (VerificationOutcome.FAILED if failed else
               VerificationOutcome.PASSED if passed and not pending else None)
    return ProjectVerificationEvidence(graph.digest, outcome, tuple(passed), tuple(failed),
                                       tuple(pending), tuple(evidence))


def _current_evidence(connection, *, process, node, task):
    if (task is None or task["process_id"] != process.process_id
            or task["work_node_id"] != node.node_id
            or task["status"] not in {"verified", "changes_requested"}):
        return None
    binding = connection.execute(select(PROJECT_AGENT_RUNS).where(
        PROJECT_AGENT_RUNS.c.project_id == process.project_id,
        PROJECT_AGENT_RUNS.c.process_id == process.process_id,
        PROJECT_AGENT_RUNS.c.team_task_id == node.subject_id,
        PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
    ).order_by(PROJECT_AGENT_RUNS.c.execution_attempt.desc()).limit(1)).mappings().one_or_none()
    if (binding is None or binding["task_result_status"] != "submitted"
            or binding["work_node_id"] != node.node_id
            or binding["team_id"] != task["target_team_id"]
            or binding["initiated_by_principal_id"] != ORCHESTRATOR_PRINCIPAL_ID
            or binding["executed_as_principal_id"] != f"team-agent:{task['target_team_id']}"
            or task["source_contract_version"] != binding["task_contract_version"]
            or task["accepted_contract_version"] != binding["task_contract_version"]):
        return None
    row = connection.execute(select(TASK_VERIFICATIONS).where(
        TASK_VERIFICATIONS.c.source_run_id == binding["run_id"],
    )).mappings().one_or_none()
    receipt = binding["task_result_json"]
    if (row is None or row["status"] not in {"PASS", "FAIL"}
            or row["project_id"] != process.project_id or row["process_id"] != process.process_id
            or row["task_id"] != node.subject_id
            or row["contract_version"] != binding["task_contract_version"]
            or task["status"] != ("verified" if row["status"] == "PASS" else "changes_requested")
            or _time(task["updated_at"]) != _time(row["updated_at"])
            or type(receipt) is not dict
            or receipt.get("artifact_manifests") != row["artifacts_json"]
            or receipt.get("verification_policy") != row["policy_json"]
            or task["verification_policy_json"] != row["policy_json"]
            or json.loads(task["artifact_resource_ids"]) != receipt.get("artifact_refs")):
        return None
    digest = _digest({"run_id": binding["run_id"],
                      "contract_version": binding["task_contract_version"],
                      "submitted_at": _time(binding["task_result_at"]),
                      "artifact_refs": receipt["artifact_refs"],
                      "artifacts": row["artifacts_json"], "policy": row["policy_json"]})
    if digest != row["subject_digest"]:
        return None
    # A withdrawal invalidates PASS. FAIL remains actionable even when the
    # withdrawal was precisely the failed criterion, without rewriting evidence.
    if row["status"] == "PASS" and not TaskVerificationService._resources_current(
        connection, task, receipt, row["artifacts_json"]
    ):
        return None
    return row
