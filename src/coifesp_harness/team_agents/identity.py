"""Delegated identities for Harness-dispatched project and Team Agents."""

from __future__ import annotations

import re
from typing import Protocol

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from ..errors import IntegrityError, PolicyDenied
from ..product.repository import PROJECT_TEAMS, TEAM_PROJECT_AGENTS
from ..security import Classification, Principal

ORCHESTRATOR_PRINCIPAL_ID = "service:project-orchestrator"
TEAM_AGENT_PREFIX = "team-agent:"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class HumanPrincipalResolver(Protocol):
    async def resolve(self, *, tenant_id: str, principal_id: str) -> Principal: ...


def project_orchestrator_principal(project_id: str, owner_team_id: str) -> Principal:
    if not _ID.fullmatch(project_id) or not _ID.fullmatch(owner_team_id):
        raise ValueError("project orchestrator scope is invalid")
    return Principal(
        principal_id=ORCHESTRATOR_PRINCIPAL_ID,
        tenant_id=owner_team_id,
        roles=frozenset({"project_orchestrator"}),
        clearance=Classification.INTERNAL,
        compartments=frozenset({f"project:{project_id}"}),
        is_service=True,
    )


class TeamAgentPrincipalResolver:
    """Resolve explicit Team Agent owners without weakening human OIDC checks."""

    def __init__(self, *, engine: Engine, human_resolver: HumanPrincipalResolver) -> None:
        self.engine = engine
        self.human_resolver = human_resolver

    async def resolve(self, *, tenant_id: str, principal_id: str) -> Principal:
        if not principal_id.startswith(TEAM_AGENT_PREFIX):
            return await self.human_resolver.resolve(
                tenant_id=tenant_id, principal_id=principal_id
            )
        team_id = principal_id[len(TEAM_AGENT_PREFIX) :]
        if (
            not _ID.fullmatch(tenant_id)
            or not _ID.fullmatch(team_id)
            or team_id != tenant_id
        ):
            raise IntegrityError("Team Agent owner identity does not match its tenant")
        with self.engine.connect() as connection:
            project_ids = tuple(
                connection.execute(
                    select(PROJECT_TEAMS.c.project_id)
                    .join(
                        TEAM_PROJECT_AGENTS,
                        and_(
                            TEAM_PROJECT_AGENTS.c.project_id
                            == PROJECT_TEAMS.c.project_id,
                            TEAM_PROJECT_AGENTS.c.team_id == PROJECT_TEAMS.c.team_id,
                        ),
                    )
                    .where(
                        and_(
                            PROJECT_TEAMS.c.team_id == team_id,
                            TEAM_PROJECT_AGENTS.c.status == "active",
                        )
                    )
                    .distinct()
                    .order_by(PROJECT_TEAMS.c.project_id)
                ).scalars()
            )
        if not project_ids:
            raise PolicyDenied("Team Agent has no active project delegation")
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            roles=frozenset({"team_agent"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset(
                f"project:{project_id}" for project_id in project_ids
            ),
            is_service=True,
        )


__all__ = [
    "ORCHESTRATOR_PRINCIPAL_ID",
    "TEAM_AGENT_PREFIX",
    "TeamAgentPrincipalResolver",
    "project_orchestrator_principal",
]
