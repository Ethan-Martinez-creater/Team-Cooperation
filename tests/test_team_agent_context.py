import json
from datetime import UTC, datetime

import pytest

from coifesp_harness.context import (
    ContentTrust,
    ContextItem,
    ContextSource,
    InstructionTrust,
)
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product import (
    TeamProjectAgent,
    TeamProjectAgentStatus,
    TeamTask,
    TeamTaskStatus,
)
from coifesp_harness.project_process import (
    ProjectAgentContextBuilder,
    ProjectProcess,
    ProjectProcessPhase,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
    TeamTaskExecutionContract,
)
from coifesp_harness.security import Classification, ResourceLabel
from coifesp_harness.work_graph import ProjectGraphSnapshot, WorkNode, WorkNodeType

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _process():
    return ProjectProcess(
        "process-a",
        "project-a",
        ProjectProcessPhase.EXECUTION,
        ProjectProcessStatus.READY,
        ProjectProcessWaitReason.NONE,
        4,
        "goal-a",
        "plan-a",
        "policy-a",
        1,
        "lead-a",
        NOW,
        NOW,
        8,
        7,
        None,
        None,
        None,
        None,
    )


def _agent():
    return TeamProjectAgent(
        "agent-a",
        "project-a",
        "team-target",
        TeamProjectAgentStatus.ACTIVE,
        1,
        NOW,
        NOW,
    )


def _task(status=TeamTaskStatus.ACCEPTED):
    return TeamTask(
        "task-a",
        "project-a",
        "team-source",
        "team-target",
        "lead-a",
        "Build API",
        "Implement the accepted interface.",
        "Contract tests pass.",
        status,
        None,
        (),
        "",
        NOW,
        NOW,
    )


def _contract(**overrides):
    values = {
        "contract_id": "contract-a",
        "task_id": "task-a",
        "project_id": "project-a",
        "target_team_id": "team-target",
        "required_capability_tags": ("python", "api"),
        "input_contract_ref": "contract:input:v1",
        "output_contract_ref": "contract:output:v1",
        "verification_policy_ref": "verify:api:v1",
        "input_resource_ids": ("resource-a",),
    }
    values.update(overrides)
    return TeamTaskExecutionContract(**values)


def _graph(project_id="project-a"):
    node = WorkNode("node:task:task-a", project_id, WorkNodeType.TASK, "task-a", NOW)
    return ProjectGraphSnapshot(
        project_id,
        (node,),
        (),
        (
            {
                "node_type": "task",
                "subject_id": "task-a",
                "value": {"task_id": "task-a", "status": "accepted"},
            },
        ),
        "sha256:" + "a" * 64,
    )


def test_context_prioritizes_contract_and_graph_without_promoting_instructions():
    shared = ContextItem(
        item_id="shared:resource-a",
        content="Reviewed shared artifact.",
        source=ContextSource.DOCUMENT,
        source_id="resource:resource-a",
        label=ResourceLabel(
            "team-source", Classification.INTERNAL, frozenset(), "resource-a"
        ),
        content_trust=ContentTrust.VERIFIED,
        instruction_trust=InstructionTrust.DATA_ONLY,
        priority=80,
    )

    items = ProjectAgentContextBuilder().build(
        process=_process(),
        team_agent=_agent(),
        task=_task(),
        contract=_contract(),
        graph=_graph(),
        shared_items=(shared,),
    )

    assert [item.priority for item in items] == [100, 90, 80]
    assert all(item.instruction_trust is InstructionTrust.DATA_ONLY for item in items)
    contract_payload = json.loads(items[0].content)
    assert contract_payload["schema"] == "coifesp.team-task-execution-context.v1"
    assert contract_payload["task"]["status"] == "accepted"
    assert contract_payload["contract"]["required_capability_tags"] == [
        "python",
        "api",
    ]
    graph_payload = json.loads(items[1].content)
    assert graph_payload["graph_snapshot_digest"] == _graph().digest


def test_proposed_task_can_never_enter_automatic_agent_context():
    with pytest.raises(GovernanceConflictError, match="only an accepted TeamTask"):
        ProjectAgentContextBuilder().build(
            process=_process(),
            team_agent=_agent(),
            task=_task(TeamTaskStatus.PROPOSED),
            contract=_contract(),
            graph=_graph(),
        )


@pytest.mark.parametrize(
    "contract",
    [
        _contract(task_id="task-other"),
        _contract(target_team_id="team-other"),
        _contract(project_id="project-other"),
    ],
)
def test_contract_binding_mismatch_fails_closed(contract):
    with pytest.raises(GovernanceConflictError):
        ProjectAgentContextBuilder().build(
            process=_process(),
            team_agent=_agent(),
            task=_task(),
            contract=contract,
            graph=_graph(),
        )


def test_task_must_be_present_in_bound_work_graph():
    graph = ProjectGraphSnapshot("project-a", (), (), (), "sha256:" + "b" * 64)

    with pytest.raises(GovernanceConflictError, match="absent from the work graph"):
        ProjectAgentContextBuilder().build(
            process=_process(),
            team_agent=_agent(),
            task=_task(),
            contract=_contract(),
            graph=graph,
        )


def test_duplicate_shared_item_id_is_rejected():
    duplicate = ContextItem(
        item_id="task-contract:contract-a",
        content="duplicate",
        source=ContextSource.DOCUMENT,
        source_id="resource:duplicate",
        label=ResourceLabel("team-target", Classification.INTERNAL),
    )

    with pytest.raises(GovernanceConflictError, match="duplicated"):
        ProjectAgentContextBuilder().build(
            process=_process(),
            team_agent=_agent(),
            task=_task(),
            contract=_contract(),
            graph=_graph(),
            shared_items=(duplicate,),
        )
