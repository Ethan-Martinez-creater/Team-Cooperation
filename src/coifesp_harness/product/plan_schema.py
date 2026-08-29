from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..work_graph import WorkRelationType

PLAN_V1 = "coifesp.project-plan.v1"
PLAN_V2 = "coifesp.project-plan.v2"
_LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_TEAM_CATEGORIES = frozenset(
    {"product", "engineering", "quality", "design", "operations", "custom"}
)


@dataclass(frozen=True, slots=True)
class ParsedProjectPlan:
    schema_version: str
    goals: str
    scope: str
    phases: tuple[dict, ...]
    milestones: tuple[dict, ...]
    risks: tuple[dict, ...]
    dependencies: tuple[dict, ...]
    acceptance_criteria: tuple[str, ...]
    team_requirements: tuple[dict, ...]
    payload: dict


def parse_project_plan(content: str) -> ParsedProjectPlan:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("plan output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("plan output must be an object")
    schema = payload.get("schema")
    if schema == PLAN_V1:
        return _parse_v1(payload)
    if schema == PLAN_V2:
        return _parse_v2(payload)
    raise ValueError("plan output schema is invalid")


def _parse_v1(payload: dict) -> ParsedProjectPlan:
    goals = _text(payload.get("goals"), "plan goals", 20000)
    scope = _text(payload.get("scope"), "plan scope", 20000)
    phases = _objects(payload.get("phases", []), "phases")
    milestones = _objects(payload.get("milestones", []), "milestones")
    risks = _objects(payload.get("risks", []), "risks")
    dependencies = _objects(payload.get("dependencies", []), "dependencies")
    criteria = _strings(payload.get("acceptance_criteria", []), "acceptance_criteria")
    teams = _team_requirements(payload.get("team_requirements", []))
    normalized = {
        "schema": PLAN_V1,
        "goals": goals,
        "scope": scope,
        "phases": list(phases),
        "milestones": list(milestones),
        "risks": list(risks),
        "dependencies": list(dependencies),
        "acceptance_criteria": list(criteria),
        "team_requirements": list(teams),
    }
    return ParsedProjectPlan(
        PLAN_V1,
        goals,
        scope,
        phases,
        milestones,
        risks,
        dependencies,
        criteria,
        teams,
        normalized,
    )


def _parse_v2(payload: dict) -> ParsedProjectPlan:
    goal = _object(payload.get("goal"), "goal")
    goal = {
        "id": _local_id(goal.get("id"), "goal.id"),
        "title": _text(goal.get("title"), "goal.title", 256),
        "description": _text(goal.get("description"), "goal.description", 20000),
        "success_criteria": list(
            _strings(goal.get("success_criteria", []), "goal.success_criteria")
        ),
    }
    scope = _text(payload.get("scope") or goal["description"], "plan scope", 20000)
    requirements = _typed_items(
        payload.get("requirements", []),
        "requirements",
        required=("id", "title", "description"),
    )
    milestones = _typed_items(
        payload.get("milestones", []),
        "milestones",
        required=("id", "title"),
    )
    phases = _typed_items(
        payload.get("phases", []),
        "phases",
        required=("id", "title"),
    )
    tasks = _typed_items(
        payload.get("tasks", []),
        "tasks",
        required=("id", "title", "description"),
    )
    dependencies = _typed_items(
        payload.get("dependencies", []),
        "dependencies",
        required=("source_id", "target_id"),
        require_id=False,
    )
    risks = _typed_items(
        payload.get("risks", []),
        "risks",
        required=("id", "title", "description"),
    )
    criteria = _strings(payload.get("acceptance_criteria", []), "acceptance_criteria")
    teams = _team_requirements(payload.get("team_requirements", []))

    ids = {goal["id"]}
    for collection_name, collection in (
        ("requirements", requirements),
        ("milestones", milestones),
        ("phases", phases),
        ("tasks", tasks),
        ("risks", risks),
    ):
        for item in collection:
            local = _local_id(item["id"], f"{collection_name}.id")
            item["id"] = local
            if local in ids:
                raise ValueError("plan-local IDs must be globally unique")
            ids.add(local)
    for index, item in enumerate(requirements):
        item["goal_id"] = _local_id(
            item.get("goal_id") or goal["id"], f"requirements[{index}].goal_id"
        )
        if item["goal_id"] != goal["id"]:
            raise ValueError(f"requirements[{index}].goal_id references an unknown goal")
        if item["goal_id"] != goal["id"]:
            raise ValueError(f"requirements[{index}].goal_id references an unknown goal")
        item["requirement_type"] = _text(
            item.get("requirement_type") or "functional",
            f"requirements[{index}].requirement_type",
            64,
        )
        item["priority"] = _text(
            item.get("priority") or "normal", f"requirements[{index}].priority", 32
        )
    for index, item in enumerate(milestones):
        item["description"] = str(item.get("description") or item["title"]).strip()
        policy = item.get("completion_policy", {})
        if not isinstance(policy, dict):
            raise ValueError(f"milestones[{index}].completion_policy is malformed")
        item["completion_policy"] = policy
    for index, item in enumerate(phases):
        item["description"] = str(item.get("description") or item["title"]).strip()
        milestone_id = item.get("milestone_id")
        item["milestone_id"] = (
            _local_id(milestone_id, f"phases[{index}].milestone_id") if milestone_id else None
        )
        milestone_ids = {entry["id"] for entry in milestones}
        if item["milestone_id"] and item["milestone_id"] not in milestone_ids:
            raise ValueError(f"phases[{index}].milestone_id references an unknown milestone")
        milestone_ids = {entry["id"] for entry in milestones}
        if item["milestone_id"] and item["milestone_id"] not in milestone_ids:
            raise ValueError(f"phases[{index}].milestone_id references an unknown milestone")
        category = str(item.get("team_category") or "").strip()
        if category and category not in _TEAM_CATEGORIES:
            raise ValueError(f"phases[{index}].team_category is invalid")
        item["team_category"] = category
    for index, item in enumerate(tasks):
        phase_id = item.get("phase_id")
        item["phase_id"] = _local_id(phase_id, f"tasks[{index}].phase_id") if phase_id else None
        phase_ids = {entry["id"] for entry in phases}
        if item["phase_id"] and item["phase_id"] not in phase_ids:
            raise ValueError(f"tasks[{index}].phase_id references an unknown phase")
        phase_ids = {entry["id"] for entry in phases}
        if item["phase_id"] and item["phase_id"] not in phase_ids:
            raise ValueError(f"tasks[{index}].phase_id references an unknown phase")
        category = str(item.get("team_category") or "").strip()
        if category not in _TEAM_CATEGORIES:
            raise ValueError(f"tasks[{index}].team_category is invalid")
        item["team_category"] = category
        item["acceptance_criteria"] = list(
            _strings(
                item.get("acceptance_criteria", criteria),
                f"tasks[{index}].acceptance_criteria",
            )
        )
    for index, item in enumerate(dependencies):
        source = _local_id(item["source_id"], f"dependencies[{index}].source_id")
        target = _local_id(item["target_id"], f"dependencies[{index}].target_id")
        if source not in ids or target not in ids:
            raise ValueError(f"dependencies[{index}] references an unknown local ID")
        relation = str(item.get("relation_type") or "depends_on")
        try:
            item["relation_type"] = WorkRelationType(relation).value
        except ValueError as exc:
            raise ValueError(f"dependencies[{index}].relation_type is invalid") from exc
        item["source_id"], item["target_id"] = source, target
    for index, item in enumerate(risks):
        item["severity"] = _text(item.get("severity") or "medium", f"risks[{index}].severity", 32)
        item["likelihood"] = _text(
            item.get("likelihood") or "medium", f"risks[{index}].likelihood", 32
        )
        item["mitigation"] = _text(
            item.get("mitigation") or "unspecified",
            f"risks[{index}].mitigation",
            20000,
        )

    normalized = {
        "schema": PLAN_V2,
        "goal": goal,
        "scope": scope,
        "requirements": list(requirements),
        "milestones": list(milestones),
        "phases": list(phases),
        "tasks": list(tasks),
        "dependencies": list(dependencies),
        "risks": list(risks),
        "acceptance_criteria": list(criteria),
        "team_requirements": list(teams),
    }
    return ParsedProjectPlan(
        PLAN_V2,
        f"{goal['title']}: {goal['description']}",
        scope,
        phases,
        milestones,
        risks,
        dependencies,
        criteria,
        teams,
        normalized,
    )


def _typed_items(value, name, *, required, require_id=True):
    items = _objects(value, name)
    result = []
    for index, original in enumerate(items):
        item = dict(original)
        fields = required if require_id else tuple(field for field in required if field != "id")
        for field in fields:
            if not str(item.get(field) or "").strip():
                raise ValueError(f"{name}[{index}].{field} is required")
        result.append(item)
    return tuple(result)


def _team_requirements(value) -> tuple[dict, ...]:
    entries = _objects(value, "team_requirements", maximum=20)
    normalized = []
    for index, entry in enumerate(entries):
        category = str(entry.get("team_category") or "").strip()
        if category not in _TEAM_CATEGORIES:
            raise ValueError(f"team_requirements[{index}] has invalid team_category")
        try:
            count = int(entry.get("count", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"team_requirements[{index}] count is invalid") from exc
        if count < 1 or count > 500:
            raise ValueError(f"team_requirements[{index}] count is out of range")
        normalized.append(
            {
                "team_category": category,
                "count": count,
                "rationale": str(entry.get("rationale") or "").strip(),
            }
        )
    return tuple(normalized)


def _objects(value, name, maximum=200) -> tuple[dict, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a list of at most {maximum} entries")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{name} contains a malformed entry")
    return tuple(dict(item) for item in value)


def _strings(value, name, maximum=200) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a list of at most {maximum} entries")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result):
        raise ValueError(f"{name} contains an empty entry")
    return result


def _object(value, name) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _local_id(value, field) -> str:
    normalized = str(value or "").strip()
    if not _LOCAL_ID.fullmatch(normalized):
        raise ValueError(f"{field} is invalid")
    return normalized


def _text(value, field, maximum) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} is invalid")
    return normalized
