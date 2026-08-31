"""Unified tool manifests shared by the Agent Worker and the Tool Worker.

The catalog separates what the model may see (a signed-off declaration) from
where the tool actually runs. Both workers build their view from the same
manifest constructors in this module; startup code compares catalog digests so
a declaration that no longer matches the executing side is reported as
unavailable instead of being guessed at.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..security import RiskLevel
from ..security.models import Principal
from ..skills import SkillCatalog
from ..tools.models import ToolDefinition
from ..tools.registry import ToolRegistry

TOOL_EXECUTOR_AGENT = "agent_worker"
TOOL_EXECUTOR_DURABLE = "tool_worker"

SkillBinding = tuple[str, str]


def _canonical_digest(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolManifest:
    tool_id: str
    version: str
    description: str
    parameters_schema: dict[str, Any]
    required_roles: frozenset[str] = field(default_factory=frozenset)
    risk: RiskLevel = RiskLevel.LOW
    executor: Literal["agent_worker", "tool_worker"] = TOOL_EXECUTOR_DURABLE
    timeout_seconds: float = 30.0
    max_output_chars: int = 50_000

    @property
    def schema_digest(self) -> str:
        return _canonical_digest(
            {
                "parameters_schema": self.parameters_schema,
                "required_roles": sorted(self.required_roles),
                "risk": self.risk.name,
                "timeout_seconds": self.timeout_seconds,
                "max_output_chars": self.max_output_chars,
            }
        )

    def declaration(self, handler=None) -> ToolDefinition:
        """Model-side declaration.

        Without an explicit handler the placeholder raises: the Agent Worker
        never executes durable tools in-process, and run-scoped handlers are
        attached only by the loop that owns the run authorization.
        """

        async def _unreachable(_: dict) -> Any:  # pragma: no cover - guard
            raise RuntimeError(f"tool {self.tool_id} must be executed by its declared executor")

        return ToolDefinition(
            name=self.tool_id,
            description=self.description,
            handler=handler if handler is not None else _unreachable,
            parameters_schema=self.parameters_schema,
            required_roles=self.required_roles,
            risk=self.risk,
            timeout_seconds=self.timeout_seconds,
            max_output_chars=self.max_output_chars,
            executor=self.executor,
        )


def sandbox_code_manifest(profile_ids: Iterable[str], *, timeout_seconds: float = 90.0) -> ToolManifest:
    return ToolManifest(
        tool_id="code.run_profile",
        version="1",
        description=(
            "Run an administrator-approved program profile in an isolated, "
            "network-disabled sandbox workspace."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "enum": sorted(profile_ids)},
                "arguments": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 32768},
                    "maxItems": 128,
                },
            },
            "required": ["profile_id", "arguments"],
            "additionalProperties": False,
        },
        required_roles=frozenset({"contributor"}),
        risk=RiskLevel.MEDIUM,
        executor=TOOL_EXECUTOR_DURABLE,
        timeout_seconds=timeout_seconds,
        max_output_chars=1_000_000,
    )


def office_message_manifest() -> ToolManifest:
    return ToolManifest(
        tool_id="office.send_message",
        version="1",
        description=(
            "Send a message through an approved office connector. High risk: "
            "every call requires a managed human approval before execution."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "connector_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "recipient": {"type": "string", "minLength": 1, "maxLength": 320},
                "content": {"type": "string", "minLength": 1, "maxLength": 8000},
            },
            "required": ["connector_id", "recipient", "content"],
            "additionalProperties": False,
        },
        required_roles=frozenset({"contributor"}),
        risk=RiskLevel.HIGH,
        executor=TOOL_EXECUTOR_DURABLE,
        timeout_seconds=30.0,
        max_output_chars=20_000,
    )


def skill_catalog_manifests() -> tuple[ToolManifest, ToolManifest]:
    """Skill discovery tools run inside the Agent Worker process.

    ``load_skill`` is bound per run: the loop builds a run-scoped handler that
    only loads the exact skill versions authorized at run creation, so a model
    cannot drift to a newer version or load an unselected skill.
    """
    return (
        ToolManifest(
            tool_id="list_skills",
            version="1",
            description="List signed skills visible to the current principal.",
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            required_roles=frozenset(),
            risk=RiskLevel.LOW,
            executor=TOOL_EXECUTOR_AGENT,
        ),
        ToolManifest(
            tool_id="load_skill",
            version="1",
            description="Load one skill version that was selected for this run.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "version": {"type": "string", "minLength": 5, "maxLength": 128},
                },
                "required": ["name", "version"],
                "additionalProperties": False,
            },
            required_roles=frozenset(),
            risk=RiskLevel.LOW,
            executor=TOOL_EXECUTOR_AGENT,
        ),
    )


def project_context_manifests() -> tuple[ToolManifest, ToolManifest]:
    """Read-only tools bound to the exact context items selected for one run."""
    return (
        ToolManifest(
            tool_id="project.list_context",
            version="1",
            description="List the project resources, code files, and document fragments selected for this run.",
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            executor=TOOL_EXECUTOR_AGENT,
        ),
        ToolManifest(
            tool_id="project.read_context",
            version="1",
            description="Read one context item that the user selected for this run.",
            parameters_schema={
                "type": "object",
                "properties": {"item_id": {"type": "string", "minLength": 1, "maxLength": 256}},
                "required": ["item_id"],
                "additionalProperties": False,
            },
            executor=TOOL_EXECUTOR_AGENT,
            max_output_chars=1_000_000,
        ),
    )


def build_builtin_manifests(
    *,
    sandbox_profile_ids: Iterable[str] = (),
    sandbox_timeout_seconds: float = 90.0,
    office_connector_configured: bool = False,
    task_artifact_publication_configured: bool = False,
) -> tuple[ToolManifest, ...]:
    manifests: list[ToolManifest] = []
    profiles = tuple(sandbox_profile_ids)
    if profiles:
        manifests.append(sandbox_code_manifest(profiles, timeout_seconds=sandbox_timeout_seconds))
    if office_connector_configured:
        manifests.append(office_message_manifest())
    if task_artifact_publication_configured:
        from ..artifacts.task_publication import task_artifact_manifest

        manifests.append(task_artifact_manifest())
    manifests.extend(skill_catalog_manifests())
    manifests.extend(project_context_manifests())
    return tuple(manifests)


def catalog_digest(manifests: Iterable[ToolManifest]) -> str:
    payload = [
        {
            "tool_id": manifest.tool_id,
            "version": manifest.version,
            "schema_digest": manifest.schema_digest,
            "executor": manifest.executor,
        }
        for manifest in sorted(manifests, key=lambda item: item.tool_id)
    ]
    return _canonical_digest({"tools": payload})


def validate_registry_manifests(
    manifests: Iterable[ToolManifest],
    registry: ToolRegistry,
    *,
    executor: str | None = None,
) -> str:
    """Fail startup when executable definitions drift from their manifests."""
    expected = {
        item.tool_id: item for item in manifests if executor is None or item.executor == executor
    }
    actual = {
        item.name: item
        for item in registry.definitions()
        if executor is None or item.executor == executor
    }
    problems: list[str] = []
    for name in sorted(set(expected) | set(actual)):
        manifest = expected.get(name)
        definition = actual.get(name)
        if manifest is None:
            problems.append(f"{name}: unexpected executable")
        elif definition is None:
            problems.append(f"{name}: executable missing")
        elif (
            definition.parameters_schema != manifest.parameters_schema
            or definition.required_roles != manifest.required_roles
            or definition.risk != manifest.risk
            or definition.executor != manifest.executor
            or definition.timeout_seconds != manifest.timeout_seconds
            or definition.max_output_chars != manifest.max_output_chars
        ):
            problems.append(f"{name}: declaration differs from manifest")
    if problems:
        raise ValueError("tool registry manifest mismatch: " + "; ".join(problems))
    return catalog_digest(expected.values())


def verify_catalog_match(
    agent_manifests: Iterable[ToolManifest],
    tool_worker_manifests: Iterable[ToolManifest],
) -> dict[str, str]:
    """Compare declarations by ``(tool_id, version, schema_digest)``.

    Returns a per-tool diagnostic map with value ``"ok"`` or a mismatch
    reason. Any mismatch must make the tool unavailable, never guessed.
    """
    agent_map = {(m.tool_id, m.version): m for m in agent_manifests}
    worker_map = {(m.tool_id, m.version): m for m in tool_worker_manifests}
    diagnostics: dict[str, str] = {}
    for key in sorted(set(agent_map) | set(worker_map), key=lambda k: (k[0], k[1])):
        tool_id = key[0]
        if key not in agent_map:
            diagnostics[tool_id] = "declaration missing on the Agent Worker"
        elif key not in worker_map:
            diagnostics[tool_id] = "executor missing on the Tool Worker"
        elif agent_map[key].executor != worker_map[key].executor:
            diagnostics[tool_id] = "declared executor differs between workers"
        elif agent_map[key].schema_digest != worker_map[key].schema_digest:
            diagnostics[tool_id] = "declaration digest differs between workers"
        else:
            diagnostics[tool_id] = "ok"
    return diagnostics


def build_agent_worker_registry(manifests: Iterable[ToolManifest]) -> ToolRegistry:
    registry = ToolRegistry()
    for manifest in manifests:
        registry.register(manifest.declaration())
    return registry


class RunScopedToolRegistry:
    """Per-run registry view: only tools authorized at run creation resolve.

    Unauthorized names resolve to ``None`` exactly like unknown tools, so a
    model fabricating a call to a tool outside this run's authorization is
    denied by the executor before any dispatch. Definitions stay filtered so
    unauthorized tools are never advertised to the model either.
    """

    def __init__(
        self,
        *,
        base: ToolRegistry,
        allowed_tools: frozenset[str] | None,
        extra: tuple[ToolDefinition, ...] = (),
    ) -> None:
        self.base = base
        self.allowed_tools = allowed_tools
        self.extra = {definition.name: definition for definition in extra}

    def register(self, definition: ToolDefinition) -> None:  # pragma: no cover
        raise ValueError("run-scoped registries are immutable")

    def get(self, name: str) -> ToolDefinition | None:
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return None
        if name in self.extra:
            return self.extra[name]
        return self.base.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        values: list[ToolDefinition] = []
        for name in sorted(
            set(self.extra) | {definition.name for definition in self.base.definitions()}
        ):
            definition = self.get(name)
            if definition is not None:
                values.append(definition)
        return tuple(values)


def build_project_context_run_tools(context_items: tuple) -> tuple[ToolDefinition, ...]:
    """Bind project context tools to immutable items already authorized for a run."""
    item_map = {item.item_id: item for item in context_items}

    async def list_handler(_: dict) -> str:
        return json.dumps(
            [
                {
                    "item_id": item.item_id,
                    "source": item.source.value,
                    "source_id": item.source_id,
                    "resource_id": item.label.resource_id,
                }
                for item in context_items
            ],
            ensure_ascii=False,
        )

    async def read_handler(arguments: dict) -> str:
        item = item_map.get(arguments["item_id"])
        if item is None:
            raise PermissionError("context item was not selected for this run")
        return item.content

    manifests = project_context_manifests()
    return (
        manifests[0].declaration(handler=list_handler),
        manifests[1].declaration(handler=read_handler),
    )


def build_skill_run_tools(
    *,
    catalog: SkillCatalog,
    principal: Principal,
    bindings: frozenset[SkillBinding],
    available_tools: frozenset[str],
) -> tuple[ToolDefinition, ...]:
    """Run-scoped skill tools pinned to the versions chosen at creation."""

    async def list_skills_handler(_: dict) -> str:
        entries = [
            {
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "required_tools": sorted(manifest.required_tools),
                "selected_for_run": (manifest.name, manifest.version) in bindings,
            }
            for manifest in catalog.list_visible(principal)
        ]
        return json.dumps(entries, ensure_ascii=False)

    async def load_skill_handler(arguments: dict) -> str:
        if (arguments["name"], arguments["version"]) not in bindings:
            raise PermissionError("skill version was not selected for this run")
        skill = catalog.load(
            principal=principal,
            name=arguments["name"],
            version=arguments["version"],
            available_tools=available_tools,
        )
        return skill.render_for_context()

    definitions = []
    for manifest in skill_catalog_manifests():
        handler = list_skills_handler if manifest.tool_id == "list_skills" else load_skill_handler
        definitions.append(manifest.declaration(handler=handler))
    return tuple(definitions)
