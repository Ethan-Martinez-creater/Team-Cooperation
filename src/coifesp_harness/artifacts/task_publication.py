"""Run-bound publication of real task artifacts, without a human impersonation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
from datetime import UTC, datetime

from jsonschema import Draft202012Validator
from sqlalchemy import func, insert, select
from sqlalchemy.exc import OperationalError

from ..agent_runs import AgentRunCheckpointCodec
from ..agent_runs.repository import AGENT_RUNS
from ..errors import GovernanceConflictError, PolicyDenied
from ..product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    TEAM_PROJECT_AGENTS,
    TEAM_TASKS,
)
from ..project_process.repository import PROJECT_PROCESSES
from ..project_process.service import ProjectProcessService
from ..security import Classification, Principal, ResourceLabel, RiskLevel
from ..tool_jobs import (
    PermanentToolError,
    RetryableToolError,
    current_tool_execution_context,
)
from ..tool_jobs.coordinator import ToolBatchCoordinator
from ..tool_jobs.repository import TOOL_JOBS
from .models import ArtifactKind, ArtifactManifest, ArtifactProvenance

TOOL_NAME = "project.publish_artifact"
_MAX_BYTES = 512 * 1024


def task_artifact_manifest():
    from ..tool_catalog import ToolManifest

    return ToolManifest(
        tool_id=TOOL_NAME, version="1",
        description=("Publish bytes produced by this task as an immutable project resource. "
                     "Use team_private for internal drafts. Project sharing is refused if "
                     "the task consumed private inputs; ask the owning team to review/share those outputs."),
        parameters_schema={"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 240},
            "media_type": {"type": "string", "minLength": 3, "maxLength": 256},
            # Even six-byte JSON control-character escapes fit the existing
            # 1 MiB ToolJob payload limit, including metadata overhead.
            "content": {"type": "string", "maxLength": 150_000},
            "encoding": {"type": "string", "enum": ["utf8", "base64"]},
            "propagation": {"type": "string", "enum": ["team_private", "project_readonly"]},
        }, "required": ["title", "media_type", "content", "encoding", "propagation"],
            "additionalProperties": False},
        required_roles=frozenset({"team_agent"}), risk=RiskLevel.LOW,
        executor="tool_worker", timeout_seconds=30, max_output_chars=4000,
    )


class TaskArtifactPublicationService:
    def __init__(self, *, repository, runs, jobs, artifact_content, clock=None):
        if artifact_content is None:
            raise ValueError("artifact content storage is required")
        if any(other is not repository.engine for other in
               (runs.engine, jobs.engine, artifact_content.repository.engine)):
            raise ValueError("task artifact publication requires one database engine")
        self.repository, self.runs, self.jobs = repository, runs, jobs
        self.content = artifact_content
        self.clock = clock or (lambda: datetime.now(UTC))

    def publish(self, *, context, arguments):
        manifest_contract = task_artifact_manifest()
        Draft202012Validator(manifest_contract.parameters_schema).validate(arguments)
        title, media = arguments["title"].strip(), arguments["media_type"]
        if not title or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media):
            raise ValueError("artifact title or media type is invalid")
        try:
            data = (arguments["content"].encode("utf-8") if arguments["encoding"] == "utf8"
                    else base64.b64decode(arguments["content"], validate=True))
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise ValueError("artifact content encoding is invalid") from exc
        if len(data) > _MAX_BYTES:
            raise ValueError("artifact content exceeds the task publication limit")
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(f"{context.tenant_id}:{context.job_id}".encode()).hexdigest()
        resource_id = "task-artifact:" + identity
        propagation = arguments["propagation"]
        with self.repository.transaction() as connection:
            self.runs._set_tenant(connection, context.tenant_id)
            binding = connection.execute(select(PROJECT_AGENT_RUNS).where(
                PROJECT_AGENT_RUNS.c.run_id == context.run_id,
                PROJECT_AGENT_RUNS.c.team_id == context.tenant_id,
                PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
            )).mappings().one_or_none()
            if binding is None:
                raise PolicyDenied("artifact publication requires a task execution binding")
            connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == binding["process_id"]
            ).with_for_update()).scalar_one()
            process = self.repository.process(connection, binding["process_id"])
            task = connection.execute(select(TEAM_TASKS).where(
                TEAM_TASKS.c.task_id == binding["team_task_id"],
                TEAM_TASKS.c.project_id == process.project_id,
            ).with_for_update()).mappings().one_or_none()
            principal_id = f"team-agent:{context.tenant_id}"
            if (task is None or process.project_id != binding["project_id"]
                    or process.phase.value != "EXECUTION"
                    or process.status.value not in {"READY", "RUNNING"}
                    or task["status"] != "in_progress"
                    or task["target_team_id"] != context.tenant_id
                    or task["process_id"] != process.process_id
                    or task["work_node_id"] != binding["work_node_id"]
                    or task["source_contract_version"] != binding["task_contract_version"]
                    or task["accepted_contract_version"] != binding["task_contract_version"]
                    or binding["task_contract_version"] is None
                    or binding["task_result_status"] is not None
                    or binding["executed_as_principal_id"] != principal_id
                    or binding["initiated_by_principal_id"] != "service:project-orchestrator"):
                raise PolicyDenied("artifact source is not the current accepted task execution")
            run_row = connection.execute(select(AGENT_RUNS).where(
                AGENT_RUNS.c.tenant_id == context.tenant_id,
                AGENT_RUNS.c.run_id == context.run_id,
            ).with_for_update()).mappings().one_or_none()
            if (run_row is None or run_row["owner_principal_id"] != principal_id
                    or run_row["status"] != "awaiting_tool"):
                raise PolicyDenied("task run is not awaiting this tool")
            raw_checkpoint = self.runs._decrypt(run_row)
            checkpoint = AgentRunCheckpointCodec().decode(raw_checkpoint)
            if (context.call_id, TOOL_NAME, arguments) not in ToolBatchCoordinator._pending_calls(raw_checkpoint):
                raise PolicyDenied("artifact publication is not a pending call of this run")
            authorization = checkpoint["tool_authorization"]
            if authorization is None or not any(
                tool.tool_id == TOOL_NAME and tool.version == manifest_contract.version
                and tool.schema_digest == manifest_contract.schema_digest for tool in authorization.tools
            ):
                raise PolicyDenied("task run did not authorize artifact publication")
            self._job_fence(connection, context)
            job = self.jobs.get(tenant_id=context.tenant_id, job_id=context.job_id,
                                include_payloads=True, connection=connection)
            if job.arguments != arguments:
                raise PolicyDenied("artifact arguments differ from the authorized tool job")
            members = set(connection.execute(select(PROJECT_TEAMS.c.team_id).where(
                PROJECT_TEAMS.c.project_id == process.project_id).with_for_update()).scalars())
            if not {task["source_team_id"], context.tenant_id}.issubset(members):
                raise PolicyDenied("task teams no longer participate in this project")
            delegated = connection.execute(select(TEAM_PROJECT_AGENTS.c.agent_id).where(
                TEAM_PROJECT_AGENTS.c.agent_id == binding["team_agent_id"],
                TEAM_PROJECT_AGENTS.c.project_id == process.project_id,
                TEAM_PROJECT_AGENTS.c.team_id == context.tenant_id,
                TEAM_PROJECT_AGENTS.c.status == "active",
            ).with_for_update()).scalar_one_or_none()
            newest = connection.execute(select(PROJECT_AGENT_RUNS.c.run_id).where(
                PROJECT_AGENT_RUNS.c.process_id == process.process_id,
                PROJECT_AGENT_RUNS.c.team_task_id == task["task_id"],
                PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
            ).order_by(PROJECT_AGENT_RUNS.c.execution_attempt.desc()).limit(1)).scalar_one()
            if delegated is None or newest != context.run_id:
                raise PolicyDenied("artifact publication delegation is no longer current")
            if propagation == "project_readonly":
                self._assert_shareable_inputs(connection, task, checkpoint)
            output = task["output_contract_json"]
            if "artifact_types" in output and media not in output["artifact_types"]:
                raise PolicyDenied("artifact type is outside the accepted output contract")
            existing = connection.execute(select(PROJECT_RESOURCES).where(
                PROJECT_RESOURCES.c.resource_id == resource_id)).mappings().one_or_none()
            if existing is not None:
                if (existing["source_run_id"] != context.run_id
                        or existing["produced_by_principal_id"] != principal_id
                        or existing["artifact_sha256"] != digest
                        or existing["title"] != title or existing["media_type"] != media
                        or existing["propagation"] != propagation):
                    raise GovernanceConflictError("artifact publication retry changed its request")
                return self._result(existing, duplicate=True)
            count = connection.execute(select(func.count()).select_from(PROJECT_RESOURCES).where(
                PROJECT_RESOURCES.c.source_run_id == context.run_id,
            )).scalar_one()
            if count >= output.get("max_count", 128):
                raise PolicyDenied("task output artifact count is exhausted")
            now = self.clock()
            stored = self.content.store.put(tenant_id=context.tenant_id, chunks=(data,) if data else (),
                expected_sha256=digest, expected_size=len(data), idempotency_key=resource_id)
            principal = Principal(principal_id, context.tenant_id, frozenset({"team_agent"}),
                Classification.INTERNAL, frozenset({f"project:{process.project_id}"}), True)
            manifest = ArtifactManifest(resource_id, ArtifactKind.GENERIC, media, stored.storage_uri,
                digest, len(data), ResourceLabel(context.tenant_id, Classification.INTERNAL,
                    principal.compartments, resource_id),
                ArtifactProvenance(principal_id, context.tenant_id, TOOL_NAME, "1", now),
                frozenset(members if propagation == "project_readonly" else {context.tenant_id}))
            self._job_fence(connection, context)
            self.content.repository._persist_publication(connection, principal=principal,
                idempotency_key=resource_id, manifest=manifest)
            values = {"resource_id": resource_id, "project_id": process.project_id,
                "owner_team_id": context.tenant_id, "created_by": None, "title": title,
                "artifact_owner_team_id": context.tenant_id, "artifact_id": resource_id,
                "artifact_sha256": digest, "media_type": media, "propagation": propagation, "created_at": now,
                "produced_by_principal_id": principal_id, "source_run_id": context.run_id,
                "source_integration_id": None, "process_id": process.process_id}
            connection.execute(insert(PROJECT_RESOURCES).values(**values))
            # Private draft references are not broadcast to other teams.
            if propagation == "project_readonly":
                ProjectProcessService(self.repository.using_connection(connection), clock=self.clock).append_fact(
                    process_id=process.process_id, event_id="published:" + identity,
                    event_type="artifact.published", expected_version=process.version,
                    expected_event_sequence=process.last_event_sequence,
                    subject_type="artifact", subject_id=resource_id,
                    initiated_by=binding["initiated_by_principal_id"], executed_as=principal_id,
                    correlation_id=run_row["correlation_id"], causation_id=context.job_id,
                    payload={"resource_id": resource_id, "run_id": context.run_id,
                             "task_id": task["task_id"], "sha256": digest})
            return self._result(values, duplicate=False)

    def _job_fence(self, connection, context):
        row = connection.execute(select(TOOL_JOBS).where(
            TOOL_JOBS.c.tenant_id == context.tenant_id, TOOL_JOBS.c.job_id == context.job_id,
        ).with_for_update()).mappings().one_or_none()
        if (row is None or not context.worker_id or not context.lease_token
                or row["tool_name"] != TOOL_NAME or row["run_id"] != context.run_id
                or row["call_id"] != context.call_id or row["idempotency_key"] != context.idempotency_key
                or row["status"] != "running" or row["lease_owner"] != context.worker_id
                or row["lease_token"] != context.lease_token or row["lease_expires_at"] is None):
            raise PolicyDenied("artifact publication tool lease is unavailable")
        expiry = row["lease_expires_at"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= self.clock():
            raise PolicyDenied("artifact publication tool lease expired")

    @staticmethod
    def _assert_shareable_inputs(connection, task, checkpoint):
        inputs = task["input_manifest_json"]["resources"]
        for entry in inputs:
            if entry["mode"] == "team_private":
                raise PolicyDenied("private-input outputs require owning-team disclosure review")
            current = connection.execute(select(PROJECT_RESOURCES.c.propagation).where(
                PROJECT_RESOURCES.c.resource_id == entry["resource_id"],
                PROJECT_RESOURCES.c.project_id == task["project_id"],
            ).with_for_update()).scalar_one_or_none()
            if current not in {"project_readonly", "portable"}:
                raise PolicyDenied("task input sharing was withdrawn")
        # This task tool is not a general publication grant for arbitrary chat
        # or memory contexts. Other context sources need an explicit review.
        allowed_inputs = {entry["resource_id"] for entry in inputs}
        for item in checkpoint["context_items"]:
            if item.source.value == "governance" and (
                item.label.owner_tenant_id != task["target_team_id"]
                or item.label.compartments != frozenset({f"project:{task['project_id']}"})
                or item.label.classification > Classification.INTERNAL
            ):
                raise PolicyDenied("governance context is outside the current task scope")
        if any(item.source.value != "governance" and not (
                item.source.value == "document" and item.label.resource_id in allowed_inputs)
               for item in checkpoint["context_items"]):
            raise PolicyDenied("non-task context requires disclosure review")

    @staticmethod
    def _result(row, *, duplicate):
        return {"resource_id": row["resource_id"], "artifact_id": row["artifact_id"],
                "sha256": row["artifact_sha256"], "media_type": row["media_type"],
                "propagation": row["propagation"], "duplicate": duplicate}


class TaskArtifactPublicationTool:
    def __init__(self, service):
        self.service = service

    def definition(self):
        return task_artifact_manifest().declaration(self.publish)

    async def publish(self, arguments):
        context = current_tool_execution_context()
        try:
            return await asyncio.to_thread(self.service.publish, context=context, arguments=arguments)
        except (OSError, OperationalError) as exc:
            raise RetryableToolError("task_artifact_publication_unavailable") from exc
        except (ValueError, PolicyDenied, GovernanceConflictError) as exc:
            raise PermanentToolError("task_artifact_publication_denied") from exc
