from dataclasses import FrozenInstanceError, replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import PolicyDenied
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    ProjectWorkspaceService,
    TeamAccountRole,
)
from coifesp_harness.product.repository import (
    PROJECT_TEAMS,
    TEAM_AGENT_PROFILES,
    TEAM_PROJECT_AGENTS,
)
from coifesp_harness.runtime import (
    AuthorizedSkill,
    AuthorizedTool,
    ModelCapability,
    ModelRoutePolicy,
    RunBudget,
    ToolAuthorization,
)
from coifesp_harness.security import Classification
from coifesp_harness.team_agents import ContextScope, TeamAgentCapabilityResolver


@pytest.fixture
def stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    for name in ("a", "b"):
        accounts.register_team(team_id=f"team-{name}", team_handle=f"team-{name}", team_name=name)
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
        description="runtime profile resolution",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    workspace = ProjectWorkspaceService(engine)
    agent = workspace.ensure_team_project_agent(project_id="project-a", team_id="team-a")
    yield engine, agent
    engine.dispose()


def _resolve(stack, resolver=None, **kwargs):
    engine, agent = stack
    resolver = resolver or TeamAgentCapabilityResolver(engine=engine)
    return resolver.resolve(agent_id=agent.agent_id, project_id=agent.project_id, **kwargs)


def _profile_update(engine, **values):
    with engine.begin() as connection:
        connection.execute(
            TEAM_AGENT_PROFILES.update()
            .where(TEAM_AGENT_PROFILES.c.profile_id == "team-a", TEAM_AGENT_PROFILES.c.version == 1)
            .values(**values)
        )


def _agent_update(stack, **values):
    engine, agent = stack
    with engine.begin() as connection:
        connection.execute(
            TEAM_PROJECT_AGENTS.update()
            .where(TEAM_PROJECT_AGENTS.c.agent_id == agent.agent_id)
            .values(**values)
        )


def test_default_profile_resolves_to_explicit_empty_authorization_and_scoped_principal(stack):
    result = _resolve(stack)

    assert result.tool_authorization == ToolAuthorization()
    assert result.tool_authorization is not None
    assert result.model_route_policy == ModelRoutePolicy()
    assert result.budget == RunBudget()
    assert result.context_scope == ContextScope(project_id="project-a", team_id="team-a")
    assert result.profile_id == "team-a"
    assert result.profile_version == 1
    assert result.principal.principal_id == "team-agent:team-a"
    assert result.principal.tenant_id == "team-a"
    assert result.principal.is_service
    assert result.principal.roles == frozenset({"team_agent"})
    assert result.principal.clearance is Classification.INTERNAL
    assert result.principal.compartments == frozenset({"project:project-a"})
    assert len(result.delegation_scope_digest) == 64
    assert int(result.delegation_scope_digest, 16) >= 0
    assert _resolve(stack) == result
    assert hash(result) == hash(_resolve(stack))
    for obj, name, value in (
        (result, "profile_version", 2),
        (result.principal, "tenant_id", "team-b"),
        (result.tool_authorization, "tools", ()),
        (result.model_route_policy, "allow_external_egress", True),
        (result.budget, "max_turns", 2),
        (result.context_scope, "team_id", "team-b"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, name, value)


def test_resolution_uses_exact_profile_version_not_latest_and_pin_changes_digest(stack):
    engine, _ = stack
    original = _resolve(stack)
    with engine.begin() as connection:
        profile = dict(connection.execute(select(TEAM_AGENT_PROFILES)).mappings().one())
        profile.update(version=2, max_run_budget_profile={"max_turns": 7})
        connection.execute(TEAM_AGENT_PROFILES.insert().values(**profile))

    assert _resolve(stack) == original
    _agent_update(stack, profile_version=2)
    current = _resolve(stack)
    assert current.profile_version == 2
    assert current.budget.max_turns == 7
    assert current.delegation_scope_digest != original.delegation_scope_digest


@pytest.mark.parametrize(
    "field", ["tool_policy_id", "skill_policy_id", "model_policy_id", "memory_policy_id"]
)
@pytest.mark.parametrize("value", ["unknown-policy", ""])
def test_unknown_or_invalid_policy_is_denied(stack, field, value):
    _profile_update(stack[0], **{field: value})
    with pytest.raises(PolicyDenied, match="policy"):
        _resolve(stack)


@pytest.mark.parametrize(
    "agent_id,project_id", [("missing", "project-a"), ("existing", "project-b")]
)
def test_missing_agent_and_wrong_project_are_denied(stack, agent_id, project_id):
    engine, agent = stack
    with pytest.raises(PolicyDenied, match="delegation"):
        TeamAgentCapabilityResolver(engine=engine).resolve(
            agent_id=agent.agent_id if agent_id == "existing" else agent_id, project_id=project_id
        )


def test_agent_without_project_team_participation_is_denied(stack):
    _agent_update(stack, team_id="team-b")
    with pytest.raises(PolicyDenied, match="participate"):
        _resolve(stack)


def test_profile_owned_by_another_team_is_denied_even_when_agent_team_participates(stack):
    _profile_update(stack[0], team_id="team-b")
    with pytest.raises(PolicyDenied, match="another team"):
        _resolve(stack)


@pytest.mark.parametrize("pin", [{"profile_id": "missing"}, {"profile_version": 2}])
def test_missing_pinned_profile_is_denied_without_fallback_or_provisioning(stack, pin):
    _agent_update(stack, **pin)
    with pytest.raises(PolicyDenied, match="pinned profile"):
        _resolve(stack)
    with stack[0].connect() as connection:
        assert len(connection.execute(select(TEAM_AGENT_PROFILES)).all()) == 1


@pytest.mark.parametrize("status", ["archived", "paused"])
def test_every_nonactive_agent_status_is_denied(stack, status):
    engine, agent = stack
    with engine.begin() as connection:
        # The current product enum has active/archived only. Simulate a future
        # paused state to verify the resolver is an active-only allowlist.
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            TEAM_PROJECT_AGENTS.update()
            .where(TEAM_PROJECT_AGENTS.c.agent_id == agent.agent_id)
            .values(status=status)
        )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(PolicyDenied, match="active delegation"):
        _resolve(stack)


def test_injected_runtime_policies_resolve_versioned_tools_skills_and_model_limits(stack):
    engine, _ = stack
    _profile_update(
        engine,
        tool_policy_id="read-tools",
        skill_policy_id="analysis-skills",
        model_policy_id="local-model",
        max_run_budget_profile={"max_turns": 4, "max_tool_calls": 8},
    )
    tool = AuthorizedTool("project.read_context", "2", "schema-v2")
    skill = AuthorizedSkill("research", "3", "skill-v3")
    model = ModelRoutePolicy(
        required_capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        allowed_provider_ids=frozenset({"local"}),
        residency_regions=frozenset({"cn-east"}),
        max_call_cost_microusd=1000,
        max_output_tokens=2000,
        max_call_total_tokens=3000,
    )
    resolver = TeamAgentCapabilityResolver(
        engine=engine,
        tool_policies={"read-tools": (tool,)},
        skill_policies={"analysis-skills": (skill,)},
        model_policies={"local-model": model},
    )

    result = _resolve(stack, resolver)
    assert result.tool_authorization == ToolAuthorization(tools=(tool,), skills=(skill,))
    assert result.model_route_policy == model
    assert result.budget == RunBudget(max_turns=4, max_tool_calls=8)
    assert result.context_scope == ContextScope("project-a", "team-a")


def test_authorization_and_policy_order_does_not_change_digest(stack):
    engine, _ = stack
    tools = (AuthorizedTool("read.a", "1", "schema-a"), AuthorizedTool("read.b", "1", "schema-b"))
    skills = (
        AuthorizedSkill("analysis.a", "1", "skill-a"),
        AuthorizedSkill("analysis.b", "1", "skill-b"),
    )
    first = TeamAgentCapabilityResolver(
        engine=engine,
        tool_policies={"unused": (), "default": tools},
        skill_policies={"default": skills},
        model_policies={"default": ModelRoutePolicy(allowed_provider_ids=frozenset({"a", "b"}))},
    )
    second = TeamAgentCapabilityResolver(
        engine=engine,
        tool_policies={"default": tuple(reversed(tools)), "unused": ()},
        skill_policies={"default": tuple(reversed(skills))},
        model_policies={"default": ModelRoutePolicy(allowed_provider_ids=frozenset({"b", "a"}))},
    )
    assert _resolve(stack, first) == _resolve(stack, second)


@pytest.mark.parametrize(
    "change",
    [
        "tool-version",
        "tool-schema",
        "skill-version",
        "skill-content",
        "model-policy",
        "policy-id",
        "budget",
        "autonomy",
    ],
)
def test_digest_changes_with_every_static_authority_input(stack, change):
    engine, _ = stack
    tool = AuthorizedTool("read", "1", "schema-1")
    skill = AuthorizedSkill("analysis", "1", "content-1")
    kwargs = {
        "tool_policies": {"default": (tool,), "equivalent": (tool,)},
        "skill_policies": {"default": (skill,)},
        "model_policies": {"default": ModelRoutePolicy()},
    }
    original = _resolve(stack, TeamAgentCapabilityResolver(engine=engine, **kwargs))
    if change == "tool-version":
        kwargs["tool_policies"]["default"] = (replace(tool, version="2"),)
    elif change == "tool-schema":
        kwargs["tool_policies"]["default"] = (replace(tool, schema_digest="schema-2"),)
    elif change == "skill-version":
        kwargs["skill_policies"]["default"] = (replace(skill, version="2"),)
    elif change == "skill-content":
        kwargs["skill_policies"]["default"] = (replace(skill, content_digest="content-2"),)
    elif change == "model-policy":
        kwargs["model_policies"]["default"] = ModelRoutePolicy(max_output_tokens=1000)
    elif change == "policy-id":
        _profile_update(engine, tool_policy_id="equivalent")
    elif change == "budget":
        _profile_update(engine, max_run_budget_profile={"max_turns": 5})
    else:
        _profile_update(engine, autonomy_level="supervised")
    changed = _resolve(stack, TeamAgentCapabilityResolver(engine=engine, **kwargs))
    assert changed.delegation_scope_digest != original.delegation_scope_digest


def test_display_and_dynamic_memory_metadata_do_not_change_scope_digest(stack):
    original = _resolve(stack)
    _profile_update(stack[0], display_name="A renamed team agent")
    _agent_update(stack, memory_version=8)
    assert _resolve(stack) == original


def test_current_project_and_profile_identity_are_bound_into_scope_digest(stack):
    engine, agent = stack
    original = _resolve(stack)
    ProjectDirectoryService(engine).create_project(
        project_id="project-b",
        name="Project B",
        description="second delegation",
        actor_id="lead-a",
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    _agent_update(stack, project_id="project-b")
    resolver = TeamAgentCapabilityResolver(engine=engine)
    moved = resolver.resolve(agent_id=agent.agent_id, project_id="project-b")
    assert moved.context_scope == ContextScope("project-b", "team-a")
    assert moved.principal.compartments == frozenset({"project:project-b"})
    assert moved.delegation_scope_digest != original.delegation_scope_digest

    _profile_update(engine, profile_id="renamed-profile")
    _agent_update(stack, profile_id="renamed-profile")
    renamed = resolver.resolve(agent_id=agent.agent_id, project_id="project-b")
    assert renamed.profile_id == "renamed-profile"
    assert renamed.delegation_scope_digest != moved.delegation_scope_digest


def test_uncommitted_participation_revocation_is_seen_on_caller_connection(stack):
    engine, agent = stack
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            PROJECT_TEAMS.update()
            .where(PROJECT_TEAMS.c.project_id == "project-a", PROJECT_TEAMS.c.team_id == "team-a")
            .values(team_id="team-b")
        )
        with pytest.raises(PolicyDenied, match="participate"):
            TeamAgentCapabilityResolver(engine=engine).resolve(
                agent_id=agent.agent_id, project_id="project-a", connection=connection
            )
        assert transaction.is_active
        transaction.rollback()
    assert _resolve(stack).context_scope == ContextScope("project-a", "team-a")


def test_injected_mutable_containers_are_copied_into_immutable_snapshots(stack):
    engine, _ = stack
    tools = [AuthorizedTool("read", "1", "schema")]
    skills = [AuthorizedSkill("analysis", "1", "content")]
    providers = {"provider-a"}
    tool_policies = {"default": tools}
    skill_policies = {"default": skills}
    model_policies = {"default": ModelRoutePolicy(allowed_provider_ids=providers)}
    resolver = TeamAgentCapabilityResolver(
        engine=engine,
        tool_policies=tool_policies,
        skill_policies=skill_policies,
        model_policies=model_policies,
    )
    original = _resolve(stack, resolver)
    tools.append(AuthorizedTool("write", "1", "write-schema"))
    skills.append(AuthorizedSkill("admin", "1", "admin-content"))
    providers.add("provider-b")
    tool_policies["default"] = ()
    skill_policies["default"] = ()
    model_policies["default"] = ModelRoutePolicy()

    assert _resolve(stack, resolver) == original
    assert isinstance(original.tool_authorization.tools, tuple)
    assert isinstance(original.tool_authorization.skills, tuple)
    assert original.model_route_policy.allowed_provider_ids == frozenset({"provider-a"})


@pytest.mark.parametrize(
    "budget",
    [
        {"capabilities": ["review"]},
        {"max_turns": 1, "capacity": 3},
        {"max_turns": True},
        {"max_turns": False},
        {"max_turns": 0},
        {"max_turns": -1},
        {"max_turns": 1.5},
        {"max_turns": "4"},
        {"max_turns": None},
        [],
        None,
    ],
)
def test_profile_budget_rejects_unknown_fields_boolean_noninteger_and_nonpositive_values(
    stack, budget
):
    _profile_update(stack[0], max_run_budget_profile=budget)
    with pytest.raises(PolicyDenied, match="budget"):
        _resolve(stack)


@pytest.mark.parametrize(
    "policies",
    [
        {"tool_policies": {"default": ("read",)}},
        {"skill_policies": {"default": ({"name": "analysis"},)}},
        {"tool_policies": {"default": (AuthorizedTool("read", "", "schema"),)}},
        {"skill_policies": {"default": (AuthorizedSkill("analysis", "1", ""),)}},
        {
            "tool_policies": {
                "default": (AuthorizedTool("read", "1", "a"), AuthorizedTool("read", "2", "b"))
            }
        },
        {
            "skill_policies": {
                "default": (
                    AuthorizedSkill("analysis", "1", "a"),
                    AuthorizedSkill("analysis", "2", "b"),
                )
            }
        },
        {"model_policies": {"default": {"allowed_provider_ids": ["all"]}}},
        {"model_policies": {"default": ModelRoutePolicy(max_output_tokens=True)}},
    ],
)
def test_injected_policies_require_valid_versioned_runtime_bindings(stack, policies):
    with pytest.raises(ValueError):
        TeamAgentCapabilityResolver(engine=stack[0], **policies)


def test_connection_resolution_reads_uncommitted_changes_without_owning_transaction(
    stack, monkeypatch
):
    engine, agent = stack
    resolver = TeamAgentCapabilityResolver(engine=engine)
    original = _resolve(stack, resolver)
    with engine.connect() as connection:
        transaction = connection.begin()
        profile = dict(connection.execute(select(TEAM_AGENT_PROFILES)).mappings().one())
        profile.update(version=2, max_run_budget_profile={"max_turns": 3})
        connection.execute(TEAM_AGENT_PROFILES.insert().values(**profile))
        connection.execute(
            TEAM_PROJECT_AGENTS.update()
            .where(TEAM_PROJECT_AGENTS.c.agent_id == agent.agent_id)
            .values(profile_version=2)
        )

        def forbidden(*args, **kwargs):
            pytest.fail("resolver must not open another connection or begin a transaction")

        with monkeypatch.context() as patch:
            patch.setattr(engine, "connect", forbidden)
            patch.setattr(connection, "begin", forbidden)
            resolved = resolver.resolve(
                agent_id=agent.agent_id, project_id="project-a", connection=connection
            )
        assert resolved.budget.max_turns == 3
        assert resolved.profile_version == 2
        assert resolved.delegation_scope_digest != original.delegation_scope_digest
        assert transaction.is_active
        assert not connection.closed
        transaction.rollback()
    assert _resolve(stack, resolver) == original


def test_failed_resolution_does_not_rollback_caller_transaction(stack):
    engine, agent = stack
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            TEAM_AGENT_PROFILES.update()
            .where(TEAM_AGENT_PROFILES.c.profile_id == "team-a")
            .values(memory_policy_id="unknown")
        )
        with pytest.raises(PolicyDenied):
            TeamAgentCapabilityResolver(engine=engine).resolve(
                agent_id=agent.agent_id, project_id="project-a", connection=connection
            )
        assert transaction.is_active
        assert (
            connection.execute(select(TEAM_AGENT_PROFILES.c.memory_policy_id)).scalar_one()
            == "unknown"
        )
        transaction.rollback()
    assert _resolve(stack).context_scope == ContextScope("project-a", "team-a")
