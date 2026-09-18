"""Evidence-bound retry authority for malformed or failed Team Agent output."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select

from ..product.repository import PROJECT_AGENT_RUNS, PROJECT_RESOURCES, TEAM_TASKS
from ..project_process.context import ProjectAgentContextBuilder
from ..project_process.repository import PROJECT_PROCESS_EVENTS
from ..security import Classification, ResourceLabel
from ..verification.service import _time
from ..work_graph.models import WorkNodeType

_GUIDANCE = {
    "invalid_task_output": (
        "Return exactly one coifesp.task-output.v1 JSON object with valid artifact_refs, "
        "a non-empty summary, and a known_limitations array. Do not add Markdown or prose."
    ),
    "agent_run_failed": (
        "Retry the accepted task within the same contract and publish valid output before "
        "returning the required coifesp.task-output.v1 JSON object."
    ),
    "agent_run_cancelled": (
        "Resume the accepted task from authoritative project context and return the required "
        "coifesp.task-output.v1 JSON object."
    ),
}


@dataclass(frozen=True, slots=True)
class TaskOutputReworkEvidence:
    run_id: str
    execution_attempt: int
    error_code: str
    available_outputs: tuple[tuple[str, str], ...]


def load_task_output_rework(connection, *, process, graph, task_id):
    """Return only the latest receipt that exactly caused the current rework state."""
    if process.phase.value != "EXECUTION" or graph.project_id != process.project_id:
        return None
    node = next(
        (
            item
            for item in graph.nodes
            if item.node_type is WorkNodeType.TASK and item.subject_id == task_id
        ),
        None,
    )
    task = (
        connection.execute(
            select(TEAM_TASKS)
            .where(
                TEAM_TASKS.c.task_id == task_id,
                TEAM_TASKS.c.project_id == process.project_id,
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if (
        node is None
        or task is None
        or task["status"] != "changes_requested"
        or task["process_id"] != process.process_id
    ):
        return None
    binding = (
        connection.execute(
            select(PROJECT_AGENT_RUNS)
            .where(
                PROJECT_AGENT_RUNS.c.process_id == process.process_id,
                PROJECT_AGENT_RUNS.c.project_id == process.project_id,
                PROJECT_AGENT_RUNS.c.team_task_id == task_id,
                PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                PROJECT_AGENT_RUNS.c.task_result_status.in_(
                    ("invalid_output", "failed", "cancelled")
                ),
            )
            .order_by(PROJECT_AGENT_RUNS.c.execution_attempt.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if binding is None or binding["task_result_at"] is None:
        return None
    receipt = binding["task_result_json"]
    error_code = receipt.get("error_code") if isinstance(receipt, dict) else None
    expected_status = {
        "invalid_task_output": "invalid_output",
        "agent_run_failed": "failed",
        "agent_run_cancelled": "cancelled",
    }.get(error_code)
    if (
        expected_status is None
        or binding["task_result_status"] != expected_status
        or receipt.get("schema") != "coifesp.task-run-result.v1"
        or receipt.get("run_id") != binding["run_id"]
        or receipt.get("task_id") != task_id
        or receipt.get("contract_version") != binding["task_contract_version"]
        or binding["task_contract_version"] != task["source_contract_version"]
        or binding["task_contract_version"] != task["accepted_contract_version"]
        or binding["work_node_id"] != node.node_id
        or binding["team_id"] != task["target_team_id"]
        or binding["initiated_by_principal_id"] != "service:project-orchestrator"
        or binding["executed_as_principal_id"]
        != f"team-agent:{task['target_team_id']}"
        or _time(task["updated_at"]) != _time(binding["task_result_at"])
    ):
        return None
    event_id = "task-output:" + hashlib.sha256(binding["run_id"].encode()).hexdigest()
    event = (
        connection.execute(
            select(PROJECT_PROCESS_EVENTS).where(
                PROJECT_PROCESS_EVENTS.c.process_id == process.process_id,
                PROJECT_PROCESS_EVENTS.c.event_id == event_id,
                PROJECT_PROCESS_EVENTS.c.event_type == "team_task.changes_requested",
                PROJECT_PROCESS_EVENTS.c.subject_id == task_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    payload = event["payload_json"] if event is not None else None
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in {
            "task_id": task_id,
            "run_id": binding["run_id"],
            "work_node_id": binding["work_node_id"],
            "contract_version": binding["task_contract_version"],
            "execution_attempt": binding["execution_attempt"],
            "error_code": error_code,
        }.items()
    ):
        return None
    resources = (
        connection.execute(
            select(PROJECT_RESOURCES.c.resource_id, PROJECT_RESOURCES.c.media_type)
            .where(
                PROJECT_RESOURCES.c.project_id == process.project_id,
                PROJECT_RESOURCES.c.owner_team_id == task["target_team_id"],
                PROJECT_RESOURCES.c.source_run_id == binding["run_id"],
                PROJECT_RESOURCES.c.propagation.in_(("project_readonly", "portable")),
            )
            .order_by(PROJECT_RESOURCES.c.created_at, PROJECT_RESOURCES.c.resource_id)
            .limit(32)
        )
        .all()
    )
    return TaskOutputReworkEvidence(
        binding["run_id"],
        binding["execution_attempt"],
        error_code,
        tuple((row.resource_id, row.media_type) for row in resources),
    )


def task_output_rework_feedback(*, task, evidence):
    identity = f"task-output-rework:{evidence.run_id}"
    return ProjectAgentContextBuilder._item(
        item_id=identity,
        source_id=identity,
        payload={
            "schema": "coifesp.task-output-rework.v1",
            "task_id": task.task_id,
            "source_run_id": evidence.run_id,
            "execution_attempt": evidence.execution_attempt,
            "code": evidence.error_code,
            "required_change": _GUIDANCE[evidence.error_code],
            "available_output_resources": [
                {"resource_id": resource_id, "media_type": media_type}
                for resource_id, media_type in evidence.available_outputs
            ],
        },
        label=ResourceLabel(
            task.target_team_id,
            Classification.INTERNAL,
            frozenset({f"project:{task.project_id}"}),
            identity,
        ),
        priority=95,
    )
