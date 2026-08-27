from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import AuthenticationError, PolicyDenied
from coifesp_harness.product import (
    DataPropagation, ProductAccountService, ProjectDataPolicy, ProjectDirectoryService,
    ProjectResourceService, ProjectTeamKind, ResourceAction, TeamAccountRole,
)
from coifesp_harness.product.repository import PROJECT_RESOURCES


def stack():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    return engine, accounts, ProjectDirectoryService(engine), ProjectDataPolicy(engine)


def create_team_account(accounts, suffix):
    team, bootstrap = accounts.register_team(team_id=f"team-{suffix}",
        team_handle=f"{suffix}-team", team_name=f"{suffix.title()} Team")
    accounts.change_initial_password(login=bootstrap.username,
        current_password=bootstrap.initial_password, new_password="Admin-Correct-Horse-42!")
    return accounts.get_account(bootstrap.account.account_id)


def relate(accounts, sender, recipient, suffix):
    recipient_team = accounts.get_team(recipient.team_id)
    request = accounts.send_team_relation_request(request_id=f"relation-{suffix}",
        actor_id=sender.account_id, recipient_team_handle=recipient_team.handle)
    accounts.decide_team_relation_request(request_id=request.request_id,
        actor_id=recipient.account_id, accept=True)


def test_registration_requires_existing_team_and_admin_approval():
    engine, accounts, _, _ = stack()
    owner = create_team_account(accounts, "alice")
    pending = accounts.request_account_registration(account_id="account-amy", username="amy",
        display_name="Amy", email="amy@example.test", password="Correct-Horse-42!",
        team_id=owner.team_id)
    with pytest.raises(AuthenticationError):
        accounts.login(login="amy", password="Correct-Horse-42!")
    with engine.connect() as connection:
        from coifesp_harness.product.repository import ACCOUNTS
        assert connection.execute(ACCOUNTS.select().where(
            ACCOUNTS.c.account_id == pending.account_id)).first() is None
    assert accounts.list_pending_registrations(owner.account_id) == [pending]
    approved = accounts.decide_account_registration(actor_id=owner.account_id,
        account_id=pending.account_id, accept=True)
    assert approved.status.value == "active"
    member = accounts.login(login="amy", password="Correct-Horse-42!").account
    assert member.team_id == owner.team_id and member.team_role is TeamAccountRole.MEMBER
    session = accounts.login(login="amy", password="Correct-Horse-42!")
    assert accounts.authenticate(session.token).team_id == owner.team_id
    with engine.connect() as connection:
        from coifesp_harness.product.repository import ACCOUNTS, ACCOUNT_SESSIONS
        rows = connection.execute(ACCOUNTS.select()).mappings().all()
        session_hashes = [row["session_hash"] for row in
            connection.execute(ACCOUNT_SESSIONS.select()).mappings().all()]
    assert all("Correct-Horse" not in row["password_hash"] for row in rows)
    assert all(session.token not in item for item in session_hashes)
    accounts.logout(session.token)
    with pytest.raises(AuthenticationError): accounts.authenticate(session.token)
    with pytest.raises(Exception, match="already"):
        accounts.request_account_registration(account_id="account-duplicate",
            username=member.username, display_name="Duplicate",
            email="duplicate@example.test", password="Correct-Horse-42!",
            team_id=owner.team_id)


def test_project_invitation_requires_team_relation_and_is_team_to_team():
    _, accounts, directory, _ = stack()
    alice, bob = create_team_account(accounts, "alice"), create_team_account(accounts, "bob")
    project = directory.create_project(project_id="project-a", name="A", description="",
        actor_id=alice.account_id, owner_assignment_name="产品团队",
        owner_kind=ProjectTeamKind.PRODUCT)
    with pytest.raises(PolicyDenied, match="relation"):
        directory.add_team(project_id=project.project_id, team_id=bob.team_id,
            name="工程团队", kind=ProjectTeamKind.ENGINEERING, actor_id=alice.account_id)
    relate(accounts, alice, bob, "ab")
    participation = directory.add_team(project_id=project.project_id, team_id=bob.team_id,
        name="工程团队", kind=ProjectTeamKind.ENGINEERING, actor_id=alice.account_id)
    assert participation.team_id == bob.team_id


def test_ordinary_account_cannot_communicate_as_individual_team_inviter():
    _, accounts, directory, _ = stack()
    owner = create_team_account(accounts, "alice")
    pending = accounts.request_account_registration(account_id="account-amy", username="amy",
        display_name="Amy", email="amy@example.test", password="Correct-Horse-42!",
        team_id=owner.team_id)
    accounts.decide_account_registration(actor_id=owner.account_id,
        account_id=pending.account_id, accept=True)
    member = accounts.get_account(pending.account_id)
    bob = create_team_account(accounts, "bob")
    with pytest.raises(PolicyDenied, match="administrators"):
        accounts.send_team_relation_request(request_id="relation-forbidden",
            actor_id=member.account_id, recipient_team_handle="bob-team")
    with pytest.raises(PolicyDenied, match="start projects"):
        directory.create_project(project_id="project-x", name="X", description="",
            actor_id=member.account_id, owner_assignment_name="工程团队",
            owner_kind=ProjectTeamKind.ENGINEERING)


def test_three_levels_follow_account_team_and_project_context():
    engine, accounts, directory, policy = stack()
    alice, bob, carol = (create_team_account(accounts, name) for name in ("alice", "bob", "carol"))
    relate(accounts, alice, bob, "ab")
    project = directory.create_project(project_id="project-a", name="A", description="",
        actor_id=alice.account_id, owner_assignment_name="产品团队",
        owner_kind=ProjectTeamKind.PRODUCT)
    directory.add_team(project_id=project.project_id, team_id=bob.team_id,
        name="工程团队", kind=ProjectTeamKind.ENGINEERING, actor_id=alice.account_id)
    with engine.begin() as connection:
        for propagation in DataPropagation:
            connection.execute(insert(PROJECT_RESOURCES).values(
                resource_id=f"resource-{propagation.value}", project_id=project.project_id,
                owner_team_id=alice.team_id, created_by=alice.account_id,
                title=propagation.value, artifact_owner_team_id=alice.team_id,
                artifact_id=f"artifact-{propagation.value}", artifact_sha256="a" * 64,
                media_type="text/plain", propagation=propagation.value,
                created_at=datetime.now(UTC)))

    assert policy.decide(account_id=alice.account_id, resource_id="resource-team_private",
        action=ResourceAction.DOWNLOAD, project_id=project.project_id).allowed
    assert not policy.decide(account_id=alice.account_id, resource_id="resource-team_private",
        action=ResourceAction.SAVE, project_id=project.project_id).allowed
    assert not policy.decide(account_id=alice.account_id, resource_id="resource-team_private",
        action=ResourceAction.RESHARE, project_id=project.project_id).allowed
    assert not policy.decide(account_id=bob.account_id, resource_id="resource-team_private",
        action=ResourceAction.VIEW, project_id=project.project_id).allowed
    assert policy.decide(account_id=bob.account_id, resource_id="resource-project_readonly",
        action=ResourceAction.AGENT_USE, project_id=project.project_id).allowed
    for action in (ResourceAction.DOWNLOAD, ResourceAction.SAVE, ResourceAction.RESHARE):
        assert not policy.decide(account_id=bob.account_id, resource_id="resource-project_readonly",
            action=action, project_id=project.project_id).allowed
    assert not policy.decide(account_id=bob.account_id, resource_id="resource-project_readonly",
        action=ResourceAction.VIEW, project_id="project-b").allowed
    assert policy.decide(account_id=bob.account_id, resource_id="resource-portable",
        action=ResourceAction.DOWNLOAD, project_id=project.project_id).allowed
    assert not policy.decide(account_id=carol.account_id, resource_id="resource-portable",
        action=ResourceAction.VIEW, project_id=project.project_id).allowed

    class Content:
        @staticmethod
        def open_policy_authorized(**kwargs):
            return iter([b"verified project context"])

    resources = ProjectResourceService(engine)
    items = resources.agent_context_items(actor_id=bob.account_id,
        project_id=project.project_id,
        resource_ids=("resource-project_readonly",), content_service=Content())
    assert items[0].content == "verified project context"
    assert items[0].instruction_trust.value == "data_only"
    with pytest.raises(PolicyDenied):
        resources.agent_context_items(actor_id=bob.account_id,
            project_id=project.project_id, resource_ids=("resource-team_private",),
            content_service=Content())
