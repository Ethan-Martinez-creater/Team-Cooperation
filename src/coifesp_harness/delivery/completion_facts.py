"""Build completion facts from the caller's locked project graph and records."""

from sqlalchemy import select

from ..product.repository import ACCOUNTS, PROJECT_TEAMS
from ..project_process.repository import PROJECT_GATES, PROJECT_INPUT_REQUESTS
from ..work_graph.repository import PROJECT_GOALS
from .manifest import current_delivery_evidence


def load_completion_facts(connection, *, repository, process, graph, delivery, contract, approvals, content):
    facts = current_delivery_evidence(connection, repository=repository, process=process,
        graph=graph, delivery=delivery, content=content)
    values = {(subject["node_type"], subject["subject_id"]): subject["value"] for subject in graph.subjects}
    nodes = {node.node_id: node for node in graph.nodes}
    incoming = {}
    for edge in graph.relations:
        if edge.relation_type.value in {"implements", "part_of", "delivers"}:
            incoming.setdefault(edge.target_node_id, []).append(edge.source_node_id)

    def task_coverage(node_id, visited=None):
        visited = set() if visited is None else visited
        if node_id in visited:
            return set()
        visited.add(node_id)
        node = nodes[node_id]
        if node.node_type.value == "task":
            return {node.subject_id}
        covered = set()
        for child in incoming.get(node_id, ()):
            covered.update(task_coverage(child, visited))
        return covered

    verified = {node.subject_id for node in graph.nodes if node.node_type.value == "task"
                and values[("task", node.subject_id)].get("status") == "verified"}
    requirements, milestones = True, True
    for node in graph.nodes:
        value = values[(node.node_type.value, node.subject_id)]
        if node.node_type.value not in {"requirement", "milestone"}:
            continue
        covered = task_coverage(node.node_id)
        satisfied = bool(covered) and covered.issubset(verified)
        if node.node_type.value == "requirement":
            requirements = requirements and satisfied and value.get("status") in {"approved", "fulfilled", "completed"}
        else:
            # Opaque/custom policies are not silently interpreted as passed.
            supported = value.get("completion_policy") in ({}, {"type": "all_tasks_verified"}, {"source_schema": "v1"})
            milestones = milestones and satisfied and supported and value.get("status") in {
                "planned", "active", "in_progress", "completed"}
    root = connection.execute(select(PROJECT_GOALS).where(
        PROJECT_GOALS.c.project_id == process.project_id,
        PROJECT_GOALS.c.goal_id == process.root_goal_id)).mappings().one_or_none()
    facts["goal_confirmed"] = bool(root and root["status"] == "approved" and root["approved_by"]
        and root["success_criteria_json"] and any(node.node_type.value == "goal"
            and node.subject_id == process.root_goal_id for node in graph.nodes))
    facts["all_required_requirements_satisfied"] = requirements
    facts["all_required_milestones_completed"] = milestones
    facts["no_open_blocking_risk"] = all(value.get("status") in {"resolved", "closed", "mitigated"}
        for (kind, _), value in values.items() if kind == "risk")

    def resolved(node_id):
        node = nodes[node_id]
        value = values[(node.node_type.value, node.subject_id)]
        if node.node_type.value == "task":
            return node.subject_id in verified
        if node.node_type.value in {"requirement", "milestone", "phase"}:
            coverage = task_coverage(node_id)
            return bool(coverage) and coverage.issubset(verified)
        if node.node_type.value == "risk":
            return value.get("status") in {"resolved", "closed", "mitigated"}
        if node.node_type.value == "artifact":
            return facts["all_required_artifacts_exist"] and facts["artifact_integrity_valid"]
        return value.get("status") in {"approved", "verified", "completed", "resolved"}

    facts["no_unresolved_blocking_dependency"] = all(
        resolved(edge.target_node_id if edge.relation_type.value == "depends_on" else edge.source_node_id)
        for edge in graph.relations if edge.relation_type.value in {"depends_on", "blocks"})
    facts["no_open_human_controls"] = not any(connection.execute(select(table.c.process_id).where(
        table.c.process_id == process.process_id, table.c.status == "OPEN").limit(1)).first()
        for table in (PROJECT_GATES, PROJECT_INPUT_REQUESTS))
    required = contract["required_human_approvers_json"]
    members = set(connection.execute(select(PROJECT_TEAMS.c.team_id).where(
        PROJECT_TEAMS.c.project_id == process.project_id).with_for_update()).scalars())
    accounts = connection.execute(select(ACCOUNTS).where(ACCOUNTS.c.account_id.in_(required))
                                  .with_for_update()).mappings().all()
    active = {row["account_id"] for row in accounts if row["enabled"]
              and row["registration_status"] == "active" and row["team_id"] in members}
    accepted = {row["actor_id"] for row in approvals if row["decision"] == "ACCEPT"
        and row["delivery_id"] == delivery["delivery_id"] and row["contract_id"] == contract["contract_id"]
        and row["contract_version"] == contract["version"]}
    facts["required_human_approvals_obtained"] = bool(required) and set(required).issubset(active & accepted)
    facts["required_delivery_manifest_exists"] = bool(delivery["artifact_refs_json"])
    facts["required_delivery_accepted"] = delivery["status"] == "ACCEPTED"
    return facts
