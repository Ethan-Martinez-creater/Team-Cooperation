from __future__ import annotations

import pytest

from coifesp_harness.context import ContextItem, ContextSource
from coifesp_harness.errors import PolicyDenied
from coifesp_harness.runtime import AuthorizedTool, ToolAuthorization
from coifesp_harness.security import Classification, ResourceLabel
from coifesp_harness.team_agents.specialist_profiles import (
    SPECIALIST_DELEGATION_TOOL_ID,
    SPECIALIST_PROFILE_CATALOG,
    SpecialistContextScope,
    SpecialistKind,
    compile_specialist_profile,
    derive_specialist_authorization,
    parse_specialist_profile,
)
from coifesp_harness.tool_catalog import project_context_manifests


def _parent_authorization(*, extra: tuple[AuthorizedTool, ...] = ()) -> ToolAuthorization:
    tools = tuple(
        AuthorizedTool(item.tool_id, item.version, item.schema_digest)
        for item in project_context_manifests()
    )
    return ToolAuthorization(tools=tools + extra)


def _context(
    *, source: ContextSource = ContextSource.DOCUMENT, content: str = "selected code"
) -> ContextItem:
    return ContextItem(
        item_id="document:source-1",
        content=content,
        source=source,
        source_id="source-1",
        label=ResourceLabel(
            owner_tenant_id="team-a",
            classification=Classification.INTERNAL,
            compartments=frozenset({"project:project-a"}),
            resource_id="project-a:source-1",
        ),
    )


def test_catalog_contains_only_the_three_server_owned_profiles():
    assert set(SPECIALIST_PROFILE_CATALOG) == set(SpecialistKind)
    for kind in SpecialistKind:
        profile = compile_specialist_profile(kind)
        assert profile.kind is kind
        assert profile.profile_digest == profile.digest
        assert profile.output_schema["type"] == "object"
        assert profile.output_schema["additionalProperties"] is False
        assert profile.output_schema["required"]
        assert SPECIALIST_DELEGATION_TOOL_ID not in profile.allowed_tools


def test_profile_digest_is_stable_and_covers_the_fixed_contract():
    first = compile_specialist_profile(SpecialistKind.CODE_REVIEW)
    second = compile_specialist_profile("code_review")
    assert first is second
    assert first.digest == second.digest
    assert len(first.digest) == 64
    assert first.digest != compile_specialist_profile(SpecialistKind.TEST_ANALYSIS).digest
    with pytest.raises(TypeError):
        first.output_schema["new_field"] = {}
    with pytest.raises(TypeError):
        SPECIALIST_PROFILE_CATALOG["new_kind"] = first


def test_profile_selector_rejects_unknown_kind_and_caller_overrides():
    with pytest.raises(PolicyDenied, match="unsupported specialist kind"):
        compile_specialist_profile("free_chat")
    with pytest.raises(PolicyDenied, match="server-owned"):
        parse_specialist_profile(
            {
                "kind": "code_review",
                "purpose": "caller supplied purpose",
            }
        )
    with pytest.raises(PolicyDenied, match="server-owned"):
        parse_specialist_profile({"kind": "code_review", "budget": {}})


def test_derived_authorization_is_a_strict_parent_tool_subset_and_has_fixed_identity():
    profile = compile_specialist_profile(SpecialistKind.CODE_REVIEW)
    extra = AuthorizedTool("project.extra_read", "1", "a" * 64)
    derived = profile.derive_authorization(
        _parent_authorization(extra=(extra,)),
        project_id="project-a",
        team_id="team-a",
        context_items=(_context(),),
    )
    assert derived.tool_authorization.tools == profile.tools
    assert derived.tool_authorization.allowed_tools < _parent_authorization(
        extra=(extra,)
    ).allowed_tools
    assert derived.principal.principal_id == "specialist-agent:team-a:code_review"
    assert derived.principal.roles == frozenset({"specialist_agent"})
    assert derived.principal.compartments == frozenset({"project:project-a"})
    assert derived.budget == profile.budget
    assert derived.model_route_policy == profile.model_route_policy
    assert derived.delegation_scope_digest == profile.digest


def test_derivation_fails_closed_for_missing_equal_or_mismatched_parent_tools():
    profile = compile_specialist_profile(SpecialistKind.CODE_REVIEW)
    with pytest.raises(PolicyDenied, match="strict subset"):
        profile.derive_authorization(
            _parent_authorization(), project_id="project-a", team_id="team-a"
        )
    with pytest.raises(PolicyDenied, match="strict subset"):
        profile.derive_authorization(
            ToolAuthorization(), project_id="project-a", team_id="team-a"
        )
    mismatched = list(_parent_authorization().tools)
    mismatched[0] = AuthorizedTool(mismatched[0].tool_id, "2", mismatched[0].schema_digest)
    with pytest.raises(PolicyDenied, match="parent tool binding"):
        profile.derive_authorization(
            ToolAuthorization(tools=tuple(mismatched) + (AuthorizedTool("extra", "1", "b" * 64),)),
            project_id="project-a",
            team_id="team-a",
        )


def test_delegation_tool_is_never_propagated_to_child():
    profile = compile_specialist_profile(SpecialistKind.SECURITY_REVIEW)
    parent = _parent_authorization(
        extra=(AuthorizedTool(SPECIALIST_DELEGATION_TOOL_ID, "1", "c" * 64),)
    )
    derived = profile.derive_authorization(
        parent,
        scope=SpecialistContextScope("project-a", "team-a"),
    )
    assert SPECIALIST_DELEGATION_TOOL_ID not in derived.tool_authorization.allowed_tools


def test_context_sources_and_item_total_limits_are_enforced():
    profile = compile_specialist_profile(SpecialistKind.TEST_ANALYSIS)
    assert profile.validate_context((_context(),))
    with pytest.raises(PolicyDenied, match="outside"):
        profile.validate_context((_context(source=ContextSource.USER),))
    too_large = _context(content="x" * (profile.context_policy.max_chars_per_item + 1))
    with pytest.raises(PolicyDenied, match="character limit"):
        profile.validate_context((too_large,))
    many = tuple(
        _context(content="x" * profile.context_policy.max_chars_per_item)
        for _ in range(profile.context_policy.max_items + 1)
    )
    with pytest.raises(PolicyDenied, match="item limit"):
        profile.validate_context(many)
    total = tuple(
        _context(content="x" * profile.context_policy.max_chars_per_item)
        for _ in range(profile.context_policy.max_total_chars // profile.context_policy.max_chars_per_item + 1)
    )
    with pytest.raises(PolicyDenied, match="total character"):
        profile.validate_context(total)


def test_non_internal_project_scope_is_rejected():
    profile = compile_specialist_profile(SpecialistKind.SECURITY_REVIEW)
    with pytest.raises(PolicyDenied, match="must be INTERNAL"):
        SpecialistContextScope("project-a", "team-a", Classification.CONFIDENTIAL)
    with pytest.raises(PolicyDenied, match="must be INTERNAL"):
        profile.derive_authorization(
            _parent_authorization(extra=(AuthorizedTool("extra", "1", "d" * 64),)),
            project_id="project-a",
            team_id="team-a",
            project_classification=Classification.CONFIDENTIAL,
        )


@pytest.mark.parametrize(
    "label",
    [
        ResourceLabel(
            "team-b",
            Classification.INTERNAL,
            frozenset({"project:project-a"}),
            "foreign-owner",
        ),
        ResourceLabel(
            "team-a",
            Classification.CONFIDENTIAL,
            frozenset({"project:project-a"}),
            "confidential",
        ),
        ResourceLabel(
            "team-a",
            Classification.INTERNAL,
            frozenset({"project:project-b"}),
            "other-project",
        ),
    ],
)
def test_derivation_rejects_context_outside_fixed_project_team_scope(label):
    item = _context()
    item = ContextItem(
        item.item_id,
        item.content,
        item.source,
        item.source_id,
        label,
        item.content_trust,
        item.instruction_trust,
        item.priority,
    )
    with pytest.raises(PolicyDenied, match="fixed project/team data scope"):
        derive_specialist_authorization(
            "security_review",
            _parent_authorization(
                extra=(AuthorizedTool("project.extra", "1", "f" * 64),)
            ),
            project_id="project-a",
            team_id="team-a",
            context_items=(item,),
        )


def test_fixed_output_schema_accepts_only_the_profile_contract():
    code = compile_specialist_profile(SpecialistKind.CODE_REVIEW)
    code.validate_output(
        {
            "summary": "looks good",
            "verdict": "pass",
            "findings": [],
        }
    )
    with pytest.raises(PolicyDenied, match="does not match"):
        code.validate_output(
            {
                "summary": "looks good",
                "verdict": "pass",
                "findings": [],
                "caller_override": True,
            }
        )
    security = compile_specialist_profile(SpecialistKind.SECURITY_REVIEW)
    with pytest.raises(PolicyDenied, match="does not match"):
        security.validate_output(
            {
                "summary": "risk",
                "verdict": "block",
                "risk_rating": "critical",
                "findings": [{"severity": "critical", "issue": "x", "recommendation": "y"}],
                "recommendations": [],
                "extra": "not allowed",
            }
        )


def test_top_level_derivation_api_uses_only_catalog_values():
    parent = _parent_authorization(
        extra=(AuthorizedTool("project.extra", "1", "e" * 64),)
    )
    result = derive_specialist_authorization(
        "test_analysis",
        parent,
        project_id="project-a",
        team_id="team-a",
        context_items=(_context(),),
    )
    assert result.kind is SpecialistKind.TEST_ANALYSIS
    assert result.profile.purpose == compile_specialist_profile("test_analysis").purpose
