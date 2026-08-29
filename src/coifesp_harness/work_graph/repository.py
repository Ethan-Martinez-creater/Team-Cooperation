from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from enum import Enum
from typing import Iterator

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ..errors import GovernanceConflictError, ResourceNotFound
from ..product.repository import PROJECTS, PROJECT_RESOURCES, PROJECT_TEAMS, TEAM_TASKS
from .models import (
    ProjectGraphSnapshot,
    WorkNode,
    WorkNodeType,
    WorkRelation,
    WorkRelationType,
)

WORK_GRAPH_METADATA = MetaData()
OBJECT = JSON().with_variant(JSONB(), "postgresql")
NODE_TYPES = ",".join(f"'{item.value}'" for item in WorkNodeType)
RELATION_TYPES = ",".join(f"'{item.value}'" for item in WorkRelationType)

PROJECT_GOALS = Table(
    "project_goals",
    WORK_GRAPH_METADATA,
    Column("goal_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("success_criteria_json", OBJECT, nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("approved_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("version >= 1", name="project_goals_version"),
)

PROJECT_REQUIREMENTS = Table(
    "project_requirements",
    WORK_GRAPH_METADATA,
    Column("requirement_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("goal_id", String(128), nullable=False),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("requirement_type", String(64), nullable=False),
    Column("priority", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("source_type", String(64), nullable=False),
    Column("source_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

PROJECT_MILESTONES = Table(
    "project_milestones",
    WORK_GRAPH_METADATA,
    Column("milestone_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("target_at", DateTime(timezone=True), nullable=True),
    Column("status", String(32), nullable=False),
    Column("completion_policy", OBJECT, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

PROJECT_PHASES = Table(
    "project_phases",
    WORK_GRAPH_METADATA,
    Column("phase_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("milestone_id", String(128), nullable=True),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("owner_team_id", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

PROJECT_RISKS = Table(
    "project_risks",
    WORK_GRAPH_METADATA,
    Column("risk_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("title", String(256), nullable=False),
    Column("description", Text, nullable=False),
    Column("severity", String(32), nullable=False),
    Column("likelihood", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("owner_team_id", String(128), nullable=True),
    Column("mitigation", Text, nullable=False),
    Column("source_run_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

PROJECT_DECISIONS = Table(
    "project_decisions",
    WORK_GRAPH_METADATA,
    Column("decision_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("title", String(256), nullable=False),
    Column("decision", Text, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("proposed_by", String(128), nullable=False),
    Column("approved_by", String(128), nullable=True),
    Column("source_run_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

PROJECT_WORK_NODES = Table(
    "project_work_nodes",
    WORK_GRAPH_METADATA,
    Column("node_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("node_type", String(32), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"node_type IN ({NODE_TYPES})", name="work_node_type"),
    UniqueConstraint("node_id", "project_id", name="uq_work_node_project"),
    UniqueConstraint("project_id", "node_type", "subject_id", name="uq_work_node_subject"),
)

PROJECT_WORK_RELATIONS = Table(
    "project_work_relations",
    WORK_GRAPH_METADATA,
    Column("relation_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("source_node_id", String(128), nullable=False),
    Column("relation_type", String(32), nullable=False),
    Column("target_node_id", String(128), nullable=False),
    Column("created_by_type", String(32), nullable=False),
    Column("created_by_id", String(128), nullable=False),
    Column("source_run_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["source_node_id", "project_id"],
        ["project_work_nodes.node_id", "project_work_nodes.project_id"],
    ),
    ForeignKeyConstraint(
        ["target_node_id", "project_id"],
        ["project_work_nodes.node_id", "project_work_nodes.project_id"],
    ),
    CheckConstraint(f"relation_type IN ({RELATION_TYPES})", name="work_relation_type"),
    CheckConstraint("source_node_id <> target_node_id", name="work_relation_self"),
    UniqueConstraint(
        "project_id",
        "source_node_id",
        "relation_type",
        "target_node_id",
        name="uq_work_relation_semantic",
    ),
)

SUBJECT_TABLES = {
    WorkNodeType.GOAL: (PROJECT_GOALS, PROJECT_GOALS.c.goal_id),
    WorkNodeType.REQUIREMENT: (
        PROJECT_REQUIREMENTS,
        PROJECT_REQUIREMENTS.c.requirement_id,
    ),
    WorkNodeType.MILESTONE: (PROJECT_MILESTONES, PROJECT_MILESTONES.c.milestone_id),
    WorkNodeType.PHASE: (PROJECT_PHASES, PROJECT_PHASES.c.phase_id),
    WorkNodeType.TASK: (TEAM_TASKS, TEAM_TASKS.c.task_id),
    WorkNodeType.RISK: (PROJECT_RISKS, PROJECT_RISKS.c.risk_id),
    WorkNodeType.DECISION: (PROJECT_DECISIONS, PROJECT_DECISIONS.c.decision_id),
    WorkNodeType.ARTIFACT: (PROJECT_RESOURCES, PROJECT_RESOURCES.c.resource_id),
}


class SQLAlchemyWorkGraphRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_schema(self) -> None:
        WORK_GRAPH_METADATA.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    @staticmethod
    def require_project(connection: Connection, project_id: str) -> None:
        present = connection.execute(
            select(PROJECTS.c.project_id).where(PROJECTS.c.project_id == project_id)
        ).scalar_one_or_none()
        if present is None:
            raise ResourceNotFound("project is unavailable")

    def put_subject(
        self,
        connection: Connection,
        *,
        table: Table,
        key_column: Column,
        values: dict,
        compare_fields: tuple[str, ...],
    ) -> dict:
        base = pg_insert(table) if connection.dialect.name == "postgresql" else sqlite_insert(table)
        connection.execute(
            base.values(**values).on_conflict_do_nothing(index_elements=[key_column.name])
        )
        row = (
            connection.execute(select(table).where(key_column == values[key_column.name]))
            .mappings()
            .one()
        )
        for field in compare_fields:
            if self._canonical(row[field]) != self._canonical(values[field]):
                raise GovernanceConflictError(
                    f"{table.name} identifier was reused with different content"
                )
        return dict(row)

    def register_node(self, connection: Connection, *, values: dict) -> WorkNode:
        node_type = WorkNodeType(values["node_type"])
        table_info = SUBJECT_TABLES.get(node_type)
        if table_info is None:
            raise ResourceNotFound("work node subject type is not materialized yet")
        table, key = table_info
        subject = connection.execute(
            select(table.c.project_id).where(key == values["subject_id"])
        ).scalar_one_or_none()
        if subject is None or subject != values["project_id"]:
            raise ResourceNotFound("work node subject is unavailable")
        row = self.put_subject(
            connection,
            table=PROJECT_WORK_NODES,
            key_column=PROJECT_WORK_NODES.c.node_id,
            values=values,
            compare_fields=("project_id", "node_type", "subject_id"),
        )
        return self._node(row)

    def add_relation(self, connection: Connection, *, values: dict) -> WorkRelation:
        nodes = (
            connection.execute(
                select(PROJECT_WORK_NODES).where(
                    and_(
                        PROJECT_WORK_NODES.c.project_id == values["project_id"],
                        PROJECT_WORK_NODES.c.node_id.in_(
                            [values["source_node_id"], values["target_node_id"]]
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        if {row["node_id"] for row in nodes} != {
            values["source_node_id"],
            values["target_node_id"],
        }:
            raise ResourceNotFound("work relation node is unavailable")
        semantic = and_(
            PROJECT_WORK_RELATIONS.c.project_id == values["project_id"],
            PROJECT_WORK_RELATIONS.c.source_node_id == values["source_node_id"],
            PROJECT_WORK_RELATIONS.c.relation_type == values["relation_type"],
            PROJECT_WORK_RELATIONS.c.target_node_id == values["target_node_id"],
        )
        existing_id = (
            connection.execute(
                select(PROJECT_WORK_RELATIONS).where(
                    PROJECT_WORK_RELATIONS.c.relation_id == values["relation_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing_id is not None:
            for field in (
                "project_id",
                "source_node_id",
                "relation_type",
                "target_node_id",
            ):
                if existing_id[field] != values[field]:
                    raise GovernanceConflictError(
                        "work relation identifier was reused with different topology"
                    )
            return self._relation(existing_id)
        base = (
            pg_insert(PROJECT_WORK_RELATIONS)
            if connection.dialect.name == "postgresql"
            else sqlite_insert(PROJECT_WORK_RELATIONS)
        )
        connection.execute(
            base.values(**values).on_conflict_do_nothing(
                index_elements=[
                    "project_id",
                    "source_node_id",
                    "relation_type",
                    "target_node_id",
                ]
            )
        )
        row = connection.execute(select(PROJECT_WORK_RELATIONS).where(semantic)).mappings().one()
        return self._relation(row)

    def relations(self, connection: Connection, *, project_id: str) -> tuple[WorkRelation, ...]:
        rows = (
            connection.execute(
                select(PROJECT_WORK_RELATIONS)
                .where(PROJECT_WORK_RELATIONS.c.project_id == project_id)
                .order_by(
                    PROJECT_WORK_RELATIONS.c.source_node_id,
                    PROJECT_WORK_RELATIONS.c.relation_type,
                    PROJECT_WORK_RELATIONS.c.target_node_id,
                )
            )
            .mappings()
            .all()
        )
        return tuple(self._relation(row) for row in rows)

    def snapshot(self, connection: Connection, *, project_id: str) -> ProjectGraphSnapshot:
        self.require_project(connection, project_id)
        rows = (
            connection.execute(
                select(PROJECT_WORK_NODES)
                .where(PROJECT_WORK_NODES.c.project_id == project_id)
                .order_by(
                    PROJECT_WORK_NODES.c.node_type,
                    PROJECT_WORK_NODES.c.subject_id,
                    PROJECT_WORK_NODES.c.node_id,
                )
            )
            .mappings()
            .all()
        )
        nodes = tuple(self._node(row) for row in rows)
        relations = self.relations(connection, project_id=project_id)
        subjects = tuple(self._subject(connection, node) for node in nodes)
        canonical = {
            "project_id": project_id,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type.value,
                    "subject_id": node.subject_id,
                }
                for node in nodes
            ],
            "relations": [
                {
                    "source": item.source_node_id,
                    "type": item.relation_type.value,
                    "target": item.target_node_id,
                }
                for item in relations
            ],
            "subjects": subjects,
        }
        encoded = json.dumps(
            self._canonical(canonical),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return ProjectGraphSnapshot(
            project_id, nodes, relations, subjects, hashlib.sha256(encoded).hexdigest()
        )

    @staticmethod
    def _subject(connection: Connection, node: WorkNode) -> dict:
        table_info = SUBJECT_TABLES.get(node.node_type)
        if table_info is None:
            return {
                "node_type": node.node_type.value,
                "subject_id": node.subject_id,
                "unresolved": True,
            }
        table, key = table_info
        row = connection.execute(select(table).where(key == node.subject_id)).mappings().one()
        volatile = {
            "created_at",
            "updated_at",
            "approved_at",
            "completed_at",
            "due_changed_at",
            "created_by",
            "approved_by",
            "decided_by",
            "source_run_id",
        }
        return {
            "node_type": node.node_type.value,
            "subject_id": node.subject_id,
            "value": SQLAlchemyWorkGraphRepository._canonical(
                {key: value for key, value in dict(row).items() if key not in volatile}
            ),
        }

    @staticmethod
    def _canonical(value):
        if isinstance(value, dict):
            return {
                str(key): SQLAlchemyWorkGraphRepository._canonical(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [SQLAlchemyWorkGraphRepository._canonical(item) for item in value]
        if isinstance(value, (datetime, date)):
            aware = value
            if isinstance(value, datetime) and value.tzinfo is None:
                aware = value.replace(tzinfo=UTC)
            return aware.isoformat()
        if isinstance(value, Enum):
            return value.value
        return value

    @staticmethod
    def _node(row) -> WorkNode:
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return WorkNode(
            row["node_id"],
            row["project_id"],
            WorkNodeType(row["node_type"]),
            row["subject_id"],
            created,
        )

    @staticmethod
    def _relation(row) -> WorkRelation:
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return WorkRelation(
            row["relation_id"],
            row["project_id"],
            row["source_node_id"],
            WorkRelationType(row["relation_type"]),
            row["target_node_id"],
            row["created_by_type"],
            row["created_by_id"],
            row["source_run_id"],
            created,
        )
