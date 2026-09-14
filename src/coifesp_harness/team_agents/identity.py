"""Delegated identities for Harness-dispatched project and Team Agents."""

from __future__ import annotations

import re
from typing import Protocol

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Engine

from ..errors import IntegrityError, PolicyDenied
from ..product.repository import (
    PROJECT_AGENT_RUNS,
    PROJECT_TEAMS,
    SPECIALIST_DELEGATIONS,
    TEAM_PROJECT_AGENTS,
    TEAM_TASKS,
)
from ..project_process.repository import PROJECT_PLANNER_INTENTS, PROJECT_PROCESSES
from ..security import Classification, Principal

ORCHESTRATOR_PRINCIPAL_ID = "service:project-orchestrator"
TEAM_AGENT_PREFIX = "team-agent:"
SPECIALIST_AGENT_PREFIX = "specialist-agent:"
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
        if principal_id.startswith(SPECIALIST_AGENT_PREFIX):
            raise PolicyDenied("Specialist Agent identity requires a durable run binding")
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

    async def resolve_for_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        run_id: str,
    ) -> Principal:
        """Resolve the owner of one durably bound delegated run.

        Run execution is narrower than the legacy identity lookup: a service
        principal must be named by the run's authoritative binding and the
        binding must still be active. Human principals intentionally keep the
        existing resolver path so this method can be used as a drop-in worker
        resolver without changing OIDC behavior.
        """
        if not isinstance(principal_id, str) or not principal_id:
            raise IntegrityError("delegated principal identity is invalid")
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise IntegrityError("delegated run identity is invalid")
        if principal_id == ORCHESTRATOR_PRINCIPAL_ID:
            if run_id.startswith("run-review-"):
                return await self._resolve_review_run(tenant_id=tenant_id, run_id=run_id)
            return await self._resolve_planner_run(
                tenant_id=tenant_id,
                principal_id=principal_id,
                run_id=run_id,
            )
        if principal_id.startswith(SPECIALIST_AGENT_PREFIX):
            return await self._resolve_specialist_run(
                tenant_id=tenant_id,
                principal_id=principal_id,
                run_id=run_id,
            )
        if principal_id.startswith(TEAM_AGENT_PREFIX):
            return await self._resolve_team_agent_run(
                tenant_id=tenant_id,
                principal_id=principal_id,
                run_id=run_id,
            )
        return await self.resolve(tenant_id=tenant_id, principal_id=principal_id)

    async def _resolve_specialist_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        run_id: str,
    ) -> Principal:
        identity = principal_id[len(SPECIALIST_AGENT_PREFIX) :]
        try:
            team_id, specialist_kind = identity.split(":", 1)
        except ValueError as exc:
            raise IntegrityError("Specialist Agent identity is malformed") from exc
        if (
            not _ID.fullmatch(tenant_id)
            or not _ID.fullmatch(team_id)
            or not _ID.fullmatch(specialist_kind)
            or team_id != tenant_id
        ):
            raise IntegrityError("Specialist Agent owner identity does not match its tenant")
        parent_runs = PROJECT_AGENT_RUNS.alias("specialist_parent_runs")
        with self.engine.begin() as connection:
            binding = (
                connection.execute(
                    select(
                        SPECIALIST_DELEGATIONS.c.delegation_id,
                        SPECIALIST_DELEGATIONS.c.project_id,
                        SPECIALIST_DELEGATIONS.c.status,
                    ).select_from(
                        SPECIALIST_DELEGATIONS.join(
                            PROJECT_AGENT_RUNS,
                            PROJECT_AGENT_RUNS.c.run_id
                            == SPECIALIST_DELEGATIONS.c.child_run_id,
                        )
                        .join(
                            parent_runs,
                            parent_runs.c.run_id
                            == SPECIALIST_DELEGATIONS.c.parent_run_id,
                        )
                        .join(
                            TEAM_PROJECT_AGENTS,
                            and_(
                                TEAM_PROJECT_AGENTS.c.agent_id
                                == SPECIALIST_DELEGATIONS.c.team_agent_id,
                                TEAM_PROJECT_AGENTS.c.project_id
                                == SPECIALIST_DELEGATIONS.c.project_id,
                                TEAM_PROJECT_AGENTS.c.team_id
                                == SPECIALIST_DELEGATIONS.c.team_id,
                            ),
                        )
                        .join(
                            PROJECT_TEAMS,
                            and_(
                                PROJECT_TEAMS.c.project_id
                                == SPECIALIST_DELEGATIONS.c.project_id,
                                PROJECT_TEAMS.c.team_id
                                == SPECIALIST_DELEGATIONS.c.team_id,
                            ),
                        )
                        .join(
                            TEAM_TASKS,
                            and_(
                                TEAM_TASKS.c.task_id
                                == SPECIALIST_DELEGATIONS.c.team_task_id,
                                TEAM_TASKS.c.project_id
                                == SPECIALIST_DELEGATIONS.c.project_id,
                            ),
                        )
                        .join(
                            PROJECT_PROCESSES,
                            PROJECT_PROCESSES.c.process_id
                            == SPECIALIST_DELEGATIONS.c.process_id,
                        )
                    ).where(
                        and_(
                            SPECIALIST_DELEGATIONS.c.child_run_id == run_id,
                            SPECIALIST_DELEGATIONS.c.team_id == team_id,
                            SPECIALIST_DELEGATIONS.c.specialist_kind == specialist_kind,
                            SPECIALIST_DELEGATIONS.c.status.in_(("PENDING", "RUNNING")),
                            PROJECT_AGENT_RUNS.c.run_kind == "specialist",
                            PROJECT_AGENT_RUNS.c.parent_run_id
                            == SPECIALIST_DELEGATIONS.c.parent_run_id,
                            PROJECT_AGENT_RUNS.c.executed_as_principal_id == principal_id,
                            PROJECT_AGENT_RUNS.c.initiated_by_principal_id
                            == TEAM_AGENT_PREFIX + team_id,
                            parent_runs.c.run_kind == "task_execution",
                            parent_runs.c.team_id == team_id,
                            parent_runs.c.team_task_id == TEAM_TASKS.c.task_id,
                            parent_runs.c.task_contract_version
                            == TEAM_TASKS.c.accepted_contract_version,
                            TEAM_TASKS.c.source_contract_version
                            == TEAM_TASKS.c.accepted_contract_version,
                            TEAM_TASKS.c.status == "in_progress",
                            TEAM_TASKS.c.target_team_id == team_id,
                            PROJECT_PROCESSES.c.phase == "EXECUTION",
                            PROJECT_PROCESSES.c.status.in_(("READY", "RUNNING")),
                            TEAM_PROJECT_AGENTS.c.status == "active",
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if binding is not None and binding["status"] == "PENDING":
                connection.execute(
                    update(SPECIALIST_DELEGATIONS)
                    .where(
                        SPECIALIST_DELEGATIONS.c.delegation_id
                        == binding["delegation_id"],
                        SPECIALIST_DELEGATIONS.c.status == "PENDING",
                    )
                    .values(status="RUNNING")
                )
        if binding is None:
            raise PolicyDenied("Specialist Agent run has no active delegation binding")
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            roles=frozenset({"specialist_agent"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset({f"project:{binding['project_id']}"}),
            is_service=True,
        )

    async def _resolve_review_run(self, *, tenant_id, run_id):
        from ..verification.repository import AGENT_REVIEWS, TASK_VERIFICATIONS

        with self.engine.connect() as connection:
            row = connection.execute(select(AGENT_REVIEWS.c.project_id).join(
                TASK_VERIFICATIONS,
                TASK_VERIFICATIONS.c.verification_id == AGENT_REVIEWS.c.verification_id,
            ).join(PROJECT_TEAMS, and_(
                PROJECT_TEAMS.c.project_id == AGENT_REVIEWS.c.project_id,
                PROJECT_TEAMS.c.team_id == AGENT_REVIEWS.c.owner_team_id,
            )).where(
                AGENT_REVIEWS.c.run_id == run_id, AGENT_REVIEWS.c.owner_team_id == tenant_id,
                AGENT_REVIEWS.c.status == "QUEUED", TASK_VERIFICATIONS.c.status == "PENDING",
                AGENT_REVIEWS.c.subject_digest == TASK_VERIFICATIONS.c.subject_digest,
                AGENT_REVIEWS.c.source_run_id == TASK_VERIFICATIONS.c.source_run_id,
                AGENT_REVIEWS.c.project_id == TASK_VERIFICATIONS.c.project_id,
                AGENT_REVIEWS.c.process_id == TASK_VERIFICATIONS.c.process_id,
                AGENT_REVIEWS.c.task_id == TASK_VERIFICATIONS.c.task_id,
                AGENT_REVIEWS.c.contract_version == TASK_VERIFICATIONS.c.contract_version,
                AGENT_REVIEWS.c.executed_as == ORCHESTRATOR_PRINCIPAL_ID,
                AGENT_REVIEWS.c.initiated_by == ORCHESTRATOR_PRINCIPAL_ID,
            )).mappings().one_or_none()
        if row is None:
            raise PolicyDenied("review run has no active project delegation")
        return project_orchestrator_principal(row["project_id"], tenant_id)

    async def _resolve_planner_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        run_id: str,
    ) -> Principal:
        if not _ID.fullmatch(tenant_id):
            raise IntegrityError("Planner owner identity does not match its tenant")
        with self.engine.connect() as connection:
            intent = (
                connection.execute(
                    select(
                        PROJECT_PLANNER_INTENTS.c.project_id,
                        PROJECT_PLANNER_INTENTS.c.owner_team_id,
                    ).where(
                        and_(
                            PROJECT_PLANNER_INTENTS.c.run_id == run_id,
                            PROJECT_PLANNER_INTENTS.c.owner_team_id == tenant_id,
                            PROJECT_PLANNER_INTENTS.c.status.in_(("PENDING", "RUNNING")),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if intent is None:
                raise PolicyDenied("Planner run has no active delegation binding")
            participating = connection.execute(
                select(PROJECT_TEAMS.c.project_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == intent["project_id"],
                        PROJECT_TEAMS.c.team_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        if participating is None:
            raise PolicyDenied("Planner owner is no longer a project participant")
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            roles=frozenset({"project_orchestrator"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset({f"project:{intent['project_id']}"}),
            is_service=True,
        )

    async def _resolve_team_agent_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        run_id: str,
    ) -> Principal:
        team_id = principal_id[len(TEAM_AGENT_PREFIX) :]
        if (
            not _ID.fullmatch(tenant_id)
            or not _ID.fullmatch(team_id)
            or team_id != tenant_id
        ):
            raise IntegrityError("Team Agent owner identity does not match its tenant")
        with self.engine.connect() as connection:
            binding = (
                connection.execute(
                    select(PROJECT_AGENT_RUNS.c.project_id).select_from(
                        PROJECT_AGENT_RUNS.join(
                            TEAM_PROJECT_AGENTS,
                            and_(
                                TEAM_PROJECT_AGENTS.c.agent_id
                                == PROJECT_AGENT_RUNS.c.team_agent_id,
                                TEAM_PROJECT_AGENTS.c.project_id
                                == PROJECT_AGENT_RUNS.c.project_id,
                                TEAM_PROJECT_AGENTS.c.team_id
                                == PROJECT_AGENT_RUNS.c.team_id,
                            ),
                        ).join(
                            PROJECT_TEAMS,
                            and_(
                                PROJECT_TEAMS.c.project_id
                                == PROJECT_AGENT_RUNS.c.project_id,
                                PROJECT_TEAMS.c.team_id
                                == PROJECT_AGENT_RUNS.c.team_id,
                            ),
                        )
                    ).where(
                        and_(
                            PROJECT_AGENT_RUNS.c.run_id == run_id,
                            PROJECT_AGENT_RUNS.c.team_id == team_id,
                            PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
                            PROJECT_AGENT_RUNS.c.executed_as_principal_id == principal_id,
                            TEAM_PROJECT_AGENTS.c.status == "active",
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if binding is None:
            raise PolicyDenied("Team Agent run has no active delegation binding")
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            roles=frozenset({"team_agent"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset({f"project:{binding['project_id']}"}),
            is_service=True,
        )


__all__ = [
    "ORCHESTRATOR_PRINCIPAL_ID",
    "SPECIALIST_AGENT_PREFIX",
    "TEAM_AGENT_PREFIX",
    "TeamAgentPrincipalResolver",
    "project_orchestrator_principal",
]
