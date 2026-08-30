"""Structured project work context for automatically dispatched Team Agents."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..context import ContentTrust, ContextItem, ContextSource, InstructionTrust
from ..errors import GovernanceConflictError
from ..product import TeamProjectAgent, TeamProjectAgentStatus, TeamTask, TeamTaskStatus
from ..security import Classification, ResourceLabel
from ..work_graph import ProjectGraphSnapshot, WorkNodeType
from .models import ProjectProcess


@dataclass(frozen=True, slots=True)
class TeamTaskExecutionContract:
    contract_id: str
    task_id: str
    project_id: str
    target_team_id: str
    required_capability_tags: tuple[str, ...]
    input_contract_ref: str
    output_contract_ref: str
    verification_policy_ref: str
    input_resource_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.contract_id,
            self.task_id,
            self.project_id,
            self.target_team_id,
            self.input_contract_ref,
            self.output_contract_ref,
            self.verification_policy_ref,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("team task execution contract fields are required")
        if (
            not self.required_capability_tags
            or len(self.required_capability_tags)
            != len(set(self.required_capability_tags))
        ):
            raise ValueError("required capability tags must be unique and non-empty")
        if len(self.input_resource_ids) != len(set(self.input_resource_ids)):
            raise ValueError("input resource ids must be unique")


class ProjectAgentContextBuilder:
    """Build prioritized data-only items; ContextAssembler remains the policy gate."""

    def build(
        self,
        *,
        process: ProjectProcess,
        team_agent: TeamProjectAgent,
        task: TeamTask,
        contract: TeamTaskExecutionContract,
        graph: ProjectGraphSnapshot,
        shared_items: tuple[ContextItem, ...] = (),
    ) -> tuple[ContextItem, ...]:
        self._validate(
            process=process,
            team_agent=team_agent,
            task=task,
            contract=contract,
            graph=graph,
        )
        label = ResourceLabel(
            owner_tenant_id=team_agent.team_id,
            classification=Classification.INTERNAL,
            compartments=frozenset({f"project:{process.project_id}"}),
            resource_id=contract.contract_id,
        )
        task_payload = {
            "schema": "coifesp.team-task-execution-context.v1",
            "process_id": process.process_id,
            "project_id": process.project_id,
            "team_agent_id": team_agent.agent_id,
            "task": {
                "task_id": task.task_id,
                "title": task.title,
                "description": task.description,
                "acceptance_criteria": task.acceptance_criteria,
                "source_team_id": task.source_team_id,
                "target_team_id": task.target_team_id,
                "status": task.status.value,
            },
            "contract": {
                "contract_id": contract.contract_id,
                "required_capability_tags": list(
                    contract.required_capability_tags
                ),
                "input_contract_ref": contract.input_contract_ref,
                "output_contract_ref": contract.output_contract_ref,
                "verification_policy_ref": contract.verification_policy_ref,
                "input_resource_ids": list(contract.input_resource_ids),
            },
        }
        visible_nodes = {item.node_id for item in graph.nodes if item.node_type is not WorkNodeType.ARTIFACT}
        graph_payload = {
            "schema": "coifesp.team-task-work-graph.v1",
            "project_id": graph.project_id,
            "graph_snapshot_digest": graph.digest,
            "nodes": [
                {
                    "node_id": item.node_id,
                    "node_type": item.node_type.value,
                    "subject_id": item.subject_id,
                }
                for item in graph.nodes
                if item.node_id in visible_nodes
            ],
            "relations": [
                {
                    "source_node_id": item.source_node_id,
                    "relation_type": item.relation_type.value,
                    "target_node_id": item.target_node_id,
                }
                for item in graph.relations
                if item.source_node_id in visible_nodes and item.target_node_id in visible_nodes
            ],
            "subjects": self._execution_subjects(graph.subjects),
        }
        generated = (
            self._item(
                item_id=f"task-contract:{contract.contract_id}",
                source_id=f"task:{task.task_id}",
                payload=task_payload,
                label=label,
                priority=100,
            ),
            self._item(
                item_id=f"task-graph:{task.task_id}:{graph.digest[:24]}",
                source_id=f"project:{process.project_id}",
                payload=graph_payload,
                label=ResourceLabel(
                    owner_tenant_id=team_agent.team_id,
                    classification=Classification.INTERNAL,
                    compartments=frozenset({f"project:{process.project_id}"}),
                    resource_id=f"project:{process.project_id}",
                ),
                priority=90,
            ),
        )
        item_ids = {item.item_id for item in generated}
        for item in shared_items:
            if item.item_id in item_ids:
                raise GovernanceConflictError("team Agent context item id is duplicated")
            item_ids.add(item.item_id)
        return generated + tuple(shared_items)

    @staticmethod
    def _execution_subjects(subjects):
        # Full graph hashing remains authoritative, but raw manifests are not
        # broadcast to other teams. The accepted task's filtered input manifest
        # is separately supplied by the transaction-bound fact loader.
        result = []
        hidden = {"input_manifest_json", "requested_capability", "output_contract_json",
                  "verification_policy_json", "artifact_resource_ids"}
        for subject in subjects:
            if subject.get("node_type") == "artifact":
                # Resource references/content only enter through input policy.
                continue
            value = subject.get("value")
            if isinstance(value, dict) and subject.get("node_type") == "task":
                subject = {**subject, "value": {k: v for k, v in value.items() if k not in hidden}}
            result.append(subject)
        return result

    @staticmethod
    def _validate(*, process, team_agent, task, contract, graph) -> None:
        project_ids = {
            process.project_id,
            team_agent.project_id,
            task.project_id,
            contract.project_id,
            graph.project_id,
        }
        if len(project_ids) != 1:
            raise GovernanceConflictError(
                "team Agent context belongs to multiple projects"
            )
        if team_agent.status is not TeamProjectAgentStatus.ACTIVE:
            raise GovernanceConflictError("team project Agent is not active")
        if task.status is not TeamTaskStatus.ACCEPTED:
            raise GovernanceConflictError(
                "only an accepted TeamTask may enter automatic execution context"
            )
        if (
            task.task_id != contract.task_id
            or task.target_team_id != team_agent.team_id
            or contract.target_team_id != team_agent.team_id
        ):
            raise GovernanceConflictError(
                "team task contract is bound to another task or team"
            )
        task_nodes = {
            item.subject_id
            for item in graph.nodes
            if item.node_type is WorkNodeType.TASK
        }
        if task.task_id not in task_nodes:
            raise GovernanceConflictError("team task is absent from the work graph")

    @staticmethod
    def _item(*, item_id, source_id, payload, label, priority) -> ContextItem:
        return ContextItem(
            item_id=item_id,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source=ContextSource.GOVERNANCE,
            source_id=source_id,
            label=label,
            content_trust=ContentTrust.AUTHORITATIVE,
            instruction_trust=InstructionTrust.DATA_ONLY,
            priority=priority,
        )


__all__ = ["ProjectAgentContextBuilder", "TeamTaskExecutionContract"]
