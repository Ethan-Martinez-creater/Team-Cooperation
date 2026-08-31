"""Validate integration FAIL independently from immutable task PASS evidence."""

from dataclasses import dataclass

from sqlalchemy import select

from ..delivery.integration import integration_subject_digest
from ..delivery.repository import INTEGRATION_RUNS
from ..product.repository import TEAM_TASKS
from ..project_process.context import ProjectAgentContextBuilder
from ..project_process.repository import PROJECT_PROCESS_EVENTS
from ..security import Classification, ResourceLabel
from ..verification.project_evidence import _current_evidence
from ..verification.repository import TASK_VERIFICATIONS
from ..verification.service import _time
from ..work_graph.models import WorkNodeType

_GUIDANCE = {
    "artifact_integrity_failed": "Regenerate the affected task output and publish fresh immutable bytes; return the new shared resource IDs.",
    "bundle_invalid": "Check output names and MIME types against the accepted contract, then publish a corrected artifact set.",
    "empty_artifact_set": "Produce and publish the deliverable files required by the task contract; a text-only completion claim is insufficient.",
    "bundle_limit_exceeded": "Reduce output file count or size within the accepted contract; if the contract needs changing, ask the project owner instead of silently dropping requirements.",
}


@dataclass(frozen=True, slots=True)
class IntegrationReworkEvidence:
    integration_id: str
    task_id: str
    verification_id: str
    codes: tuple[str, ...]


def load_integration_rework(connection, *, process, graph, task_id):
    if process.phase.value != "EXECUTION" or graph.project_id != process.project_id:
        return None
    node = next((node for node in graph.nodes if node.node_type is WorkNodeType.TASK
                 and node.subject_id == task_id), None)
    if node is None:
        return None
    task = connection.execute(select(TEAM_TASKS).where(TEAM_TASKS.c.task_id == task_id,
        TEAM_TASKS.c.project_id == process.project_id).with_for_update()).mappings().one_or_none()
    if task is None or task["status"] != "changes_requested" or task["process_id"] != process.process_id:
        return None
    row = connection.execute(select(INTEGRATION_RUNS).where(
        INTEGRATION_RUNS.c.process_id == process.process_id,
        INTEGRATION_RUNS.c.project_id == process.project_id,
        INTEGRATION_RUNS.c.status == "FAIL",
    ).order_by(INTEGRATION_RUNS.c.version.desc()).limit(1)).mappings().one_or_none()
    if (row is None or task_id not in row["impacted_work_ids_json"]
            or row["initiated_by"] != "service:project-orchestrator"
            or row["executed_as"] != "service:project-integrator"
            or row["completed_at"] is None or _time(task["updated_at"]) != _time(row["completed_at"])
            or row["subject_digest"] != integration_subject_digest(row)):
        return None
    event = connection.execute(select(PROJECT_PROCESS_EVENTS).where(
        PROJECT_PROCESS_EVENTS.c.process_id == process.process_id,
        PROJECT_PROCESS_EVENTS.c.subject_id == row["integration_id"],
        PROJECT_PROCESS_EVENTS.c.event_type == "project.integration.completed",
        PROJECT_PROCESS_EVENTS.c.transition_key == "integration.failed",
    )).mappings().one_or_none()
    if event is None or event["payload_json"].get("subject_digest") != row["subject_digest"]:
        return None
    refs = [ref for ref in row["verification_refs_json"] if ref.get("task_id") == task_id]
    if len(refs) != 1:
        return None
    ref = refs[0]
    verification = connection.execute(select(TASK_VERIFICATIONS).where(
        TASK_VERIFICATIONS.c.verification_id == ref["verification_id"],
    )).mappings().one_or_none()
    if (verification is None or verification["status"] != "PASS"
            or any(verification[key] != ref[key] for key in
                ("task_id", "source_run_id", "contract_version", "subject_digest"))):
        return None
    # Validate the historical PASS against the still-latest Run/contract/receipt.
    # Only this local view restores its old lifecycle fields, never persistence.
    original = {**task, "status": "verified", "updated_at": verification["updated_at"]}
    current = _current_evidence(connection, process=process, node=node, task=original)
    if current is None or current["verification_id"] != ref["verification_id"]:
        return None
    artifact_ids = {item["resource_id"] for item in verification["artifacts_json"]}
    codes = tuple(sorted({check["code"] for check in row["checks_json"]
        if check.get("status") == "FAIL" and check.get("type") == "artifact_composition"
        and check.get("code") in _GUIDANCE
        and (check["code"] == "empty_artifact_set"
             or artifact_ids.intersection(check.get("resource_ids", [])))}))
    if not codes:
        return None
    return IntegrationReworkEvidence(row["integration_id"], task_id, ref["verification_id"], codes)


def integration_rework_feedback(*, task, evidence):
    identity = f"integration-rework:{evidence.integration_id}"
    return ProjectAgentContextBuilder._item(item_id=identity, source_id=identity,
        payload={"schema": "coifesp.task-integration-rework.v1", "task_id": task.task_id,
            "integration_id": evidence.integration_id, "status": "FAIL",
            "findings": [{"code": code, "required_change": _GUIDANCE[code]} for code in evidence.codes]},
        label=ResourceLabel(task.target_team_id, Classification.INTERNAL,
            frozenset({f"project:{task.project_id}"}), identity), priority=95)
