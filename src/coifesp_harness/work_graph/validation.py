from __future__ import annotations

import re
from collections.abc import Iterable

from ..errors import GovernanceConflictError
from .models import WorkNodeType, WorkRelation, WorkRelationType

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def validate_identifier(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not _ID.fullmatch(normalized):
        raise ValueError(f"{field} is invalid")
    return normalized


def parse_node_type(value: WorkNodeType | str) -> WorkNodeType:
    try:
        return value if isinstance(value, WorkNodeType) else WorkNodeType(value)
    except ValueError as exc:
        raise ValueError("node_type is invalid") from exc


def parse_relation_type(value: WorkRelationType | str) -> WorkRelationType:
    try:
        return value if isinstance(value, WorkRelationType) else WorkRelationType(value)
    except ValueError as exc:
        raise ValueError("relation_type is invalid") from exc


def ensure_dependency_acyclic(
    relations: Iterable[WorkRelation],
    *,
    source_node_id: str,
    target_node_id: str,
) -> None:
    if source_node_id == target_node_id:
        raise GovernanceConflictError("work relation cannot reference itself")
    adjacency: dict[str, set[str]] = {}
    for relation in relations:
        if relation.relation_type is WorkRelationType.DEPENDS_ON:
            adjacency.setdefault(relation.source_node_id, set()).add(relation.target_node_id)
    pending = [target_node_id]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == source_node_id:
            raise GovernanceConflictError("depends_on relation would create a cycle")
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, ()))
