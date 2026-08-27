from __future__ import annotations

import json

from ..security.models import Principal, RiskLevel
from ..tools.models import ToolDefinition
from .catalog import SkillCatalog


def create_skill_tools(
    *,
    catalog: SkillCatalog,
    principal: Principal,
    available_tools: frozenset[str],
) -> tuple[ToolDefinition, ToolDefinition]:
    """Create model-callable catalog tools without granting new capabilities."""

    async def list_skills(_: dict) -> str:
        manifests = catalog.list_visible(principal)
        return json.dumps(
            [
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "required_tools": sorted(manifest.required_tools),
                    "classification": manifest.classification.name,
                }
                for manifest in manifests
            ],
            ensure_ascii=False,
        )

    async def load_skill(arguments: dict) -> str:
        skill = catalog.load(
            principal=principal,
            name=arguments["name"],
            version=arguments.get("version"),
            available_tools=available_tools,
        )
        return skill.render_for_context()

    list_definition = ToolDefinition(
        name="list_skills",
        description="List signed skills visible to the current tenant and principal.",
        handler=list_skills,
        parameters_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
    )
    load_definition = ToolDefinition(
        name="load_skill",
        description=(
            "Load one signed skill on demand. Loading instructions never grants "
            "the tools declared by that skill."
        ),
        handler=load_skill,
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "version": {
                    "type": "string",
                    "minLength": 5,
                    "maxLength": 128,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
    )
    return list_definition, load_definition
