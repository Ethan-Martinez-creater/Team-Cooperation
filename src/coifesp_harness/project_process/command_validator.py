"""Fail-closed validation for structured Planner orchestration commands."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..errors import GovernanceConflictError
from ..work_graph import ProjectGraphSnapshot, WorkNodeType, WorkRelationType
from .commands import ProjectProcessCommandType

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEVERITY = frozenset({"low", "medium", "high", "critical"})
_LIKELIHOOD = frozenset({"low", "medium", "high"})
_REWORK_STATUSES = frozenset({"submitted", "verified", "changes_requested"})


@dataclass(frozen=True, slots=True)
class ValidatedPlannerCommand:
    command_id: str
    command_type: ProjectProcessCommandType
    request: dict

    def as_record(self) -> dict:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type,
            "request": dict(self.request),
        }


class ProjectOrchestrationCommandValidator:
    """Validate model output as data; never infer intent from prose."""

    def validate(
        self,
        *,
        planner_intent_id: str,
        project_id: str,
        graph: ProjectGraphSnapshot,
        graph_snapshot_digest: str,
        participating_team_ids: Iterable[str],
        commands: Iterable[Mapping],
    ) -> tuple[ValidatedPlannerCommand, ...]:
        intent_id = self._identifier(planner_intent_id, "planner_intent_id")
        project_id = self._identifier(project_id, "project_id")
        if not isinstance(graph, ProjectGraphSnapshot):
            raise TypeError("graph must be ProjectGraphSnapshot")
        if graph.project_id != project_id:
            raise GovernanceConflictError("planner graph belongs to another project")
        if graph.digest != graph_snapshot_digest:
            raise GovernanceConflictError("planner graph digest is stale")
        teams = self._identifiers(participating_team_ids, "participating_team_ids")
        if not teams:
            raise GovernanceConflictError("project has no participating teams")
        raw_commands = self._command_objects(commands)
        references, task_statuses = self._graph_references(graph)
        new_tasks = self._new_task_ids(raw_commands)
        for task_id in new_tasks:
            node_id = f"node:task:{task_id}"
            if task_id in references or node_id in references:
                raise GovernanceConflictError("planner task already exists")
            references[task_id] = node_id
            references[node_id] = node_id

        normalized: list[tuple[ProjectProcessCommandType, dict]] = []
        dependency_edges = self._existing_dependency_edges(graph)
        for raw in raw_commands:
            command_type = self._command_type(raw)
            if command_type is ProjectProcessCommandType.PROPOSE_TASK:
                request = self._propose_task(raw, teams=teams, references=references)
                source = references[request["task_id"]]
                for dependency in request["dependencies"]:
                    dependency_edges.add((source, references[dependency]))
            elif command_type is ProjectProcessCommandType.PROPOSE_DEPENDENCY:
                request = self._propose_dependency(raw, references=references)
                dependency_edges.add(
                    (references[request["source_id"]], references[request["target_id"]])
                )
            elif command_type is ProjectProcessCommandType.PROPOSE_RISK:
                request = self._propose_risk(raw)
            elif command_type is ProjectProcessCommandType.PROPOSE_DECISION:
                request = self._propose_decision(raw)
            elif command_type is ProjectProcessCommandType.REQUEST_REWORK:
                request = self._request_rework(
                    raw, references=references, task_statuses=task_statuses
                )
            elif command_type is ProjectProcessCommandType.REQUEST_REPLAN:
                request = self._request_replan(raw)
            elif command_type is ProjectProcessCommandType.REQUEST_HUMAN_INPUT:
                request = self._request_human_input(raw)
            elif command_type is ProjectProcessCommandType.REQUEST_HUMAN_GATE:
                request = self._request_human_gate(raw, references=references)
            else:  # pragma: no cover - enum exhaustiveness
                raise ValueError("planner command type is unsupported")
            normalized.append((command_type, request))

        self._assert_acyclic(dependency_edges)
        values = []
        for index, (command_type, request) in enumerate(normalized):
            digest = self._digest(
                {"type": command_type.value, "request": request, "index": index}
            )
            values.append(
                ValidatedPlannerCommand(
                    command_id=f"command:{hashlib.sha256(f'{intent_id}:{digest}'.encode()).hexdigest()}",
                    command_type=command_type,
                    request=request,
                )
            )
        return tuple(values)

    @staticmethod
    def _command_objects(commands: Iterable[Mapping]) -> tuple[dict, ...]:
        if commands is None or isinstance(commands, (str, bytes, Mapping)):
            raise TypeError("commands must be an array of objects")
        values = tuple(commands)
        if any(not isinstance(item, Mapping) for item in values):
            raise TypeError("commands must contain only objects")
        if len(values) > 100:
            raise ValueError("planner command batch is too large")
        return tuple(dict(item) for item in values)

    @classmethod
    def _new_task_ids(cls, commands: tuple[dict, ...]) -> tuple[str, ...]:
        values = []
        for command in commands:
            if command.get("type") == ProjectProcessCommandType.PROPOSE_TASK.value:
                values.append(cls._identifier(command.get("task_id"), "task_id"))
        if len(values) != len(set(values)):
            raise GovernanceConflictError("planner batch repeats a task id")
        return tuple(values)

    @staticmethod
    def _command_type(raw: dict) -> ProjectProcessCommandType:
        try:
            return ProjectProcessCommandType(raw.get("type"))
        except (TypeError, ValueError) as exc:
            raise ValueError("planner command type is invalid") from exc

    @classmethod
    def _propose_task(cls, raw, *, teams, references):
        contract = raw.get("contract")
        cls._keys(
            {key: value for key, value in raw.items() if key != "contract"},
            required={"type", "task_id", "team_id", "title", "description", "dependencies"},
        )
        task_id = cls._identifier(raw["task_id"], "task_id")
        team_id = cls._identifier(raw["team_id"], "team_id")
        if team_id not in teams:
            raise GovernanceConflictError("planner task targets a non-participating team")
        dependencies = cls._reference_array(raw["dependencies"], references, "dependencies")
        if task_id in dependencies or f"node:task:{task_id}" in dependencies:
            raise GovernanceConflictError("planner task cannot depend on itself")
        result = {
            "task_id": task_id,
            "team_id": team_id,
            "title": cls._text(raw["title"], "title", 256),
            "description": cls._text(raw["description"], "description", 8000),
            "dependencies": dependencies,
        }
        if "contract" in raw:
            from ..team_agents.task_contract_models import validate_task_contract

            if type(contract) is not dict or set(contract) != {"requested_capability", "input_manifest",
                    "output_contract", "verification_policy", "autonomy_requirement"}:
                raise ValueError("planner task contract must contain all structured execution fields")
            result["contract"] = validate_task_contract(**contract)
        return result

    @classmethod
    def _propose_dependency(cls, raw, *, references):
        cls._keys(raw, required={"type", "source_id", "target_id"})
        source = cls._reference(raw["source_id"], references, "source_id")
        target = cls._reference(raw["target_id"], references, "target_id")
        if references[source] == references[target]:
            raise GovernanceConflictError("planner dependency cannot reference itself")
        return {"source_id": source, "target_id": target}

    @classmethod
    def _propose_risk(cls, raw):
        cls._keys(
            raw,
            required={
                "type",
                "risk_id",
                "title",
                "description",
                "severity",
                "likelihood",
                "mitigation",
            },
        )
        severity = str(raw["severity"])
        likelihood = str(raw["likelihood"])
        if severity not in _SEVERITY or likelihood not in _LIKELIHOOD:
            raise ValueError("planner risk enum is invalid")
        return {
            "risk_id": cls._identifier(raw["risk_id"], "risk_id"),
            "title": cls._text(raw["title"], "title", 256),
            "description": cls._text(raw["description"], "description", 8000),
            "severity": severity,
            "likelihood": likelihood,
            "mitigation": cls._text(raw["mitigation"], "mitigation", 8000),
        }

    @classmethod
    def _propose_decision(cls, raw):
        cls._keys(raw, required={"type", "decision_id", "title", "description", "options"})
        options = cls._string_array(raw["options"], "options", minimum=2)
        return {
            "decision_id": cls._identifier(raw["decision_id"], "decision_id"),
            "title": cls._text(raw["title"], "title", 256),
            "description": cls._text(raw["description"], "description", 8000),
            "options": options,
        }

    @classmethod
    def _request_rework(cls, raw, *, references, task_statuses):
        cls._keys(raw, required={"type", "task_id", "reason"})
        task_id = cls._reference(raw["task_id"], references, "task_id")
        status = task_statuses.get(references[task_id])
        if status not in _REWORK_STATUSES:
            raise GovernanceConflictError("planner rework target is not eligible")
        return {"task_id": task_id, "reason": cls._text(raw["reason"], "reason", 4000)}

    @classmethod
    def _request_replan(cls, raw):
        cls._keys(raw, required={"type", "reason"})
        return {"reason": cls._text(raw["reason"], "reason", 4000)}

    @classmethod
    def _request_human_input(cls, raw):
        cls._keys(raw, required={"type", "question", "input_schema"})
        if not isinstance(raw["input_schema"], Mapping):
            raise TypeError("input_schema must be an object")
        schema = dict(raw["input_schema"])
        if schema.get("type") != "object":
            raise ValueError("input_schema must describe an object")
        return {
            "question": cls._text(raw["question"], "question", 4000),
            "input_schema": schema,
        }

    @classmethod
    def _request_human_gate(cls, raw, *, references):
        cls._keys(
            raw,
            required={"type", "gate_type", "subject_id", "reason", "allowed_decisions"},
        )
        subject_id = cls._reference(raw["subject_id"], references, "subject_id")
        allowed = cls._string_array(raw["allowed_decisions"], "allowed_decisions")
        return {
            "gate_type": cls._identifier(raw["gate_type"], "gate_type"),
            "subject_id": subject_id,
            "reason": cls._text(raw["reason"], "reason", 4000),
            "allowed_decisions": allowed,
        }

    @staticmethod
    def _keys(raw: dict, *, required: set[str]) -> None:
        missing = required - set(raw)
        unknown = set(raw) - required
        if missing:
            raise ValueError(f"planner command is missing fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"planner command has unknown fields: {sorted(unknown)}")

    @classmethod
    def _graph_references(cls, graph):
        references: dict[str, str] = {}
        task_statuses: dict[str, str] = {}
        subjects = {
            (str(item.get("node_type")), str(item.get("subject_id"))): item
            for item in graph.subjects
            if isinstance(item, Mapping)
        }
        for node in graph.nodes:
            references[node.node_id] = node.node_id
            references[node.subject_id] = node.node_id
            if node.node_type is WorkNodeType.TASK:
                subject = subjects.get((WorkNodeType.TASK.value, node.subject_id), {})
                value = subject.get("value") if isinstance(subject, Mapping) else None
                if not isinstance(value, Mapping) or not isinstance(value.get("status"), str):
                    raise GovernanceConflictError("planner task subject is unresolved")
                task_statuses[node.node_id] = value["status"]
        return references, task_statuses

    @staticmethod
    def _existing_dependency_edges(graph) -> set[tuple[str, str]]:
        return {
            (item.source_node_id, item.target_node_id)
            for item in graph.relations
            if item.relation_type is WorkRelationType.DEPENDS_ON
        }

    @staticmethod
    def _assert_acyclic(edges: set[tuple[str, str]]) -> None:
        adjacency: dict[str, set[str]] = {}
        for source, target in edges:
            if source == target:
                raise GovernanceConflictError("planner dependency cannot reference itself")
            adjacency.setdefault(source, set()).add(target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise GovernanceConflictError("planner dependencies contain a cycle")
            if node in visited:
                return
            visiting.add(node)
            for target in adjacency.get(node, ()):
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in tuple(adjacency):
            visit(node)

    @classmethod
    def _reference_array(cls, value, references, field):
        values = cls._string_array(value, field, minimum=0)
        return tuple(cls._reference(item, references, field) for item in values)

    @classmethod
    def _reference(cls, value, references, field):
        identifier = cls._identifier(value, field)
        if identifier not in references:
            raise GovernanceConflictError(f"planner {field} is absent from the work graph")
        return identifier

    @classmethod
    def _identifiers(cls, values, field):
        if isinstance(values, (str, bytes)):
            raise TypeError(f"{field} must be an array")
        result = frozenset(cls._identifier(item, field) for item in values)
        return result

    @classmethod
    def _string_array(cls, value, field, *, minimum=1):
        if not isinstance(value, list) or len(value) < minimum:
            raise ValueError(f"{field} must be an array with at least {minimum} items")
        values = tuple(cls._text(item, field, 1024) for item in value)
        if len(values) != len(set(values)):
            raise ValueError(f"{field} cannot contain duplicates")
        return values

    @staticmethod
    def _identifier(value, field):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _text(value, field, maximum):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{field} is invalid")
        return value.strip()

    @staticmethod
    def _digest(value) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = [
    "ProjectOrchestrationCommandValidator",
    "ValidatedPlannerCommand",
]
