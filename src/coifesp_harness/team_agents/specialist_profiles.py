"""Server-owned, bounded profiles for Specialist Agent delegation.

Specialists are deliberately configuration objects, not a second free-form
agent surface.  A profile fixes the purpose, context boundary, tools, model
route, budget, and JSON result contract.  Callers select a profile kind; they
cannot provide any of those values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from ..context.models import ContextItem, ContextSource, InstructionTrust
from ..errors import PolicyDenied
from ..runtime.models import (
    AuthorizedTool,
    ModelCapability,
    ModelRoutePolicy,
    RunBudget,
    ToolAuthorization,
)
from ..security import Classification, Principal
from ..tool_catalog import project_context_manifests

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

# This name is reserved for the future delegation executor.  It must never
# be admitted to a specialist even if a caller adds it to the parent set.
SPECIALIST_DELEGATION_TOOL_ID = "specialist.delegate"
SPECIALIST_DELEGATION_TOOL_NAMES = frozenset(
    {SPECIALIST_DELEGATION_TOOL_ID, "delegate_specialist"}
)


class SpecialistKind(str, Enum):
    """The only specialist capabilities supported by this release."""

    CODE_REVIEW = "code_review"
    TEST_ANALYSIS = "test_analysis"
    SECURITY_REVIEW = "security_review"

    @classmethod
    def parse(cls, value: object) -> SpecialistKind:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise PolicyDenied("specialist kind must be a supported string")
        try:
            return cls(value)
        except ValueError as exc:
            raise PolicyDenied(f"unsupported specialist kind: {value}") from exc


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _freeze(value: Any) -> Any:
    """Recursively freeze JSON-like catalog data."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        _thaw(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_payload(tool: AuthorizedTool) -> dict[str, str]:
    return {
        "tool_id": tool.tool_id,
        "version": tool.version,
        "schema_digest": tool.schema_digest,
    }


@dataclass(frozen=True, slots=True)
class SpecialistContextPolicy:
    """The fixed context boundary of one specialist profile."""

    allowed_sources: frozenset[ContextSource]
    max_items: int
    max_chars_per_item: int
    max_total_chars: int
    classification: Classification = Classification.INTERNAL

    def __post_init__(self) -> None:
        sources: frozenset[ContextSource]
        try:
            sources = frozenset(
                source if isinstance(source, ContextSource) else ContextSource(source)
                for source in self.allowed_sources
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("specialist context sources are invalid") from exc
        if not sources:
            raise ValueError("specialist context sources cannot be empty")
        if type(self.max_items) is not int or self.max_items <= 0:
            raise ValueError("specialist context max_items must be positive")
        if type(self.max_chars_per_item) is not int or self.max_chars_per_item <= 0:
            raise ValueError("specialist context max_chars_per_item must be positive")
        if type(self.max_total_chars) is not int or self.max_total_chars <= 0:
            raise ValueError("specialist context max_total_chars must be positive")
        if self.max_total_chars < self.max_chars_per_item:
            raise ValueError("specialist context total limit cannot be narrower than item limit")
        if self.classification is not Classification.INTERNAL:
            raise ValueError("specialist context classification must be INTERNAL")
        object.__setattr__(self, "allowed_sources", sources)

    def validate(self, items: Iterable[ContextItem]) -> tuple[ContextItem, ...]:
        """Validate and return the exact selected context without widening it."""

        selected = tuple(items)
        if len(selected) > self.max_items:
            raise PolicyDenied("specialist context item limit exceeded")
        total_chars = 0
        for item in selected:
            source = item.source
            if not isinstance(source, ContextSource):
                try:
                    source = ContextSource(source)
                except (TypeError, ValueError) as exc:
                    raise PolicyDenied("specialist context source is invalid") from exc
            if source not in self.allowed_sources:
                raise PolicyDenied("specialist context source is outside the profile scope")
            content = getattr(item, "content", None)
            if not isinstance(content, str) or not content:
                raise PolicyDenied("specialist context content is invalid")
            chars = len(content.encode("utf-8"))
            if chars > self.max_chars_per_item:
                raise PolicyDenied("specialist context item character limit exceeded")
            total_chars += chars
        if total_chars > self.max_total_chars:
            raise PolicyDenied("specialist context total character limit exceeded")
        return selected


@dataclass(frozen=True, slots=True)
class SpecialistContextScope:
    """The project/team scope in which a specialist may execute."""

    project_id: str
    team_id: str
    classification: Classification = Classification.INTERNAL

    def __post_init__(self) -> None:
        if not _valid_identifier(self.project_id) or not _valid_identifier(self.team_id):
            raise ValueError("specialist project/team scope is invalid")
        if self.classification is not Classification.INTERNAL:
            raise PolicyDenied("specialist project scope must be INTERNAL")


@dataclass(frozen=True, slots=True)
class SpecialistProfile:
    """Immutable server-owned execution profile."""

    profile_id: str
    version: int
    kind: SpecialistKind
    purpose: str
    context_policy: SpecialistContextPolicy
    tools: tuple[AuthorizedTool, ...]
    model_route_policy: ModelRoutePolicy
    budget: RunBudget
    output_schema: Mapping[str, Any]
    profile_digest: str

    def __post_init__(self) -> None:
        if not _valid_identifier(self.profile_id) or type(self.version) is not int or self.version <= 0:
            raise ValueError("specialist profile identity is invalid")
        kind = SpecialistKind.parse(self.kind)
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise ValueError("specialist purpose is required")
        tools = tuple(self.tools)
        _validate_tool_bindings(tools)
        if any(tool.tool_id in SPECIALIST_DELEGATION_TOOL_NAMES for tool in tools):
            raise ValueError("specialist delegation tool is not allowed in a specialist profile")
        if not isinstance(self.model_route_policy, ModelRoutePolicy):
            raise TypeError("specialist model route is invalid")
        if self.model_route_policy.data_classification is not Classification.INTERNAL:
            raise ValueError("specialist model route must remain INTERNAL")
        if not isinstance(self.budget, RunBudget):
            raise TypeError("specialist budget is invalid")
        schema = _freeze(self.output_schema)
        if not isinstance(schema, Mapping):
            raise TypeError("specialist output schema must be a JSON object")
        try:
            Draft202012Validator.check_schema(_thaw(schema))
        except Exception as exc:  # jsonschema uses several schema error types
            raise ValueError("specialist output schema is invalid") from exc
        if not isinstance(self.profile_digest, str) or not _DIGEST.fullmatch(self.profile_digest):
            raise ValueError("specialist profile digest is invalid")
        object.__setattr__(self, "tools", tuple(sorted(tools, key=lambda item: item.tool_id)))
        object.__setattr__(self, "output_schema", schema)

    @property
    def digest(self) -> str:
        """Short alias used by run bindings."""

        return self.profile_digest

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(item.tool_id for item in self.tools)

    def validate_output(self, output: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(output, Mapping):
            raise PolicyDenied("specialist output must be a JSON object")
        try:
            Draft202012Validator(_thaw(self.output_schema)).validate(dict(output))
        except ValidationError as exc:
            raise PolicyDenied(f"specialist output does not match its fixed schema: {exc.message}") from exc
        return output

    def validate_context(
        self,
        items: Iterable[ContextItem],
        *,
        scope: SpecialistContextScope | None = None,
    ) -> tuple[ContextItem, ...]:
        if scope is not None and scope.classification is not Classification.INTERNAL:
            raise PolicyDenied("specialist context scope must be INTERNAL")
        selected = self.context_policy.validate(items)
        if scope is not None:
            project_compartment = frozenset({f"project:{scope.project_id}"})
            for item in selected:
                if (
                    item.label.owner_tenant_id != scope.team_id
                    or item.label.classification is not Classification.INTERNAL
                    or item.label.compartments != project_compartment
                    or item.instruction_trust is not InstructionTrust.DATA_ONLY
                ):
                    raise PolicyDenied(
                        "specialist context item is outside the fixed project/team data scope"
                    )
        return selected

    def derive_authorization(
        self,
        parent_authorization: ToolAuthorization | Iterable[AuthorizedTool] | object,
        *,
        project_id: str | None = None,
        team_id: str | None = None,
        project_classification: Classification = Classification.INTERNAL,
        scope: SpecialistContextScope | None = None,
        context_items: Iterable[ContextItem] = (),
    ) -> SpecialistAuthorization:
        """Derive a child authorization from a parent Team Agent snapshot.

        Every profile tool must have the same version and schema binding in
        the parent.  The resulting set must be a *proper* subset, so a parent
        cannot delegate its complete authority unchanged.  Skills and the
        reserved delegation tool are never propagated.
        """

        resolved_scope = _resolve_scope(
            project_id=project_id,
            team_id=team_id,
            project_classification=project_classification,
            scope=scope,
        )
        selected_context = self.validate_context(context_items, scope=resolved_scope)
        parent_tools = _parent_tools(parent_authorization)
        _validate_tool_bindings(parent_tools)
        parent_by_id = {item.tool_id: item for item in parent_tools}
        child_ids = self.allowed_tools
        if child_ids & SPECIALIST_DELEGATION_TOOL_NAMES:
            raise PolicyDenied("specialist delegation recursion is denied")
        if not child_ids < set(parent_by_id):
            raise PolicyDenied("specialist tools must be a strict subset of parent tools")
        child_tools: list[AuthorizedTool] = []
        for required in self.tools:
            parent = parent_by_id.get(required.tool_id)
            if parent is None or parent != required:
                raise PolicyDenied("parent tool binding does not match specialist catalog")
            child_tools.append(required)
        principal = Principal(
            principal_id=f"specialist-agent:{resolved_scope.team_id}:{self.kind.value}",
            tenant_id=resolved_scope.team_id,
            roles=frozenset({"specialist_agent"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset({f"project:{resolved_scope.project_id}"}),
            is_service=True,
        )
        authorization = ToolAuthorization(tools=tuple(child_tools), skills=())
        return SpecialistAuthorization(
            profile=self,
            principal=principal,
            tool_authorization=authorization,
            model_route_policy=self.model_route_policy,
            budget=self.budget,
            context_scope=resolved_scope,
            context_items=selected_context,
            delegation_scope_digest=self.profile_digest,
        )


@dataclass(frozen=True, slots=True)
class SpecialistAuthorization:
    """Immutable child authorization produced by a profile."""

    profile: SpecialistProfile
    principal: Principal
    tool_authorization: ToolAuthorization
    model_route_policy: ModelRoutePolicy
    budget: RunBudget
    context_scope: SpecialistContextScope
    context_items: tuple[ContextItem, ...]
    delegation_scope_digest: str

    @property
    def kind(self) -> SpecialistKind:
        return self.profile.kind

    @property
    def output_schema(self) -> Mapping[str, Any]:
        return self.profile.output_schema


def _validate_tool_bindings(tools: Iterable[AuthorizedTool]) -> None:
    values = tuple(tools)
    for item in values:
        if not isinstance(item, AuthorizedTool):
            raise TypeError("specialist tools must be AuthorizedTool bindings")
        if not _valid_identifier(item.tool_id) or not item.version or not _DIGEST.fullmatch(item.schema_digest):
            raise ValueError("specialist tool binding is invalid")
    if len({item.tool_id for item in values}) != len(values):
        raise ValueError("specialist tools contain duplicate tool IDs")


def _parent_tools(value: object) -> tuple[AuthorizedTool, ...]:
    if isinstance(value, ToolAuthorization):
        return tuple(value.tools)
    authorization = getattr(value, "tool_authorization", None)
    if isinstance(authorization, ToolAuthorization):
        return tuple(authorization.tools)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return tuple(value)
    raise PolicyDenied("parent Team Agent tool authorization is required")


def _resolve_scope(
    *,
    project_id: str | None,
    team_id: str | None,
    project_classification: Classification,
    scope: SpecialistContextScope | None,
) -> SpecialistContextScope:
    if scope is not None:
        if project_id is not None and project_id != scope.project_id:
            raise PolicyDenied("specialist project scope conflicts with the requested project")
        if team_id is not None and team_id != scope.team_id:
            raise PolicyDenied("specialist team scope conflicts with the requested team")
        if project_classification is not Classification.INTERNAL:
            raise PolicyDenied("specialist project scope must be INTERNAL")
        return scope
    if project_id is None or team_id is None:
        raise PolicyDenied("specialist project and team scope are required")
    return SpecialistContextScope(project_id, team_id, project_classification)


def _finding_schema(*, include_location: bool = True) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
        "issue": {"type": "string", "minLength": 1, "maxLength": 4000},
        "recommendation": {"type": "string", "minLength": 1, "maxLength": 4000},
    }
    required = ["severity", "issue", "recommendation"]
    if include_location:
        properties.update(
            {
                "file": {"type": "string", "minLength": 1, "maxLength": 1024},
                "line": {"type": "integer", "minimum": 1},
            }
        )
        required.extend(["file", "line"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _output_schema(kind: SpecialistKind) -> dict[str, Any]:
    base = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    if kind is SpecialistKind.CODE_REVIEW:
        base.update(
            {
                "properties": {
                    "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "verdict": {"type": "string", "enum": ["pass", "changes_requested", "block"]},
                    "findings": {"type": "array", "items": _finding_schema(), "maxItems": 100},
                },
                "required": ["summary", "verdict", "findings"],
                "additionalProperties": False,
            }
        )
    elif kind is SpecialistKind.TEST_ANALYSIS:
        base.update(
            {
                "properties": {
                    "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "verdict": {"type": "string", "enum": ["pass", "changes_requested", "block"]},
                    "test_command": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "tests_run": {"type": "integer", "minimum": 0},
                    "failures": {
                        "type": "array",
                        "items": _finding_schema(),
                        "maxItems": 100,
                    },
                },
                "required": ["summary", "verdict", "test_command", "tests_run", "failures"],
                "additionalProperties": False,
            }
        )
    else:
        base.update(
            {
                "properties": {
                    "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "verdict": {"type": "string", "enum": ["pass", "changes_requested", "block"]},
                    "risk_rating": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "findings": {
                        "type": "array",
                        "items": _finding_schema(include_location=False),
                        "maxItems": 100,
                    },
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "maxItems": 100,
                    },
                },
                "required": ["summary", "verdict", "risk_rating", "findings", "recommendations"],
                "additionalProperties": False,
            }
        )
    return base


def _model_payload(policy: ModelRoutePolicy) -> dict[str, Any]:
    return {
        "data_classification": int(policy.data_classification),
        "required_capabilities": sorted(capability.value for capability in policy.required_capabilities),
        "allowed_provider_ids": sorted(policy.allowed_provider_ids),
        "residency_regions": sorted(policy.residency_regions),
        "allow_external_egress": policy.allow_external_egress,
        "max_call_cost_microusd": policy.max_call_cost_microusd,
        "max_output_tokens": policy.max_output_tokens,
        "max_call_total_tokens": policy.max_call_total_tokens,
    }


def _budget_payload(budget: RunBudget) -> dict[str, int]:
    return {
        "max_turns": budget.max_turns,
        "max_tool_calls": budget.max_tool_calls,
        "max_total_tokens": budget.max_total_tokens,
        "max_model_cost_microusd": budget.max_model_cost_microusd,
    }


def _profile(
    *,
    kind: SpecialistKind,
    purpose: str,
    context_policy: SpecialistContextPolicy,
    budget: RunBudget,
) -> SpecialistProfile:
    manifests = {manifest.tool_id: manifest for manifest in project_context_manifests()}
    tools = tuple(
        AuthorizedTool(name, manifests[name].version, manifests[name].schema_digest)
        for name in sorted(manifests)
    )
    model = ModelRoutePolicy(
        data_classification=Classification.INTERNAL,
        required_capabilities=frozenset({ModelCapability.JSON_OUTPUT}),
        allow_external_egress=False,
        max_output_tokens=4096,
        max_call_total_tokens=budget.max_total_tokens,
    )
    schema = _output_schema(kind)
    digest_payload = {
        "schema": "coifesp.specialist-profile.v1",
        "profile_id": f"specialist.{kind.value}",
        "version": 1,
        "kind": kind.value,
        "purpose": purpose,
        "context_policy": {
            "allowed_sources": sorted(source.value for source in context_policy.allowed_sources),
            "max_items": context_policy.max_items,
            "max_chars_per_item": context_policy.max_chars_per_item,
            "max_total_chars": context_policy.max_total_chars,
            "classification": int(context_policy.classification),
        },
        "tools": [_tool_payload(tool) for tool in tools],
        "model_route": _model_payload(model),
        "budget": _budget_payload(budget),
        "output_schema": schema,
    }
    return SpecialistProfile(
        profile_id=f"specialist.{kind.value}",
        version=1,
        kind=kind,
        purpose=purpose,
        context_policy=context_policy,
        tools=tools,
        model_route_policy=model,
        budget=budget,
        output_schema=schema,
        profile_digest=_canonical_digest(digest_payload),
    )


_COMMON_SOURCES = frozenset(
    {ContextSource.DOCUMENT, ContextSource.GOVERNANCE, ContextSource.TOOL}
)

_SPECIALIST_PROFILES = MappingProxyType(
    {
        SpecialistKind.CODE_REVIEW: _profile(
            kind=SpecialistKind.CODE_REVIEW,
            purpose="Review the selected project code and report actionable defects.",
            context_policy=SpecialistContextPolicy(_COMMON_SOURCES, 32, 50_000, 300_000),
            budget=RunBudget(max_turns=3, max_tool_calls=6, max_total_tokens=12_000, max_model_cost_microusd=1_000_000),
        ),
        SpecialistKind.TEST_ANALYSIS: _profile(
            kind=SpecialistKind.TEST_ANALYSIS,
            purpose="Analyze selected project tests and evidence for coverage and failures.",
            context_policy=SpecialistContextPolicy(_COMMON_SOURCES, 64, 50_000, 300_000),
            budget=RunBudget(max_turns=4, max_tool_calls=8, max_total_tokens=16_000, max_model_cost_microusd=1_500_000),
        ),
        SpecialistKind.SECURITY_REVIEW: _profile(
            kind=SpecialistKind.SECURITY_REVIEW,
            purpose="Review selected project context for bounded security risks and mitigations.",
            context_policy=SpecialistContextPolicy(_COMMON_SOURCES, 48, 50_000, 300_000),
            budget=RunBudget(max_turns=4, max_tool_calls=8, max_total_tokens=16_000, max_model_cost_microusd=1_500_000),
        ),
    }
)

# Public read-only view; profile objects and nested schema/context policy are
# immutable, and the map itself rejects writes.
SPECIALIST_PROFILE_CATALOG = _SPECIALIST_PROFILES


def compile_specialist_profile(kind: SpecialistKind | str) -> SpecialistProfile:
    """Compile a kind into the server-owned immutable profile."""

    parsed = SpecialistKind.parse(kind)
    try:
        return _SPECIALIST_PROFILES[parsed]
    except KeyError as exc:  # defensive: the enum and catalog must stay closed
        raise PolicyDenied(f"specialist kind is not in the server catalog: {parsed.value}") from exc


def parse_specialist_profile(value: SpecialistKind | str | Mapping[str, Any]) -> SpecialistProfile:
    """Parse a kind selector; profile fields supplied by callers are rejected."""

    if isinstance(value, Mapping):
        if set(value) != {"kind"}:
            raise PolicyDenied("specialist profile fields are server-owned and cannot be overridden")
        value = value["kind"]
    return compile_specialist_profile(value)


def get_specialist_profile(kind: SpecialistKind | str) -> SpecialistProfile:
    """Compatibility alias for callers resolving a profile by kind."""

    return compile_specialist_profile(kind)


def derive_specialist_authorization(
    kind: SpecialistKind | str,
    parent_authorization: ToolAuthorization | Iterable[AuthorizedTool] | object,
    *,
    project_id: str | None = None,
    team_id: str | None = None,
    project_classification: Classification = Classification.INTERNAL,
    scope: SpecialistContextScope | None = None,
    context_items: Iterable[ContextItem] = (),
) -> SpecialistAuthorization:
    """Resolve a catalog profile and derive its bounded child authorization."""

    return compile_specialist_profile(kind).derive_authorization(
        parent_authorization,
        project_id=project_id,
        team_id=team_id,
        project_classification=project_classification,
        scope=scope,
        context_items=context_items,
    )


__all__ = [
    "SPECIALIST_DELEGATION_TOOL_ID",
    "SPECIALIST_DELEGATION_TOOL_NAMES",
    "SPECIALIST_PROFILE_CATALOG",
    "SpecialistAuthorization",
    "SpecialistContextPolicy",
    "SpecialistContextScope",
    "SpecialistKind",
    "SpecialistProfile",
    "compile_specialist_profile",
    "derive_specialist_authorization",
    "get_specialist_profile",
    "parse_specialist_profile",
]
