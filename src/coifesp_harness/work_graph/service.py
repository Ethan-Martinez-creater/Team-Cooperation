from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from ..errors import GovernanceConflictError, ResourceNotFound
from .models import ProjectGraphSnapshot, WorkNodeType, WorkRelation, WorkRelationType
from ..product.repository import PROJECT_TEAMS
from .repository import (
    PROJECT_DECISIONS,
    PROJECT_GOALS,
    PROJECT_MILESTONES,
    PROJECT_PHASES,
    PROJECT_REQUIREMENTS,
    PROJECT_RISKS,
    SQLAlchemyWorkGraphRepository,
)
from .validation import (
    ensure_dependency_acyclic,
    parse_node_type,
    parse_relation_type,
    validate_identifier,
)


class ProjectWorkGraphService:
    def __init__(self, repository: SQLAlchemyWorkGraphRepository) -> None:
        self.repository = repository

    def create_goal(
        self,
        *,
        goal_id: str,
        project_id: str,
        title: str,
        description: str,
        success_criteria: tuple[str, ...],
        created_by: str,
        status: str = "approved",
        version: int = 1,
    ):
        now = datetime.now(UTC)
        values = {
            "goal_id": validate_identifier(goal_id, "goal_id"),
            "project_id": validate_identifier(project_id, "project_id"),
            "title": self._text(title, "title", 256),
            "description": self._text(description, "description", 20000),
            "success_criteria_json": [
                self._text(item, "success criterion", 2000) for item in success_criteria
            ],
            "status": self._text(status, "status", 32),
            "version": self._positive(version, "version"),
            "created_by": validate_identifier(created_by, "created_by"),
            "approved_by": created_by if status == "approved" else None,
            "created_at": now,
            "approved_at": now if status == "approved" else None,
        }
        return self._put_with_node(
            table=PROJECT_GOALS,
            key=PROJECT_GOALS.c.goal_id,
            values=values,
            compare=(
                "project_id",
                "title",
                "description",
                "success_criteria_json",
                "status",
                "version",
                "created_by",
                "approved_by",
            ),
            node_type=WorkNodeType.GOAL,
        )

    def create_requirement(
        self,
        *,
        requirement_id: str,
        project_id: str,
        goal_id: str,
        title: str,
        description: str,
        requirement_type: str,
        priority: str,
        source_type: str,
        source_id: str,
        status: str = "approved",
    ):
        values = {
            "requirement_id": validate_identifier(requirement_id, "requirement_id"),
            "project_id": validate_identifier(project_id, "project_id"),
            "goal_id": validate_identifier(goal_id, "goal_id"),
            "title": self._text(title, "title", 256),
            "description": self._text(description, "description", 20000),
            "requirement_type": self._text(requirement_type, "requirement_type", 64),
            "priority": self._text(priority, "priority", 32),
            "status": self._text(status, "status", 32),
            "source_type": self._text(source_type, "source_type", 64),
            "source_id": validate_identifier(source_id, "source_id"),
            "created_at": datetime.now(UTC),
        }
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, values["project_id"])
            goal_project = connection.execute(
                select(PROJECT_GOALS.c.project_id).where(
                    PROJECT_GOALS.c.goal_id == values["goal_id"]
                )
            ).scalar_one_or_none()
            if goal_project != values["project_id"]:
                raise ResourceNotFound("requirement goal is unavailable")
            return self._put_with_node_in_transaction(
                connection,
                table=PROJECT_REQUIREMENTS,
                key=PROJECT_REQUIREMENTS.c.requirement_id,
                values=values,
                compare=tuple(key for key in values if key != "created_at"),
                node_type=WorkNodeType.REQUIREMENT,
            )

    def create_milestone(
        self,
        *,
        milestone_id: str,
        project_id: str,
        title: str,
        description: str,
        target_at: datetime | None,
        completion_policy: dict,
        status: str = "planned",
    ):
        values = {
            "milestone_id": validate_identifier(milestone_id, "milestone_id"),
            "project_id": validate_identifier(project_id, "project_id"),
            "title": self._text(title, "title", 256),
            "description": self._text(description, "description", 20000),
            "target_at": target_at,
            "status": self._text(status, "status", 32),
            "completion_policy": dict(completion_policy),
            "created_at": datetime.now(UTC),
        }
        return self._put_with_node(
            table=PROJECT_MILESTONES,
            key=PROJECT_MILESTONES.c.milestone_id,
            values=values,
            compare=tuple(key for key in values if key != "created_at"),
            node_type=WorkNodeType.MILESTONE,
        )

    def create_phase(
        self,
        *,
        phase_id: str,
        project_id: str,
        title: str,
        description: str,
        milestone_id: str | None,
        owner_team_id: str | None,
        status: str = "planned",
    ):
        values = {
            "phase_id": validate_identifier(phase_id, "phase_id"),
            "project_id": validate_identifier(project_id, "project_id"),
            "milestone_id": (
                validate_identifier(milestone_id, "milestone_id") if milestone_id else None
            ),
            "title": self._text(title, "title", 256),
            "description": self._text(description, "description", 20000),
            "owner_team_id": (
                validate_identifier(owner_team_id, "owner_team_id") if owner_team_id else None
            ),
            "status": self._text(status, "status", 32),
            "created_at": datetime.now(UTC),
        }
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, values["project_id"])
            if values["milestone_id"]:
                parent_project = connection.execute(
                    select(PROJECT_MILESTONES.c.project_id).where(
                        PROJECT_MILESTONES.c.milestone_id == values["milestone_id"]
                    )
                ).scalar_one_or_none()
                if parent_project != values["project_id"]:
                    raise ResourceNotFound("phase milestone is unavailable")
            self._require_project_team(
                connection,
                project_id=values["project_id"],
                team_id=values["owner_team_id"],
            )
            return self._put_with_node_in_transaction(
                connection,
                table=PROJECT_PHASES,
                key=PROJECT_PHASES.c.phase_id,
                values=values,
                compare=tuple(key for key in values if key != "created_at"),
                node_type=WorkNodeType.PHASE,
            )

    def create_risk(
        self,
        *,
        risk_id: str,
        project_id: str,
        title: str,
        description: str,
        severity: str,
        likelihood: str,
        mitigation: str,
        owner_team_id: str | None = None,
        source_run_id: str | None = None,
        status: str = "open",
    ):
        values = {
            "risk_id": validate_identifier(risk_id, "risk_id"),
            "project_id": validate_identifier(project_id, "project_id"),
            "title": self._text(title, "title", 256),
            "description": self._text(description, "description", 20000),
            "severity": self._text(severity, "severity", 32),
            "likelihood": self._text(likelihood, "likelihood", 32),
            "status": self._text(status, "status", 32),
            "owner_team_id": (
                validate_identifier(owner_team_id, "owner_team_id") if owner_team_id else None
            ),
            "mitigation": self._text(mitigation or "unspecified", "mitigation", 20000),
            "source_run_id": source_run_id,
            "created_at": datetime.now(UTC),
        }
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, values["project_id"])
            self._require_project_team(
                connection,
                project_id=values["project_id"],
                team_id=values["owner_team_id"],
            )
            return self._put_with_node_in_transaction(
                connection,
                table=PROJECT_RISKS,
                key=PROJECT_RISKS.c.risk_id,
                values=values,
                compare=tuple(key for key in values if key != "created_at"),
                node_type=WorkNodeType.RISK,
            )

    def create_decision(
        self,
        *,
        decision_id: str,
        project_id: str,
        title: str,
        decision: str,
        rationale: str,
        proposed_by: str,
        approved_by: str | None = None,
        source_run_id: str | None = None,
        status: str = "accepted",
    ):
        values = {
            "decision_id": validate_identifier(decision_id, "decision_id"),
            "project_id": validate_identifier(project_id, "project_id"),
            "title": self._text(title, "title", 256),
            "decision": self._text(decision, "decision", 20000),
            "rationale": self._text(rationale, "rationale", 20000),
            "status": self._text(status, "status", 32),
            "proposed_by": validate_identifier(proposed_by, "proposed_by"),
            "approved_by": (
                validate_identifier(approved_by or proposed_by, "approved_by")
                if status == "accepted"
                else None
            ),
            "source_run_id": source_run_id,
            "created_at": datetime.now(UTC),
        }
        return self._put_with_node(
            table=PROJECT_DECISIONS,
            key=PROJECT_DECISIONS.c.decision_id,
            values=values,
            compare=tuple(key for key in values if key != "created_at"),
            node_type=WorkNodeType.DECISION,
        )

    def register_existing_subject(
        self,
        *,
        node_id: str,
        project_id: str,
        node_type: WorkNodeType | str,
        subject_id: str,
    ):
        parsed = parse_node_type(node_type)
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, project_id)
            return self.repository.register_node(
                connection,
                values={
                    "node_id": validate_identifier(node_id, "node_id"),
                    "project_id": validate_identifier(project_id, "project_id"),
                    "node_type": parsed.value,
                    "subject_id": validate_identifier(subject_id, "subject_id"),
                    "created_at": datetime.now(UTC),
                },
            )

    def add_relation(
        self,
        *,
        relation_id: str,
        project_id: str,
        source_node_id: str,
        relation_type: WorkRelationType | str,
        target_node_id: str,
        created_by_type: str,
        created_by_id: str,
        source_run_id: str | None = None,
    ) -> WorkRelation:
        parsed = parse_relation_type(relation_type)
        if source_node_id == target_node_id:
            raise GovernanceConflictError("work relation cannot reference itself")
        with self.repository.transaction() as connection:
            existing = self.repository.relations(connection, project_id=project_id)
            if parsed is WorkRelationType.DEPENDS_ON:
                ensure_dependency_acyclic(
                    existing,
                    source_node_id=source_node_id,
                    target_node_id=target_node_id,
                )
            return self.repository.add_relation(
                connection,
                values={
                    "relation_id": validate_identifier(relation_id, "relation_id"),
                    "project_id": validate_identifier(project_id, "project_id"),
                    "source_node_id": validate_identifier(source_node_id, "source_node_id"),
                    "relation_type": parsed.value,
                    "target_node_id": validate_identifier(target_node_id, "target_node_id"),
                    "created_by_type": self._text(created_by_type, "created_by_type", 32),
                    "created_by_id": validate_identifier(created_by_id, "created_by_id"),
                    "source_run_id": source_run_id,
                    "created_at": datetime.now(UTC),
                },
            )

    def snapshot(self, *, project_id: str) -> ProjectGraphSnapshot:
        with self.repository.transaction() as connection:
            return self.repository.snapshot(connection, project_id=project_id)

    def _put_with_node(self, *, table, key, values, compare, node_type):
        with self.repository.transaction() as connection:
            self.repository.require_project(connection, values["project_id"])
            return self._put_with_node_in_transaction(
                connection,
                table=table,
                key=key,
                values=values,
                compare=compare,
                node_type=node_type,
            )

    def _put_with_node_in_transaction(self, connection, *, table, key, values, compare, node_type):
        subject = self.repository.put_subject(
            connection,
            table=table,
            key_column=key,
            values=values,
            compare_fields=compare,
        )
        node = self.repository.register_node(
            connection,
            values={
                "node_id": f"node:{node_type.value}:{values[key.name]}",
                "project_id": values["project_id"],
                "node_type": node_type.value,
                "subject_id": values[key.name],
                "created_at": values["created_at"],
            },
        )
        return subject, node

    @staticmethod
    def _require_project_team(connection, *, project_id: str, team_id: str | None):
        if team_id is None:
            return
        team_project = connection.execute(
            select(PROJECT_TEAMS.c.project_id).where(PROJECT_TEAMS.c.team_id == team_id)
        ).scalar_one_or_none()
        if team_project != project_id:
            raise ResourceNotFound("work object owner team is unavailable")

    @staticmethod
    def _text(value: str, field: str, maximum: int) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"{field} is invalid")
        return normalized

    @staticmethod
    def _positive(value: int, field: str) -> int:
        parsed = int(value)
        if parsed < 1:
            raise ValueError(f"{field} must be positive")
        return parsed
