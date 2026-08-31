"""Explicit delivery rejection as a separate, current rework authority."""

from dataclasses import dataclass

from sqlalchemy import select

from ..delivery.integration import integration_subject_digest
from ..delivery.repository import (
    INTEGRATION_RUNS,
    PROJECT_DELIVERIES,
    PROJECT_DELIVERY_APPROVALS,
)
from ..product.repository import TEAM_TASKS
from ..project_process.context import ProjectAgentContextBuilder
from ..project_process.repository import PROJECT_PROCESS_EVENTS
from ..security import Classification, ResourceLabel
from ..verification.project_evidence import _current_evidence
from ..verification.repository import TASK_VERIFICATIONS
from ..verification.service import _time


@dataclass(frozen=True)
class DeliveryReworkEvidence:
    delivery_id: str
    reason: str


def load_delivery_rework(connection, *, process, graph, task_id):
    if process.phase.value != "EXECUTION" or graph.project_id != process.project_id:
        return None
    node = next((node for node in graph.nodes if node.node_type.value == "task" and node.subject_id == task_id), None)
    task = connection.execute(select(TEAM_TASKS).where(TEAM_TASKS.c.task_id == task_id,
        TEAM_TASKS.c.process_id == process.process_id).with_for_update()).mappings().one_or_none()
    if node is None or task is None or task["status"] != "changes_requested":
        return None
    delivery = connection.execute(select(PROJECT_DELIVERIES).where(
        PROJECT_DELIVERIES.c.process_id == process.process_id,
        PROJECT_DELIVERIES.c.project_id == process.project_id, PROJECT_DELIVERIES.c.status == "REJECTED"
    ).order_by(PROJECT_DELIVERIES.c.created_at.desc()).limit(1)).mappings().one_or_none()
    if (delivery is None or delivery["decided_at"] is None
            or _time(task["updated_at"]) != _time(delivery["decided_at"])):
        return None
    event = connection.execute(select(PROJECT_PROCESS_EVENTS).where(
        PROJECT_PROCESS_EVENTS.c.event_id == delivery["delivery_id"] + ":rejected",
        PROJECT_PROCESS_EVENTS.c.process_id == process.process_id,
        PROJECT_PROCESS_EVENTS.c.event_type == "project.delivery.rejected",
        PROJECT_PROCESS_EVENTS.c.subject_id == delivery["delivery_id"],
        PROJECT_PROCESS_EVENTS.c.transition_key == "delivery.rejected")).mappings().one_or_none()
    approval = connection.execute(select(PROJECT_DELIVERY_APPROVALS).where(
        PROJECT_DELIVERY_APPROVALS.c.delivery_id == delivery["delivery_id"],
        PROJECT_DELIVERY_APPROVALS.c.actor_id == delivery["decided_by"],
        PROJECT_DELIVERY_APPROVALS.c.decision == "REJECT",
        PROJECT_DELIVERY_APPROVALS.c.decision_digest == delivery["decision_digest"])).mappings().one_or_none()
    if (event is None or approval is None
            or event["payload_json"].get("delivery_id") != delivery["delivery_id"]
            or task_id not in event["payload_json"].get("impacted_task_ids", [])
            or approval["project_id"] != process.project_id or approval["process_id"] != process.process_id
            or approval["contract_id"] != delivery["acceptance_requirements_json"].get("contract_id")
            or approval["contract_version"] != delivery["acceptance_requirements_json"].get("contract_version")
            or approval["expected_delivery_version"] + 1 != delivery["version"]
            or approval["expected_process_version"] != event["process_version_before"]
            or approval["reason"] != delivery["decision_reason"]):
        return None
    integration = connection.execute(select(INTEGRATION_RUNS).where(
        INTEGRATION_RUNS.c.integration_id == delivery["integration_id"],
        INTEGRATION_RUNS.c.process_id == process.process_id,
        INTEGRATION_RUNS.c.project_id == process.project_id)).mappings().one_or_none()
    if (integration is None or integration["status"] != "PASS"
            or integration["subject_digest"] != integration_subject_digest(integration)
            or integration["graph_digest"] != delivery["graph_digest"]
            or integration["verification_refs_json"] != delivery["verification_refs_json"]):
        return None
    refs = [ref for ref in delivery["verification_refs_json"] if ref["task_id"] == task_id]
    if len(refs) != 1:
        return None
    verification = connection.execute(select(TASK_VERIFICATIONS).where(
        TASK_VERIFICATIONS.c.verification_id == refs[0]["verification_id"])).mappings().one_or_none()
    if verification is None or verification["status"] != "PASS":
        return None
    current = _current_evidence(connection, process=process, node=node,
        task={**task, "status": "verified", "updated_at": verification["updated_at"]})
    if current is None or any(current[key] != refs[0][key] for key in
            ("verification_id", "source_run_id", "contract_version", "subject_digest")):
        return None
    return DeliveryReworkEvidence(delivery["delivery_id"], approval["reason"])


def delivery_rework_feedback(*, task, evidence):
    identity = "delivery-rework:" + evidence.delivery_id
    return ProjectAgentContextBuilder._item(item_id=identity, source_id=identity,
        payload={"schema": "coifesp.task-delivery-rework.v1", "task_id": task.task_id,
            "delivery_id": evidence.delivery_id, "code": "delivery_rejected",
            "public_reason": evidence.reason, "required_change": "Address the delivery rejection within the accepted task contract; publish and submit new verified output."},
        label=ResourceLabel(task.target_team_id, Classification.INTERNAL,
            frozenset({f"project:{task.project_id}"}), identity), priority=95)
