"""Shared freshness predicate for task verification and independent review."""

import json
from datetime import UTC

from sqlalchemy import func, select

from ..product.repository import PROJECT_AGENT_RUNS


def submission_is_current(connection, *, process, binding, task):
    def time(value):
        if value is None:
            return None
        return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()

    latest = connection.execute(select(func.max(PROJECT_AGENT_RUNS.c.execution_attempt)).where(
        PROJECT_AGENT_RUNS.c.process_id == binding["process_id"],
        PROJECT_AGENT_RUNS.c.team_task_id == task["task_id"],
        PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
    )).scalar_one()
    return (
        task["status"] == "submitted"
        and process.project_id == task["project_id"]
        and process.status.value not in {"COMPLETED", "FAILED", "CANCELLED"}
        and task["process_id"] == binding["process_id"]
        and task["work_node_id"] == binding["work_node_id"]
        and task["source_contract_version"] == binding["task_contract_version"]
        and task["accepted_contract_version"] == binding["task_contract_version"]
        and latest == binding["execution_attempt"]
        and time(task["updated_at"]) == time(binding["task_result_at"])
        and json.loads(task["artifact_resource_ids"]) == binding["task_result_json"]["artifact_refs"]
    )
