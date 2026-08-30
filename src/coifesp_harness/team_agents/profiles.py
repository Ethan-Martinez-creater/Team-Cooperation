"""Resolve pinned Team Agent profiles into immutable runtime policy snapshots.

Profiles select existing, versioned runtime authorizations. They are not a
business-capability catalog and never consult the dynamic capability registry.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection, Engine

from ..errors import PolicyDenied
from ..product.repository import PROJECT_TEAMS, TEAM_AGENT_PROFILES, TEAM_PROJECT_AGENTS
from ..runtime.models import (
    AuthorizedSkill,
    AuthorizedTool,
    ModelCapability,
    ModelRoutePolicy,
    RunBudget,
    ToolAuthorization,
)
from ..security import Classification, Principal
from .identity import TEAM_AGENT_PREFIX

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUDGET_FIELDS = frozenset(item.name for item in fields(RunBudget))


@dataclass(frozen=True, slots=True)
class ContextScope:
    """The only project/team memory domain delegated to this execution."""

    project_id: str
    team_id: str

    def __post_init__(self) -> None:
        if not _valid_identifier(self.project_id) or not _valid_identifier(self.team_id):
            raise ValueError("Team Agent context scope is invalid")


@dataclass(frozen=True, slots=True)
class ResolvedTeamAgentRuntime:
    principal: Principal
    tool_authorization: ToolAuthorization
    model_route_policy: ModelRoutePolicy
    budget: RunBudget
    context_scope: ContextScope
    delegation_scope_digest: str
    profile_id: str
    profile_version: int


class TeamAgentCapabilityResolver:
    """Resolve one active project delegation using its exact profile version.

    Policy maps are trusted runtime configuration, not user-authored capability
    facts. Their values are copied and frozen at construction. Tools and skills
    are selected independently so a tool policy cannot implicitly grant skills.
    The built-in ``default`` policies deny all tools and skills, use the default
    model route, and restrict context to the current project and team. A caller
    may explicitly replace the default tool, skill, or model policy via a map.
    Memory policies other than ``default`` are not supported and fail closed.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        tool_policies: Mapping[str, tuple[AuthorizedTool, ...]] | None = None,
        skill_policies: Mapping[str, tuple[AuthorizedSkill, ...]] | None = None,
        model_policies: Mapping[str, ModelRoutePolicy] | None = None,
    ) -> None:
        self.engine = engine
        tools = {"default": ()}
        skills = {"default": ()}
        models = {"default": ModelRoutePolicy()}
        for policy_id, values in (tool_policies or {}).items():
            _require_policy_id(policy_id)
            tools[policy_id] = _tool_snapshot(values)
        for policy_id, values in (skill_policies or {}).items():
            _require_policy_id(policy_id)
            skills[policy_id] = _skill_snapshot(values)
        for policy_id, value in (model_policies or {}).items():
            _require_policy_id(policy_id)
            models[policy_id] = _model_snapshot(value)
        self._tool_policies = MappingProxyType(tools)
        self._skill_policies = MappingProxyType(skills)
        self._model_policies = MappingProxyType(models)

    def resolve(
        self,
        *,
        agent_id: str,
        project_id: str,
        connection: Connection | None = None,
    ) -> ResolvedTeamAgentRuntime:
        if not _valid_identifier(agent_id) or not _valid_identifier(project_id):
            raise PolicyDenied("Team Agent delegation identifiers are invalid")
        if connection is not None:
            # The dispatcher's transaction owns this connection and its lifetime.
            # Do not start, commit, roll back, or open another transaction here.
            return self._resolve(connection, agent_id=agent_id, project_id=project_id)
        with self.engine.connect() as owned_connection:
            return self._resolve(owned_connection, agent_id=agent_id, project_id=project_id)

    def _resolve(
        self, connection: Connection, *, agent_id: str, project_id: str
    ) -> ResolvedTeamAgentRuntime:
        agent = (
            connection.execute(
                select(TEAM_PROJECT_AGENTS).where(TEAM_PROJECT_AGENTS.c.agent_id == agent_id)
            )
            .mappings()
            .first()
        )
        if agent is None or agent["project_id"] != project_id or agent["status"] != "active":
            raise PolicyDenied("Team Agent has no active delegation to this project")
        team_id = agent["team_id"]
        if not _valid_identifier(team_id):
            raise PolicyDenied("Team Agent delegation team is invalid")
        participation = connection.execute(
            select(PROJECT_TEAMS.c.team_id).where(
                and_(PROJECT_TEAMS.c.project_id == project_id, PROJECT_TEAMS.c.team_id == team_id)
            )
        ).first()
        if participation is None:
            raise PolicyDenied("Team Agent team does not participate in this project")

        profile_id = agent["profile_id"]
        profile_version = agent["profile_version"]
        if not _valid_identifier(profile_id) or not _positive_integer(profile_version):
            raise PolicyDenied("Team Agent profile pin is invalid")
        profile = (
            connection.execute(
                select(TEAM_AGENT_PROFILES).where(
                    and_(
                        TEAM_AGENT_PROFILES.c.profile_id == profile_id,
                        TEAM_AGENT_PROFILES.c.version == profile_version,
                    )
                )
            )
            .mappings()
            .first()
        )
        if profile is None or profile["team_id"] != team_id:
            raise PolicyDenied("Team Agent pinned profile is absent or belongs to another team")

        policy_ids = {
            name: profile[f"{name}_policy_id"] for name in ("tool", "skill", "model", "memory")
        }
        if any(not _valid_identifier(value) for value in policy_ids.values()):
            raise PolicyDenied("Team Agent profile contains an invalid policy identifier")
        try:
            authorization = ToolAuthorization(
                tools=self._tool_policies[policy_ids["tool"]],
                skills=self._skill_policies[policy_ids["skill"]],
            )
            model = self._model_policies[policy_ids["model"]]
        except KeyError as exc:
            raise PolicyDenied("Team Agent profile references an unknown runtime policy") from exc
        if policy_ids["memory"] != "default":
            raise PolicyDenied("Team Agent profile references an unknown memory policy")
        autonomy_level = profile["autonomy_level"]
        if autonomy_level not in {"supervised", "bounded", "autonomous"}:
            raise PolicyDenied("Team Agent profile autonomy level is invalid")
        budget = _run_budget(profile["max_run_budget_profile"])
        scope = ContextScope(project_id=project_id, team_id=team_id)
        principal = Principal(
            principal_id=f"{TEAM_AGENT_PREFIX}{team_id}",
            tenant_id=team_id,
            roles=frozenset({"team_agent"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset({f"project:{project_id}"}),
            is_service=True,
        )
        digest_payload = {
            "schema": "team-agent-runtime-scope.v1",
            "agent_id": agent_id,
            "project_id": project_id,
            "team_id": team_id,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "policy_ids": policy_ids,
            "autonomy_level": autonomy_level,
            "principal": {
                "principal_id": principal.principal_id,
                "tenant_id": principal.tenant_id,
                "roles": sorted(principal.roles),
                "clearance": int(principal.clearance),
                "compartments": sorted(principal.compartments),
                "is_service": principal.is_service,
            },
            "context_scope": {"project_id": scope.project_id, "team_id": scope.team_id},
            "tool_authorization": {
                "tools": [
                    {
                        "tool_id": item.tool_id,
                        "version": item.version,
                        "schema_digest": item.schema_digest,
                    }
                    for item in authorization.tools
                ],
                "skills": [
                    {
                        "name": item.name,
                        "version": item.version,
                        "content_digest": item.content_digest,
                    }
                    for item in authorization.skills
                ],
                "catalog_digest": authorization.catalog_digest,
            },
            "model_route_policy": {
                "data_classification": int(model.data_classification),
                "required_capabilities": sorted(item.value for item in model.required_capabilities),
                "allowed_provider_ids": sorted(model.allowed_provider_ids),
                "residency_regions": sorted(model.residency_regions),
                "allow_external_egress": model.allow_external_egress,
                "max_call_cost_microusd": model.max_call_cost_microusd,
                "max_output_tokens": model.max_output_tokens,
                "max_call_total_tokens": model.max_call_total_tokens,
            },
            "budget": {name: getattr(budget, name) for name in sorted(_BUDGET_FIELDS)},
        }
        digest = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ResolvedTeamAgentRuntime(
            principal=principal,
            tool_authorization=authorization,
            model_route_policy=model,
            budget=budget,
            context_scope=scope,
            delegation_scope_digest=digest,
            profile_id=profile_id,
            profile_version=profile_version,
        )


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _require_policy_id(value: object) -> None:
    if not _valid_identifier(value):
        raise ValueError("runtime policy identifier is invalid")


def _require_binding(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime authorization requires nonempty versioned bindings")


def _tool_snapshot(values: tuple[AuthorizedTool, ...]) -> tuple[AuthorizedTool, ...]:
    result = []
    for item in values:
        if not isinstance(item, AuthorizedTool):
            raise ValueError("tool policies must contain runtime AuthorizedTool bindings")
        for value in (item.tool_id, item.version, item.schema_digest):
            _require_binding(value)
        result.append(AuthorizedTool(item.tool_id, item.version, item.schema_digest))
    if len({item.tool_id for item in result}) != len(result):
        raise ValueError("tool policy contains duplicate tool IDs")
    return tuple(sorted(result, key=lambda item: item.tool_id))


def _skill_snapshot(values: tuple[AuthorizedSkill, ...]) -> tuple[AuthorizedSkill, ...]:
    result = []
    for item in values:
        if not isinstance(item, AuthorizedSkill):
            raise ValueError("skill policies must contain runtime AuthorizedSkill bindings")
        for value in (item.name, item.version, item.content_digest):
            _require_binding(value)
        result.append(AuthorizedSkill(item.name, item.version, item.content_digest))
    if len({item.name for item in result}) != len(result):
        raise ValueError("skill policy contains duplicate skill names")
    return tuple(sorted(result, key=lambda item: item.name))


def _model_snapshot(value: ModelRoutePolicy) -> ModelRoutePolicy:
    if not isinstance(value, ModelRoutePolicy):
        raise ValueError("model policies must contain runtime ModelRoutePolicy values")
    if not isinstance(value.data_classification, Classification):
        raise ValueError("model policy classification is invalid")
    if type(value.allow_external_egress) is not bool:
        raise ValueError("model policy egress flag must be boolean")
    if any(not isinstance(item, ModelCapability) for item in value.required_capabilities):
        raise ValueError("model policy capabilities are invalid")
    for values in (value.allowed_provider_ids, value.residency_regions):
        if isinstance(values, str) or any(not _valid_identifier(item) for item in values):
            raise ValueError("model policy identifiers are invalid")
    for ceiling in (
        value.max_call_cost_microusd,
        value.max_output_tokens,
        value.max_call_total_tokens,
    ):
        if ceiling is not None and not _positive_integer(ceiling):
            raise ValueError("model policy ceilings must be positive integers")
    return ModelRoutePolicy(
        data_classification=value.data_classification,
        required_capabilities=frozenset(value.required_capabilities),
        allowed_provider_ids=frozenset(value.allowed_provider_ids),
        residency_regions=frozenset(value.residency_regions),
        allow_external_egress=value.allow_external_egress,
        max_call_cost_microusd=value.max_call_cost_microusd,
        max_output_tokens=value.max_output_tokens,
        max_call_total_tokens=value.max_call_total_tokens,
    )


def _run_budget(value: object) -> RunBudget:
    if not isinstance(value, Mapping) or set(value) - _BUDGET_FIELDS:
        raise PolicyDenied("Team Agent budget contains unknown runtime fields")
    if any(not _positive_integer(limit) for limit in value.values()):
        raise PolicyDenied("Team Agent budget limits must be positive integers")
    return RunBudget(**value)


__all__ = ["ContextScope", "ResolvedTeamAgentRuntime", "TeamAgentCapabilityResolver"]
