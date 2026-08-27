"""Team project agent uniqueness and conversation isolation."""
from coifesp_harness.product.workspace import ProjectWorkspaceService

from test_project_conversations import seed


def test_each_project_team_has_one_agent():
    s = seed()
    workspace = s["workspace"]
    for team_id in ("team-product", "team-engineering", "team-quality"):
        workspace.ensure_team_project_agent(project_id="project-demo", team_id=team_id)
    agents = workspace.list_team_project_agents(project_id="project-demo")
    assert {agent.team_id for agent in agents} == {
        "team-product",
        "team-engineering",
        "team-quality",
    }


def test_same_team_members_share_agent_but_keep_private_conversations():
    s = seed()
    workspace = s["workspace"]
    # A second member of the product team joins the demo project.
    s["accounts"].ensure_active_account(
        account_id="lead-second",
        username="lead-second",
        display_name="产品二组",
        email="lead-second@demo.invalid",
        team_id="team-product",
    )
    agent_a = workspace.ensure_team_project_agent(project_id="project-demo", team_id="team-product")
    agent_b = workspace.ensure_team_project_agent(project_id="project-demo", team_id="team-product")
    assert agent_a.agent_id == agent_b.agent_id
    conv_a = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    conv_b = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-second")
    assert conv_a.team_agent_id == conv_b.team_agent_id
    assert conv_a.conversation_id != conv_b.conversation_id


def test_team_agent_cannot_see_other_team_private_attachment():
    s = seed()
    workspace = s["workspace"]
    from coifesp_harness.product import DataPropagation
    from coifesp_harness.product.repository import PROJECT_RESOURCES
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with s["engine"].begin() as connection:
        connection.execute(
            PROJECT_RESOURCES.insert().values(
                resource_id="attach-private",
                project_id="project-demo",
                owner_team_id="team-engineering",
                created_by="contributor-zhou",
                title="工程机密",
                artifact_owner_team_id="team-engineering",
                artifact_id="artifact-attach",
                artifact_sha256="d" * 64,
                media_type="text/plain",
                propagation=DataPropagation.TEAM_PRIVATE.value,
                created_at=now,
            )
        )
    conversation = workspace.ensure_conversation(project_id="project-demo", actor_id="lead-lin")
    # product user tries to attach the engineering-private resource
    from coifesp_harness.errors import GovernanceConflictError

    try:
        workspace.append_user_message(
            conversation_id=conversation.conversation_id,
            actor_id="lead-lin",
            content="引用工程私有文件",
            idempotency_key="attach-1",
            attachment_resource_ids=("attach-private",),
        )
        raise AssertionError("expected attaching another team's private resource to be rejected")
    except GovernanceConflictError:
        pass