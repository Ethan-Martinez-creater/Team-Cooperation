from __future__ import annotations

import pytest

from coifesp_harness.security import RiskLevel
from coifesp_harness.tool_catalog import (
    TOOL_EXECUTOR_AGENT,
    TOOL_EXECUTOR_DURABLE,
    RunScopedToolRegistry,
    build_agent_worker_registry,
    build_builtin_manifests,
    build_project_context_run_tools,
    build_skill_run_tools,
    catalog_digest,
    github_connector_manifests,
    office_message_manifest,
    sandbox_code_manifest,
    skill_catalog_manifests,
    validate_registry_manifests,
    verify_catalog_match,
)
from coifesp_harness.tools.registry import ToolRegistry


def _simple_tool(name: str, executor: str = TOOL_EXECUTOR_DURABLE):
    from coifesp_harness.tool_catalog import ToolManifest

    return ToolManifest(
        tool_id=name,
        version="1",
        description=f"tool {name}",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        executor=executor,
    )


def test_manifest_digest_is_stable_and_sensitive():
    manifest = sandbox_code_manifest(["python.isolated"])
    again = sandbox_code_manifest(["python.isolated"])
    assert manifest.schema_digest == again.schema_digest
    assert (
        manifest.schema_digest
        != sandbox_code_manifest(["python.isolated", "bash.limited"]).schema_digest
    )
    changed = office_message_manifest()
    assert changed.risk is RiskLevel.HIGH
    assert changed.executor == TOOL_EXECUTOR_DURABLE


def test_catalog_digest_orders_tools_deterministically():
    left = build_builtin_manifests(sandbox_profile_ids=["a"], office_connector_configured=True)
    right = tuple(
        reversed(
            build_builtin_manifests(sandbox_profile_ids=["a"], office_connector_configured=True)
        )
    )
    assert catalog_digest(left) == catalog_digest(right)


def test_task_artifact_tool_only_advertised_when_storage_is_configured():
    plain = build_builtin_manifests()
    enabled = build_builtin_manifests(task_artifact_publication_configured=True)
    assert "project.publish_artifact" not in {item.tool_id for item in plain}
    manifest = next(item for item in enabled if item.tool_id == "project.publish_artifact")
    assert manifest.executor == TOOL_EXECUTOR_DURABLE
    assert manifest.required_roles == frozenset({"team_agent"})
    assert catalog_digest(plain) != catalog_digest(enabled)


def test_github_tools_only_advertised_for_complete_adapter_configuration():
    plain = build_builtin_manifests()
    enabled = build_builtin_manifests(github_connector_configured=True)
    github_ids = {manifest.tool_id for manifest in github_connector_manifests()}
    assert not github_ids.intersection(manifest.tool_id for manifest in plain)
    assert github_ids.issubset(manifest.tool_id for manifest in enabled)


def test_specialist_delegation_only_advertised_when_executor_is_configured():
    plain = build_builtin_manifests()
    enabled = build_builtin_manifests(specialist_delegation_configured=True)
    assert "specialist.delegate" not in {item.tool_id for item in plain}
    manifest = next(item for item in enabled if item.tool_id == "specialist.delegate")
    assert manifest.executor == TOOL_EXECUTOR_DURABLE
    assert manifest.required_roles == frozenset({"team_agent"})
    assert manifest.risk is RiskLevel.LOW


def test_verify_catalog_match_reports_ok_and_each_mismatch_kind():
    sandbox = sandbox_code_manifest(["p"])
    skills = skill_catalog_manifests()
    ok = verify_catalog_match([sandbox, *skills], [sandbox, *skills])
    assert set(ok.values()) == {"ok"}

    missing_worker = verify_catalog_match([sandbox], [])
    assert missing_worker["code.run_profile"] == "executor missing on the Tool Worker"

    missing_agent = verify_catalog_match([], [sandbox])
    assert missing_agent["code.run_profile"] == "declaration missing on the Agent Worker"

    drifted = sandbox_code_manifest(["p", "extra"])
    digest_differs = verify_catalog_match([drifted], [sandbox])
    assert digest_differs["code.run_profile"] == "declaration digest differs between workers"


def test_registry_manifest_validation_fails_on_missing_or_extra_executor():
    manifest = _simple_tool("alpha")
    registry = ToolRegistry()
    registry.register(manifest.declaration())
    assert validate_registry_manifests([manifest], registry) == catalog_digest([manifest])

    unexpected = ToolRegistry()
    unexpected.register(_simple_tool("beta").declaration())
    with pytest.raises(ValueError, match="registry manifest mismatch"):
        validate_registry_manifests([manifest], unexpected)


def test_project_context_tools_only_read_selected_items():
    import asyncio
    from types import SimpleNamespace

    item = SimpleNamespace(
        item_id="repo:src/app.py",
        content="print(123)",
        source=SimpleNamespace(value="repository"),
        source_id="repo-1",
        label=SimpleNamespace(resource_id="project:repo-1:src/app.py"),
    )
    definitions = {value.name: value for value in build_project_context_run_tools((item,))}
    listing = asyncio.run(definitions["project.list_context"].handler({}))
    assert "repo:src/app.py" in listing
    assert (
        asyncio.run(definitions["project.read_context"].handler({"item_id": "repo:src/app.py"}))
        == "print(123)"
    )
    with pytest.raises(PermissionError, match="not selected"):
        asyncio.run(definitions["project.read_context"].handler({"item_id": "repo:secret.env"}))


def test_declaration_registry_has_no_executable_handlers():
    manifests = build_builtin_manifests(sandbox_profile_ids=["p"])
    registry = build_agent_worker_registry(manifests)
    definition = registry.get("code.run_profile")
    assert definition is not None and definition.executor == TOOL_EXECUTOR_DURABLE
    with pytest.raises(RuntimeError):
        import asyncio

        asyncio.run(definition.handler({}))


def test_run_scoped_registry_hides_unauthorized_tools():
    base = ToolRegistry()
    base.register(_simple_tool("alpha").declaration())
    base.register(_simple_tool("beta").declaration())
    scoped = RunScopedToolRegistry(base=base, allowed_tools=frozenset({"alpha"}))
    assert [item.name for item in scoped.definitions()] == ["alpha"]
    assert scoped.get("beta") is None  # unauthorized == unknown for the executor
    assert scoped.get("alpha") is not None
    with pytest.raises(ValueError):
        scoped.register(_simple_tool("gamma").declaration())


def test_run_scoped_registry_without_authorization_keeps_everything():
    base = ToolRegistry()
    base.register(_simple_tool("alpha").declaration())
    scoped = RunScopedToolRegistry(base=base, allowed_tools=None)
    assert len(scoped.definitions()) == 1


def test_run_scoped_registry_extra_overrides_base():
    async def _handler(_: dict) -> str:
        return "ok"

    base = ToolRegistry()
    base.register(_simple_tool("load_skill", TOOL_EXECUTOR_AGENT).declaration())
    replacement = _simple_tool("load_skill", TOOL_EXECUTOR_AGENT).declaration(handler=_handler)
    import asyncio

    scoped = RunScopedToolRegistry(
        base=base, allowed_tools=frozenset({"load_skill"}), extra=(replacement,)
    )
    assert asyncio.run(scoped.get("load_skill").handler({})) == "ok"


def test_skill_run_tools_pin_versions_and_reject_unselected():
    import asyncio
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from coifesp_harness.security.models import Classification, Principal
    from coifesp_harness.security.policy import PolicyEngine
    from coifesp_harness.skills import SkillCatalog, SkillTrustStore

    private = Ed25519PrivateKey.generate()
    trust = SkillTrustStore()
    trust.register(
        tenant_id="team-a",
        key_id="key-1",
        public_key=private.public_key(),
    )
    import pathlib
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp(dir="."))
    try:
        package = root / "team-a" / "review" / "1.0.0"
        package.mkdir(parents=True)
        body = (
            b"---\n"
            b"name: review\n"
            b"version: 1.0.0\n"
            b"description: Review shared contracts.\n"
            b"tenant_id: team-a\n"
            b"classification: INTERNAL\n"
            b"compartments: []\n"
            b"required_tools: []\n"
            b"signer_key_id: key-1\n"
            b"---\nReview carefully.\n"
        )
        (package / "SKILL.md").write_bytes(body)
        (package / "SKILL.sig").write_bytes(base64.b64encode(private.sign(body)))
        catalog = SkillCatalog(root=root, trust_store=trust, policy=PolicyEngine())
        catalog.scan()
        principal = Principal(
            principal_id="acct-1",
            tenant_id="team-a",
            roles=frozenset({"contributor"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset(),
        )
        tools = build_skill_run_tools(
            catalog=catalog,
            principal=principal,
            bindings=frozenset({("review", "1.0.0")}),
            available_tools=frozenset({"load_skill"}),
        )
        by_name = {definition.name: definition for definition in tools}
        assert by_name["load_skill"].executor == TOOL_EXECUTOR_AGENT

        loaded = asyncio.run(by_name["load_skill"].handler({"name": "review", "version": "1.0.0"}))
        assert "Review carefully" in loaded

        with pytest.raises(PermissionError):
            asyncio.run(by_name["load_skill"].handler({"name": "review", "version": "2.0.0"}))
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
