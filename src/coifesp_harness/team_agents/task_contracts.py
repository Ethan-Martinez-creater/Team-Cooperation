"""Versioned contracts on Product TeamTask and transaction-bound dispatch facts.

This is not another task registry. Human acceptance remains the existing target
team task transition. The loader never manufactures acceptance or readiness.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select, update

from ..artifacts.repository import ARTIFACT_MANIFESTS
from ..context import ContentTrust, ContextSource
from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    TEAM_TASKS,
)
from ..product.service import TeamCollaborationService
from ..project_process.capability_adapter import ProjectCapabilityRequirement
from ..project_process.context import (
    ProjectAgentContextBuilder,
    TeamTaskExecutionContract,
)
from ..project_process.repository import PROJECT_PROCESSES
from ..security import Classification, ResourceLabel
from ..work_graph.repository import PROJECT_WORK_NODES as WORK_NODES
from .dispatcher import TaskDispatchFacts
from .task_contract_models import validate_task_contract


def _spec(row):
    return validate_task_contract(
        requested_capability=row["requested_capability"],
        input_manifest=row["input_manifest_json"],
        output_contract=row["output_contract_json"],
        verification_policy=row["verification_policy_json"],
        autonomy_requirement=row["autonomy_requirement"],
    )


def _bindings(connection, row, *, process_id, work_node_id):
    process_project = connection.execute(
        select(PROJECT_PROCESSES.c.project_id).where(
            PROJECT_PROCESSES.c.process_id == process_id,
        )
    ).scalar_one_or_none()
    node = (
        connection.execute(
            select(WORK_NODES).where(
                WORK_NODES.c.node_id == work_node_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        process_project != row["project_id"]
        or node is None
        or (
            node["project_id"] != row["project_id"]
            or node["node_type"] != "task"
            or node["subject_id"] != row["task_id"]
        )
    ):
        raise GovernanceConflictError(
            "task contract process or work node does not match task"
        )
    teams = set(
        connection.execute(
            select(PROJECT_TEAMS.c.team_id).where(
                PROJECT_TEAMS.c.project_id == row["project_id"],
            )
        ).scalars()
    )
    if not {row["source_team_id"], row["target_team_id"]} <= teams:
        raise GovernanceConflictError(
            "both task teams must still participate in the project"
        )


class TeamTaskContractService:
    def __init__(self, engine):
        self.engine = engine

    def propose(
        self,
        *,
        project_id,
        task_id,
        actor_id,
        expected_version,
        process_id,
        work_node_id,
        requested_capability,
        input_manifest,
        output_contract,
        verification_policy,
        autonomy_requirement,
    ):
        """Attach/revise a proposed contract, never silently amend accepted work."""
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected contract version must be a nonnegative integer")
        spec = validate_task_contract(
            requested_capability=requested_capability,
            input_manifest=input_manifest,
            output_contract=output_contract,
            verification_policy=verification_policy,
            autonomy_requirement=autonomy_requirement,
        )
        with self.engine.begin() as connection:
            actor = TeamCollaborationService._participant(
                connection, project_id, actor_id
            )
            row = TeamCollaborationService._task_row(connection, project_id, task_id)
            if actor["team_id"] != row["source_team_id"]:
                raise PolicyDenied("only the source team may propose a task contract")
            if row["status"] not in {"proposed", "rejected"}:
                raise GovernanceConflictError(
                    "accepted or executing task contracts are immutable"
                )
            if (row["source_contract_version"] or 0) != expected_version:
                raise GovernanceConflictError("task contract version changed")
            _bindings(connection, row, process_id=process_id, work_node_id=work_node_id)
            values = {
                "process_id": process_id,
                "work_node_id": work_node_id,
                "requested_capability": spec["requested_capability"],
                "input_manifest_json": spec["input_manifest"],
                "output_contract_json": spec["output_contract"],
                "verification_policy_json": spec["verification_policy"],
                "autonomy_requirement": spec["autonomy_requirement"],
                "source_contract_version": expected_version + 1,
                "accepted_contract_version": None,
                "status": "proposed",
                "updated_at": datetime.now(UTC),
            }
            result = connection.execute(
                update(TEAM_TASKS)
                .where(
                    TEAM_TASKS.c.task_id == task_id,
                    TEAM_TASKS.c.status == row["status"],
                    TEAM_TASKS.c.source_contract_version.is_(None)
                    if expected_version == 0
                    else TEAM_TASKS.c.source_contract_version == expected_version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise GovernanceConflictError("task contract changed concurrently")
            return self._view({**dict(row), **values})

    def get(self, *, project_id, task_id, actor_id):
        with self.engine.connect() as connection:
            actor = TeamCollaborationService._participant(
                connection, project_id, actor_id
            )
            row = TeamCollaborationService._task_row(connection, project_id, task_id)
            if actor["team_id"] not in {row["source_team_id"], row["target_team_id"]}:
                raise PolicyDenied(
                    "task contracts are visible to their participating teams"
                )
            if row["source_contract_version"] is None:
                raise ResourceNotFound("task has no structured execution contract")
            return self._view(row)

    def results(self, *, project_id, task_id, actor_id):
        # Raw assistant summaries/limitations have not undergone disclosure
        # approval. Only the executing team can read receipts; project-wide
        # submission events contain already-shared resource references only.
        with self.engine.connect() as connection:
            actor = TeamCollaborationService._participant(connection, project_id, actor_id)
            task = TeamCollaborationService._task_row(connection, project_id, task_id)
            if actor["team_id"] != task["target_team_id"]:
                raise PolicyDenied("task execution receipts belong to the executing team")
            return list(connection.execute(select(PROJECT_AGENT_RUNS.c.task_result_json).where(
                PROJECT_AGENT_RUNS.c.project_id == project_id,
                PROJECT_AGENT_RUNS.c.team_task_id == task_id,
                PROJECT_AGENT_RUNS.c.team_id == actor["team_id"],
                PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                PROJECT_AGENT_RUNS.c.task_result_status.is_not(None),
            ).order_by(PROJECT_AGENT_RUNS.c.created_at, PROJECT_AGENT_RUNS.c.run_id)).scalars())

    @staticmethod
    def _view(row):
        return {
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "process_id": row["process_id"],
            "work_node_id": row["work_node_id"],
            "version": row["source_contract_version"],
            "accepted_version": row["accepted_contract_version"],
            **_spec(row),
        }


class PersistentTaskDispatchFactLoader:
    """Read accepted contract + live visibility facts on the dispatch connection.

    Object storage is immutable; its reader verifies content hash. A required
    input without a configured reader fails admission instead of becoming a
    fictitious usable resource. Binary inputs are references, not invented text.
    """

    def __init__(self, *, engine, artifact_content=None, verification_evidence_loader=None):
        self.engine = engine
        self.artifact_content = artifact_content
        self.verification_evidence_loader = verification_evidence_loader

    def __call__(
        self, *, connection, process, task, graph=None, verification_evidence=None,
    ):
        if connection.engine is not self.engine or not connection.in_transaction():
            raise ValueError(
                "task contract loader requires the active dispatch transaction"
            )
        row = TeamCollaborationService._task_row(
            connection, process.project_id, task.task_id
        )
        version = row["source_contract_version"]
        if (
            version is None
            or row["accepted_contract_version"] != version
            or row["status"] not in {"accepted", "changes_requested"}
        ):
            raise GovernanceConflictError(
                "a version-pinned accepted task contract is required"
            )
        if row["status"] == "changes_requested":
            self._require_current_rework_evidence(
                connection,
                process=process,
                graph=graph,
                verification_evidence=verification_evidence,
                task_id=task.task_id,
            )
        if row["process_id"] != process.process_id:
            raise GovernanceConflictError("task contract belongs to another process")
        _bindings(
            connection,
            row,
            process_id=process.process_id,
            work_node_id=row["work_node_id"],
        )
        spec = _spec(row)
        for node_id in spec["input_manifest"]["work_nodes"]:
            node = connection.execute(
                select(WORK_NODES.c.project_id).where(
                    WORK_NODES.c.node_id == node_id,
                )
            ).scalar_one_or_none()
            if node != process.project_id:
                raise GovernanceConflictError(
                    "contract input work node is unavailable in this project"
                )
        resources, inputs = [], []
        for entry in spec["input_manifest"]["resources"]:
            try:
                resource, item = self._input(connection, row, entry)
            except (ResourceNotFound, PolicyDenied, GovernanceConflictError):
                if entry["required"]:
                    raise
                continue
            resources.append(resource)
            inputs.append(item)
        capability = spec["requested_capability"]
        contract_id = (
            "contract:"
            + hashlib.sha256(f"{task.task_id}:{version}".encode()).hexdigest()
        )
        # Only accessible entries are projected. Other tasks' raw manifests are
        # also omitted by ProjectAgentContextBuilder's graph projection.
        safe_manifest = {
            "resources": resources,
            "work_nodes": spec["input_manifest"]["work_nodes"],
        }
        details = ProjectAgentContextBuilder._item(
            item_id=f"details:{contract_id}",
            source_id=contract_id,
            payload={
                "schema": "coifesp.team-task-contract.v1",
                "version": version,
                "input_manifest": safe_manifest,
                "output_contract": spec["output_contract"],
                "verification_policy": spec["verification_policy"],
                "autonomy_requirement": spec["autonomy_requirement"],
            },
            label=ResourceLabel(
                task.target_team_id,
                Classification.INTERNAL,
                frozenset({f"project:{process.project_id}"}),
                contract_id,
            ),
            priority=99,
        )
        return TaskDispatchFacts(
            contract=TeamTaskExecutionContract(
                contract_id,
                task.task_id,
                process.project_id,
                task.target_team_id,
                tuple(capability["tags"]),
                capability["input_contract_ref"],
                capability["output_contract_ref"],
                capability["verification_policy_ref"],
                tuple(item["resource_id"] for item in resources),
            ),
            requirement=ProjectCapabilityRequirement(
                process.project_id,
                row["source_team_id"],
                row["target_team_id"],
                tuple(capability["tags"]),
                capability["protocol"],
                Classification(capability["input_classification"]),
                tuple(capability["compartments"]),
                tuple(capability["residency"]),
                capability["slots"],
            ),
            contract_accepted=True,
            shared_items=(details, *inputs),
        )

    def _require_current_rework_evidence(
        self, connection, *, process, graph, verification_evidence, task_id,
    ) -> None:
        """Keep direct loader use fail-closed for ``changes_requested`` tasks."""
        if graph is None:
            from ..work_graph.repository import SQLAlchemyWorkGraphRepository

            graph = SQLAlchemyWorkGraphRepository(self.engine).snapshot(
                connection, project_id=process.project_id,
            )
        if verification_evidence is None:
            loader = self.verification_evidence_loader
            if loader is None:
                from ..verification.project_evidence import (
                    load_project_verification_evidence,
                )

                loader = load_project_verification_evidence
            verification_evidence = loader(
                connection, process=process, graph=graph,
            )
        if (
            verification_evidence is None
            or verification_evidence.graph_digest != graph.digest
            or getattr(verification_evidence.outcome, "value", verification_evidence.outcome)
            != "FAILED"
            or task_id not in verification_evidence.failed_task_ids
        ):
            from .integration_rework import load_integration_rework

            if load_integration_rework(connection, process=process, graph=graph, task_id=task_id) is not None:
                return
            raise GovernanceConflictError(
                "changes_requested task lacks current verification FAIL evidence"
            )

    def _input(self, connection, task, entry):
        resource = (
            connection.execute(
                select(PROJECT_RESOURCES).where(
                    PROJECT_RESOURCES.c.resource_id == entry["resource_id"],
                    PROJECT_RESOURCES.c.project_id == task["project_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if resource is None:
            raise ResourceNotFound("task input resource is unavailable")
        if resource["propagation"] != entry["mode"] or (
            resource["propagation"] == "team_private"
            and resource["owner_team_id"] != task["target_team_id"]
        ):
            raise PolicyDenied(
                "task input resource is not readable by the executing team"
            )
        if self.artifact_content is None:
            raise GovernanceConflictError("task input content reader is unavailable")
        manifest = (
            connection.execute(
                select(ARTIFACT_MANIFESTS).where(
                    ARTIFACT_MANIFESTS.c.owner_tenant_id
                    == resource["artifact_owner_team_id"],
                    ARTIFACT_MANIFESTS.c.artifact_id == resource["artifact_id"],
                    ARTIFACT_MANIFESTS.c.sha256 == resource["artifact_sha256"],
                    ARTIFACT_MANIFESTS.c.media_type == resource["media_type"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if manifest is None:
            raise GovernanceConflictError(
                "task input artifact manifest does not match resource"
            )
        if (
            manifest["classification"]
            > task["requested_capability"]["input_classification"]
            or resource["artifact_owner_team_id"] != resource["owner_team_id"]
        ):
            raise PolicyDenied("task input exceeds its declared capability contract")
        if manifest["classification"] > Classification.INTERNAL or not set(
            manifest["compartments"]
        ) <= {f"project:{task['project_id']}"}:
            raise PolicyDenied("task input exceeds the Team Agent project scope")
        # Bound inline data, not arbitrary local paths or an unbounded download.
        if manifest["size_bytes"] > 512_000:
            raise GovernanceConflictError("task input exceeds inline execution limit")
        raw = b"".join(
            self.artifact_content.open_policy_authorized(
                owner_tenant_id=resource["artifact_owner_team_id"],
                sha256=resource["artifact_sha256"],
                expected_size=manifest["size_bytes"],
            )
        )
        if (
            len(raw) != manifest["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != resource["artifact_sha256"]
        ):
            raise GovernanceConflictError(
                "task input artifact content integrity mismatch"
            )
        payload = {
            key: resource[key]
            for key in (
                "resource_id",
                "artifact_owner_team_id",
                "artifact_id",
                "artifact_sha256",
                "media_type",
            )
        }
        try:
            payload["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            payload["representation"] = "binary_artifact_reference"
        token = hashlib.sha256(entry["resource_id"].encode()).hexdigest()
        item = ProjectAgentContextBuilder._item(
            item_id=f"task-input:{token}",
            source_id=f"resource:{token}",
            payload=payload,
            label=ResourceLabel(
                task["target_team_id"],
                Classification.INTERNAL,
                frozenset({f"project:{task['project_id']}"}),
                entry["resource_id"],
            ),
            priority=80,
        )
        # Content integrity verifies identity, not truth or instruction authority.
        item = replace(item, source=ContextSource.DOCUMENT, content_trust=ContentTrust.UNTRUSTED)
        return {**entry, "sha256": resource["artifact_sha256"]}, item
