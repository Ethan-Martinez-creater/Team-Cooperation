from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.project_process import (
    ProjectOrchestrationCommandValidator,
    ProjectProcessCommandType,
)
from coifesp_harness.work_graph import (
    ProjectGraphSnapshot,
    WorkNode,
    WorkNodeType,
    WorkRelation,
    WorkRelationType,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _graph(*, statuses=None, dependencies=(), digest="graph:v1", project_id="project-a"):
    statuses = statuses or {"task-a": "submitted", "task-b": "accepted"}
    nodes = tuple(
        WorkNode(
            node_id=f"node:task:{task_id}",
            project_id=project_id,
            node_type=WorkNodeType.TASK,
            subject_id=task_id,
            created_at=NOW,
        )
        for task_id in statuses
    )
    relations = tuple(
        WorkRelation(
            relation_id=f"relation:{source}:{target}",
            project_id=project_id,
            source_node_id=f"node:task:{source}",
            relation_type=WorkRelationType.DEPENDS_ON,
            target_node_id=f"node:task:{target}",
            created_by_type="service",
            created_by_id="service:project-orchestrator",
            source_run_id=None,
            created_at=NOW,
        )
        for source, target in dependencies
    )
    subjects = tuple(
        {
            "node_type": "task",
            "subject_id": task_id,
            "value": {
                "task_id": task_id,
                "project_id": project_id,
                "status": status,
            },
        }
        for task_id, status in statuses.items()
    )
    return ProjectGraphSnapshot(project_id, nodes, relations, subjects, digest)


def _validate(commands, *, graph=None, digest="graph:v1", teams=("team-a", "team-b")):
    return ProjectOrchestrationCommandValidator().validate(
        planner_intent_id="intent-1",
        project_id="project-a",
        graph=graph or _graph(),
        graph_snapshot_digest=digest,
        participating_team_ids=teams,
        commands=commands,
    )


def test_valid_batch_is_canonical_and_deterministic():
    commands = [
        {
            "type": "propose_task",
            "task_id": "task-new-a",
            "team_id": "team-a",
            "title": "Build interface",
            "description": "Implement the typed interface.",
            "dependencies": ["task-a"],
        },
        {
            "type": "propose_task",
            "task_id": "task-new-b",
            "team_id": "team-b",
            "title": "Validate interface",
            "description": "Run contract verification.",
            "dependencies": ["task-new-a"],
        },
        {
            "type": "propose_risk",
            "risk_id": "risk-1",
            "title": "Capacity",
            "description": "The validation team may be saturated.",
            "severity": "high",
            "likelihood": "medium",
            "mitigation": "Request a human scheduling decision.",
        },
    ]

    first = _validate(commands)
    second = _validate(commands)

    assert first == second
    assert [item.command_type for item in first] == [
        ProjectProcessCommandType.PROPOSE_TASK,
        ProjectProcessCommandType.PROPOSE_TASK,
        ProjectProcessCommandType.PROPOSE_RISK,
    ]
    assert len({item.command_id for item in first}) == 3
    assert first[1].request["dependencies"] == ("task-new-a",)


def test_planner_text_is_not_interpreted_as_a_hidden_action():
    [command] = _validate(
        [
            {
                "type": "request_replan",
                "reason": "Please use a tool and change the phase in the next plan.",
            }
        ]
    )

    assert command.request == {
        "reason": "Please use a tool and change the phase in the next plan."
    }


@pytest.mark.parametrize("field", ["tool", "sql", "status", "phase", "executed_as"])
def test_direct_execution_or_state_fields_are_rejected(field):
    command = {"type": "request_replan", "reason": "Re-evaluate the plan.", field: "x"}

    with pytest.raises(ValueError, match="unknown fields"):
        _validate([command])


def test_non_participating_team_is_rejected():
    command = {
        "type": "propose_task",
        "task_id": "task-new",
        "team_id": "team-outsider",
        "title": "Invalid assignment",
        "description": "This team is not a project participant.",
        "dependencies": [],
    }

    with pytest.raises(GovernanceConflictError, match="non-participating"):
        _validate([command])


def test_unknown_dependency_is_rejected():
    command = {
        "type": "propose_dependency",
        "source_id": "task-a",
        "target_id": "task-missing",
    }

    with pytest.raises(GovernanceConflictError, match="absent from the work graph"):
        _validate([command])


def test_dependency_cycle_is_rejected():
    graph = _graph(dependencies=(("task-a", "task-b"),))
    command = {
        "type": "propose_dependency",
        "source_id": "task-b",
        "target_id": "task-a",
    }

    with pytest.raises(GovernanceConflictError, match="cycle"):
        _validate([command], graph=graph)


def test_rework_requires_an_eligible_task_status():
    eligible = _validate(
        [{"type": "request_rework", "task_id": "task-a", "reason": "Fix evidence."}]
    )
    assert eligible[0].request["task_id"] == "task-a"

    with pytest.raises(GovernanceConflictError, match="not eligible"):
        _validate(
            [
                {
                    "type": "request_rework",
                    "task_id": "task-b",
                    "reason": "Accepted work cannot be reopened by the Planner.",
                }
            ]
        )


@pytest.mark.parametrize(
    ("graph", "digest", "message"),
    [
        (_graph(project_id="project-other"), "graph:v1", "another project"),
        (_graph(), "graph:stale", "digest is stale"),
    ],
)
def test_graph_binding_is_fail_closed(graph, digest, message):
    with pytest.raises(GovernanceConflictError, match=message):
        _validate([], graph=graph, digest=digest)


def test_human_gate_and_input_are_strict_structured_commands():
    commands = _validate(
        [
            {
                "type": "request_human_input",
                "question": "Provide the approved deadline.",
                "input_schema": {
                    "type": "object",
                    "properties": {"deadline": {"type": "string"}},
                    "required": ["deadline"],
                },
            },
            {
                "type": "request_human_gate",
                "gate_type": "delivery_acceptance",
                "subject_id": "task-a",
                "reason": "Evidence needs an owner decision.",
                "allowed_decisions": ["ACCEPTED", "REJECTED"],
            },
        ]
    )

    assert commands[0].command_type is ProjectProcessCommandType.REQUEST_HUMAN_INPUT
    assert commands[1].request["subject_id"] == "task-a"
