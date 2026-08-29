from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WorkNodeType(StrEnum):
    GOAL = "goal"
    REQUIREMENT = "requirement"
    MILESTONE = "milestone"
    PHASE = "phase"
    TASK = "task"
    RISK = "risk"
    DECISION = "decision"
    ARTIFACT = "artifact"
    VERIFICATION = "verification"


class WorkRelationType(StrEnum):
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    IMPLEMENTS = "implements"
    DELIVERS = "delivers"
    VERIFIES = "verifies"
    DERIVED_FROM = "derived_from"
    SUPERSEDES = "supersedes"
    RELATES_TO = "relates_to"
    PART_OF = "part_of"


@dataclass(frozen=True, slots=True)
class WorkNode:
    node_id: str
    project_id: str
    node_type: WorkNodeType
    subject_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkRelation:
    relation_id: str
    project_id: str
    source_node_id: str
    relation_type: WorkRelationType
    target_node_id: str
    created_by_type: str
    created_by_id: str
    source_run_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectGraphSnapshot:
    project_id: str
    nodes: tuple[WorkNode, ...]
    relations: tuple[WorkRelation, ...]
    subjects: tuple[dict, ...]
    digest: str
