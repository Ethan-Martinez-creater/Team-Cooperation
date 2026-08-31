"""Materialize Planner graph commands on a caller-owned connection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Connection

from ..errors import GovernanceConflictError, ResourceNotFound
from ..work_graph.models import WorkNodeType, WorkRelationType
from ..work_graph.repository import PROJECT_DECISIONS, PROJECT_RISKS, PROJECT_WORK_NODES
from ..work_graph.validation import ensure_dependency_acyclic, validate_identifier
from .commands import ProjectProcessCommand, ProjectProcessCommandType

_ORCHESTRATOR_PRINCIPAL_ID = "service:project-orchestrator"


class PlannerGraphMutations:
    """Apply graph-producing Planner commands on an existing connection."""

    def __init__(self, work_graph_repository) -> None:
        if work_graph_repository is None:
            raise TypeError("work_graph_repository is required")
        self.repository = work_graph_repository

    def apply(
        self,
        connection: Connection,
        *,
        command: ProjectProcessCommand,
        process,
        source_run_id: str | None,
        now: datetime,
    ) -> str:
        """Materialize one supported command without opening a transaction."""
        if not isinstance(connection, Connection):
            raise TypeError("connection must be a SQLAlchemy Connection")
        if not isinstance(command, ProjectProcessCommand):
            raise TypeError("command must be a ProjectProcessCommand")
        if (
            process is None
            or not hasattr(process, "process_id")
            or not hasattr(process, "project_id")
        ):
            raise TypeError("process must provide process_id and project_id")
        if command.process_id != process.process_id or command.project_id != process.project_id:
            raise GovernanceConflictError("planner graph command is bound to another process")
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        try:
            command_type = ProjectProcessCommandType(command.command_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("planner graph command type is unsupported") from exc
        request = self._request(command)
        if command_type is ProjectProcessCommandType.PROPOSE_RISK:
            return self._propose_risk(
                connection,
                project_id=process.project_id,
                request=request,
                source_run_id=source_run_id,
                now=now,
            )
        if command_type is ProjectProcessCommandType.PROPOSE_DECISION:
            return self._propose_decision(
                connection,
                project_id=process.project_id,
                request=request,
                source_run_id=source_run_id,
                now=now,
            )
        if command_type is ProjectProcessCommandType.PROPOSE_DEPENDENCY:
            return self._propose_dependency(
                connection,
                project_id=process.project_id,
                request=request,
                command=command,
                source_run_id=source_run_id,
                now=now,
            )
        raise ValueError("planner graph command type is unsupported")

    def _propose_risk(
        self,
        connection: Connection,
        *,
        project_id: str,
        request: Mapping,
        source_run_id: str | None,
        now: datetime,
    ) -> str:
        self._require_keys(
            request,
            {
                "risk_id",
                "title",
                "description",
                "severity",
                "likelihood",
                "mitigation",
            },
        )
        risk_id = validate_identifier(request["risk_id"], "risk_id")
        values = {
            "risk_id": risk_id,
            "project_id": project_id,
            "title": self._text(request["title"], "title", 256),
            "description": self._text(request["description"], "description", 20_000),
            "severity": self._text(request["severity"], "severity", 32),
            "likelihood": self._text(request["likelihood"], "likelihood", 32),
            "status": "open",
            "owner_team_id": None,
            "mitigation": self._text(request["mitigation"], "mitigation", 20_000),
            "source_run_id": source_run_id,
            "created_at": now,
        }
        self._assert_risk_decision_id_available(
            connection, identifier=risk_id, candidate=WorkNodeType.RISK
        )
        self.repository.put_subject(
            connection,
            table=PROJECT_RISKS,
            key_column=PROJECT_RISKS.c.risk_id,
            values=values,
            compare_fields=tuple(key for key in values if key != "created_at"),
        )
        self.repository.register_node(
            connection,
            values={
                "node_id": self._node_id(WorkNodeType.RISK, risk_id),
                "project_id": project_id,
                "node_type": WorkNodeType.RISK.value,
                "subject_id": risk_id,
                "created_at": now,
            },
        )
        return risk_id

    def _propose_decision(
        self,
        connection: Connection,
        *,
        project_id: str,
        request: Mapping,
        source_run_id: str | None,
        now: datetime,
    ) -> str:
        self._require_keys(request, {"decision_id", "title", "description", "options"})
        decision_id = validate_identifier(request["decision_id"], "decision_id")
        options = self._options(request["options"])
        values = {
            "decision_id": decision_id,
            "project_id": project_id,
            "title": self._text(request["title"], "title", 256),
            "decision": json.dumps(
                options, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "rationale": self._text(request["description"], "description", 20_000),
            "status": "proposed",
            "proposed_by": _ORCHESTRATOR_PRINCIPAL_ID,
            "approved_by": None,
            "source_run_id": source_run_id,
            "created_at": now,
        }
        self._assert_risk_decision_id_available(
            connection, identifier=decision_id, candidate=WorkNodeType.DECISION
        )
        self.repository.put_subject(
            connection,
            table=PROJECT_DECISIONS,
            key_column=PROJECT_DECISIONS.c.decision_id,
            values=values,
            compare_fields=tuple(key for key in values if key != "created_at"),
        )
        self.repository.register_node(
            connection,
            values={
                "node_id": self._node_id(WorkNodeType.DECISION, decision_id),
                "project_id": project_id,
                "node_type": WorkNodeType.DECISION.value,
                "subject_id": decision_id,
                "created_at": now,
            },
        )
        return decision_id

    def _propose_dependency(
        self,
        connection: Connection,
        *,
        project_id: str,
        request: Mapping,
        command: ProjectProcessCommand,
        source_run_id: str | None,
        now: datetime,
    ) -> str:
        self._require_keys(request, {"source_id", "target_id"})
        source_node_id = self._resolve_node(
            connection, project_id=project_id, alias=request["source_id"], field="source_id"
        )
        target_node_id = self._resolve_node(
            connection, project_id=project_id, alias=request["target_id"], field="target_id"
        )
        relations = self.repository.relations(connection, project_id=project_id)
        ensure_dependency_acyclic(
            relations, source_node_id=source_node_id, target_node_id=target_node_id
        )
        relation = self.repository.add_relation(
            connection,
            values={
                "relation_id": self.relation_id_for_command(command.command_id),
                "project_id": project_id,
                "source_node_id": source_node_id,
                "relation_type": WorkRelationType.DEPENDS_ON.value,
                "target_node_id": target_node_id,
                "created_by_type": "service",
                "created_by_id": _ORCHESTRATOR_PRINCIPAL_ID,
                "source_run_id": source_run_id,
                "created_at": now,
            },
        )
        return relation.relation_id

    @staticmethod
    def relation_id_for_command(command_id: str) -> str:
        """Return a stable relation identity derived from one command id."""
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("command_id is invalid")
        digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
        return f"relation:{digest}"

    @staticmethod
    def _request(command: ProjectProcessCommand) -> Mapping:
        request = command.request_json
        if not isinstance(request, Mapping):
            raise TypeError("planner graph command request must be an object")
        return request

    @staticmethod
    def _require_keys(request: Mapping, expected: set[str]) -> None:
        keys = set(request)
        missing = expected - keys
        unknown = keys - expected
        if missing:
            raise ValueError(f"planner graph command is missing fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"planner graph command has unknown fields: {sorted(unknown)}")

    @staticmethod
    def _text(value, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{field} is invalid")
        return value.strip()

    @classmethod
    def _options(cls, value) -> list[str]:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            raise ValueError("options must contain at least two items")
        options = [cls._text(item, "options", 1_024) for item in value]
        if len(options) != len(set(options)):
            raise ValueError("options cannot contain duplicates")
        return options

    @staticmethod
    def _assert_risk_decision_id_available(
        connection: Connection, *, identifier: str, candidate: WorkNodeType
    ) -> None:
        """Reject an already claimed risk or decision identifier."""
        candidate_table = PROJECT_RISKS if candidate is WorkNodeType.RISK else PROJECT_DECISIONS
        candidate_key = (
            candidate_table.c.risk_id
            if candidate is WorkNodeType.RISK
            else candidate_table.c.decision_id
        )
        other_table = PROJECT_DECISIONS if candidate is WorkNodeType.RISK else PROJECT_RISKS
        other_key = (
            other_table.c.decision_id if candidate is WorkNodeType.RISK else other_table.c.risk_id
        )
        for table_key in ((candidate_table, candidate_key), (other_table, other_key)):
            table, key = table_key
            if (
                connection.execute(select(key).where(key == identifier)).scalar_one_or_none()
                is not None
            ):
                raise GovernanceConflictError("risk or decision identifier is already claimed")

    @staticmethod
    def _node_id(node_type: WorkNodeType, subject_id: str) -> str:
        node_id = f"node:{node_type.value}:{subject_id}"
        if len(node_id) > 128:
            raise ValueError("subject identifier produces an oversized node_id")
        return node_id

    @staticmethod
    def _resolve_node(connection: Connection, *, project_id: str, alias, field: str) -> str:
        alias = validate_identifier(alias, field)
        rows = (
            connection.execute(
                select(PROJECT_WORK_NODES).where(
                    and_(
                        PROJECT_WORK_NODES.c.project_id == project_id,
                        or_(
                            PROJECT_WORK_NODES.c.node_id == alias,
                            PROJECT_WORK_NODES.c.subject_id == alias,
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ResourceNotFound(f"planner dependency {field} is unavailable")
        if len(rows) > 1:
            raise GovernanceConflictError(f"planner dependency {field} is ambiguous")
        return rows[0]["node_id"]


__all__ = ["PlannerGraphMutations"]
