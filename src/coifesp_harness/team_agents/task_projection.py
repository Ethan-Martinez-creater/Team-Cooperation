"""Replayable Team Agent output -> real artifacts -> Product task submission.

Run completion is not verification. Receipts live on the existing Run Binding;
task state, receipt and authoritative event commit in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..agent_runs import AgentRunCheckpointCodec, DurableRunStatus
from ..agent_runs.models import TERMINAL_RUN_STATES
from ..agent_runs.repository import AGENT_RUNS
from ..artifacts.repository import ARTIFACT_MANIFESTS
from ..errors import (
    GovernanceConflictError,
    IntegrityError,
    PolicyDenied,
    ResourceNotFound,
)
from ..product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    TEAM_TASKS,
)
from ..product.service import TeamCollaborationService
from ..project_process.repository import PROJECT_PROCESSES
from ..project_process.service import ProjectProcessService
from .identity import ORCHESTRATOR_PRINCIPAL_ID
from .task_result_models import parse_task_result, validate_task_artifact_types

logger = logging.getLogger("coifesp.team_agents.task_projection")


class TeamTaskResultProjection:
    def __init__(self, *, repository, run_repository, artifact_content=None, clock=None):
        if repository.engine is not run_repository.engine:
            raise ValueError("task result projection requires one shared database engine")
        self.repository = repository
        self.runs = run_repository
        self.artifact_content = artifact_content
        self.clock = clock or (lambda: datetime.now(UTC))

    def on_run_terminal(self, run):
        # Callback values are notifications, not authority for identity/status.
        return self.project(run_id=run.run_id)

    def project(self, *, run_id: str) -> bool:
        with self.repository.transaction() as connection:
            binding = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS).where(
                        PROJECT_AGENT_RUNS.c.run_id == run_id,
                        PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if binding is None:
                return False
            connection.execute(
                select(PROJECT_PROCESSES.c.process_id)
                .where(
                    PROJECT_PROCESSES.c.process_id == binding["process_id"],
                )
                .with_for_update()
            ).scalar_one()
            # Re-read after the process lock: a concurrent terminal callback may
            # already have committed the receipt while this transaction waited.
            binding = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS)
                    .where(
                        PROJECT_AGENT_RUNS.c.run_id == run_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if binding["task_result_status"] is not None:
                return True
            runs = self.runs.using_connection(connection)
            run = runs.get(tenant_id=binding["team_id"], run_id=run_id)
            if (
                run.owner_principal_id != binding["executed_as_principal_id"]
                or binding["executed_as_principal_id"] != f"team-agent:{binding['team_id']}"
                or binding["initiated_by_principal_id"] != ORCHESTRATOR_PRINCIPAL_ID
            ):
                raise GovernanceConflictError("task result Run identity differs from its binding")
            if run.status not in TERMINAL_RUN_STATES:
                return False
            task = TeamCollaborationService._task_row(
                connection, binding["project_id"], binding["team_task_id"]
            )
            process = self.repository.process(connection, binding["process_id"])
            if (
                task["target_team_id"] != binding["team_id"]
                or process.project_id != task["project_id"]
            ):
                raise GovernanceConflictError(
                    "task result binding belongs to another project or team"
                )
            latest = connection.execute(
                select(func.max(PROJECT_AGENT_RUNS.c.execution_attempt)).where(
                    PROJECT_AGENT_RUNS.c.process_id == binding["process_id"],
                    PROJECT_AGENT_RUNS.c.team_task_id == binding["team_task_id"],
                    PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                )
            ).scalar_one()
            version = binding["task_contract_version"]
            current = (
                task["status"] == "in_progress"
                and latest == binding["execution_attempt"]
                and process.status.value not in {"COMPLETED", "FAILED", "CANCELLED"}
                and version is not None
                and version == task["source_contract_version"]
                and version == task["accepted_contract_version"]
                and task["process_id"] == binding["process_id"]
                and task["work_node_id"] == binding["work_node_id"]
            )
            result = None
            error_code = None
            if not current:
                status, error_code = "invalid_output", "stale_or_unbound_task_contract"
            elif run.status is DurableRunStatus.FAILED:
                status, error_code = "failed", "agent_run_failed"
            elif run.status is DurableRunStatus.CANCELLED:
                status, error_code = "cancelled", "agent_run_cancelled"
            else:
                try:
                    decoded = AgentRunCheckpointCodec().decode(
                        runs.load_checkpoint(
                            tenant_id=binding["team_id"],
                            run_id=run_id,
                        )
                    )
                    messages = decoded["messages"]
                    assistant = messages[-1] if messages else None
                    if assistant is None or assistant.role != "assistant" or assistant.tool_calls:
                        raise ValueError("completed task run has no final assistant output")
                    result = parse_task_result(
                        assistant.content, output_contract=task["output_contract_json"]
                    )
                    self._validate_artifacts(connection, task, result)
                    status = "submitted"
                except (ValueError, PolicyDenied, ResourceNotFound, IntegrityError):
                    # No raw model output or private artifact identifiers in a
                    # project-wide failure event. Transient DB/store errors are
                    # not swallowed: no receipt commits and replay can retry.
                    status, error_code, result = (
                        "invalid_output",
                        "invalid_task_output",
                        None,
                    )
            now = self.clock()
            receipt = {
                "schema": "coifesp.task-run-result.v1",
                "run_id": run_id,
                "task_id": task["task_id"],
                "contract_version": version,
                "status": status,
                "error_code": error_code,
                "artifact_refs": result["artifact_refs"] if result else [],
                "summary": result["summary"] if result else "",
                "known_limitations": result["known_limitations"] if result else [],
            }
            if current:
                next_status = "submitted" if status == "submitted" else "changes_requested"
                values = {"status": next_status, "updated_at": now}
                if status == "submitted":
                    values["artifact_resource_ids"] = json.dumps(
                        result["artifact_refs"], separators=(",", ":")
                    )
                changed = connection.execute(
                    TEAM_TASKS.update()
                    .where(
                        TEAM_TASKS.c.task_id == task["task_id"],
                        TEAM_TASKS.c.status == "in_progress",
                        TEAM_TASKS.c.source_contract_version == version,
                        TEAM_TASKS.c.accepted_contract_version == version,
                    )
                    .values(**values)
                ).rowcount
                if changed != 1:
                    raise GovernanceConflictError("task changed before result projection")
                event_id = "task-output:" + hashlib.sha256(run_id.encode()).hexdigest()
                # Append event on the same connection: the normal repository
                # listener publishes the transactional outbox/wakeup, if wired.
                bound = self.repository.using_connection(connection)
                ProjectProcessService(bound, clock=self.clock).append_fact(
                    process_id=process.process_id,
                    event_id=event_id,
                    event_type=f"team_task.{next_status}",
                    expected_version=process.version,
                    expected_event_sequence=process.last_event_sequence,
                    subject_type="team_task",
                    subject_id=task["task_id"],
                    initiated_by=binding["initiated_by_principal_id"],
                    executed_as=binding["executed_as_principal_id"],
                    correlation_id=run.correlation_id,
                    source_aggregate_version=version,
                    payload={
                        "task_id": task["task_id"],
                        "run_id": run_id,
                        "work_node_id": binding["work_node_id"],
                        "contract_version": version,
                        "execution_attempt": binding["execution_attempt"],
                        "artifact_resource_ids": receipt["artifact_refs"],
                        "error_code": error_code,
                    },
                )
            changed = connection.execute(
                PROJECT_AGENT_RUNS.update()
                .where(
                    PROJECT_AGENT_RUNS.c.run_id == run_id,
                    PROJECT_AGENT_RUNS.c.task_result_status.is_(None),
                )
                .values(
                    task_result_status=status,
                    task_result_json=receipt,
                    task_result_at=now,
                )
            ).rowcount
            if changed != 1:
                raise GovernanceConflictError("task result receipt changed concurrently")
            return True

    def _validate_artifacts(self, connection, task, result):
        teams = set(
            connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    PROJECT_TEAMS.c.project_id == task["project_id"],
                )
            ).scalars()
        )
        if not {task["source_team_id"], task["target_team_id"]} <= teams:
            raise PolicyDenied("task teams no longer participate in the project")
        if result["artifact_refs"] and self.artifact_content is None:
            raise RuntimeError("task artifact content reader is unavailable")
        media_types = {}
        for resource_id in result["artifact_refs"]:
            resource = (
                connection.execute(
                    select(PROJECT_RESOURCES).where(
                        PROJECT_RESOURCES.c.resource_id == resource_id,
                        PROJECT_RESOURCES.c.project_id == task["project_id"],
                        PROJECT_RESOURCES.c.owner_team_id == task["target_team_id"],
                        PROJECT_RESOURCES.c.propagation.in_(("project_readonly", "portable")),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if resource is None or resource["artifact_owner_team_id"] != task["target_team_id"]:
                raise PolicyDenied("task output must be an owned, already shared project resource")
            manifest = (
                connection.execute(
                    select(ARTIFACT_MANIFESTS).where(
                        ARTIFACT_MANIFESTS.c.owner_tenant_id == resource["artifact_owner_team_id"],
                        ARTIFACT_MANIFESTS.c.artifact_id == resource["artifact_id"],
                        ARTIFACT_MANIFESTS.c.sha256 == resource["artifact_sha256"],
                        ARTIFACT_MANIFESTS.c.media_type == resource["media_type"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if manifest is None:
                raise ValueError("task output artifact manifest does not match shared resource")
            actual, size = hashlib.sha256(), 0
            for chunk in self.artifact_content.open_policy_authorized(
                owner_tenant_id=resource["artifact_owner_team_id"],
                sha256=resource["artifact_sha256"],
                expected_size=manifest["size_bytes"],
            ):
                actual.update(chunk)
                size += len(chunk)
                if size > manifest["size_bytes"]:
                    raise ValueError("task output artifact exceeds its declared size")
            if size != manifest["size_bytes"] or actual.hexdigest() != resource["artifact_sha256"]:
                raise ValueError("task output artifact integrity mismatch")
            media_types[resource_id] = manifest["media_type"]
        validate_task_artifact_types(
            result,
            media_types=media_types,
            output_contract=task["output_contract_json"],
        )

    def replay_pending(self, _run_service=None):
        with self.repository.transaction() as connection:
            run_ids = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS.c.run_id)
                    .join(
                        AGENT_RUNS,
                        (AGENT_RUNS.c.run_id == PROJECT_AGENT_RUNS.c.run_id)
                        & (AGENT_RUNS.c.tenant_id == PROJECT_AGENT_RUNS.c.team_id),
                    )
                    .where(
                        PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                        PROJECT_AGENT_RUNS.c.task_result_status.is_(None),
                        AGENT_RUNS.c.status.in_([status.value for status in TERMINAL_RUN_STATES]),
                    )
                    .order_by(PROJECT_AGENT_RUNS.c.run_id)
                )
                .scalars()
                .all()
            )
        projected = 0
        for run_id in run_ids:
            try:
                projected += bool(self.project(run_id=run_id))
            except Exception as exc:  # noqa: BLE001 - independent receipts must still recover
                logger.warning(
                    "task result replay deferred run_id=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
        return projected
