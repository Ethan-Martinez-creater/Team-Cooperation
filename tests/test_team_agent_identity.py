import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import IntegrityError, PolicyDenied
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    ProjectWorkspaceService,
    TeamAccountRole,
)
from coifesp_harness.security import Classification, Principal
from coifesp_harness.team_agents import (
    TeamAgentPrincipalResolver,
    project_orchestrator_principal,
)


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    accounts.register_team(team_id="team-a", team_handle="team-a", team_name="Team A")
    accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="identity test",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    return engine


class HumanResolver:
    def __init__(self):
        self.calls = []

    async def resolve(self, *, tenant_id, principal_id):
        self.calls.append((tenant_id, principal_id))
        return Principal(principal_id, tenant_id)


def test_team_agent_identity_is_machine_delegation_scoped_to_active_projects():
    engine = _stack()
    ProjectWorkspaceService(engine).ensure_team_project_agent(
        project_id="project-a", team_id="team-a"
    )
    human = HumanResolver()
    resolver = TeamAgentPrincipalResolver(engine=engine, human_resolver=human)

    principal = asyncio.run(
        resolver.resolve(tenant_id="team-a", principal_id="team-agent:team-a")
    )

    assert principal.is_service is True
    assert principal.roles == frozenset({"team_agent"})
    assert principal.clearance is Classification.INTERNAL
    assert principal.compartments == frozenset({"project:project-a"})
    assert human.calls == []


def test_human_identity_still_uses_strict_existing_resolver():
    engine = _stack()
    human = HumanResolver()
    resolver = TeamAgentPrincipalResolver(engine=engine, human_resolver=human)

    principal = asyncio.run(
        resolver.resolve(tenant_id="team-a", principal_id="human-a")
    )

    assert principal.principal_id == "human-a"
    assert human.calls == [("team-a", "human-a")]


def test_team_agent_identity_rejects_tenant_mismatch_and_missing_delegation():
    engine = _stack()
    resolver = TeamAgentPrincipalResolver(engine=engine, human_resolver=HumanResolver())

    with pytest.raises(IntegrityError, match="does not match"):
        asyncio.run(
            resolver.resolve(
                tenant_id="team-a", principal_id="team-agent:team-other"
            )
        )
    with pytest.raises(PolicyDenied, match="no active project delegation"):
        asyncio.run(
            resolver.resolve(tenant_id="team-a", principal_id="team-agent:team-a")
        )


def test_project_orchestrator_identity_has_only_project_scope():
    principal = project_orchestrator_principal("project-a", "team-a")

    assert principal.principal_id == "service:project-orchestrator"
    assert principal.roles == frozenset({"project_orchestrator"})
    assert principal.compartments == frozenset({"project:project-a"})
    assert principal.is_service is True
