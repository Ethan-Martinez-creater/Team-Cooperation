from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from ..errors import SkillError
from ..skills import SkillCatalog
from .agent_capabilities import AgentCapabilityService
from .auth import Authenticated, BearerAuthenticator
from .models import StrictModel


class AgentCapabilityToolView(StrictModel):
    tool_id: str
    version: str
    status: str = Field(pattern=r"^(available|approval_required|not_configured|no_permission)$")
    reason: str
    read_only: bool
    modifies_external_system: bool


class AgentCapabilitySkillView(StrictModel):
    name: str
    version: str
    description: str
    publisher_team: str
    required_tools: tuple[str, ...]
    classification: str


class AgentCapabilityModelView(StrictModel):
    provider_id: str
    model: str
    status: str = Field(pattern=r"^(available|not_configured)$")
    reason: str


class AgentCapabilityReportView(StrictModel):
    project_id: str | None = None
    tools: list[AgentCapabilityToolView]
    skills: list[AgentCapabilitySkillView]
    models: list[AgentCapabilityModelView]


class SkillVersionView(StrictModel):
    name: str
    version: str
    description: str
    publisher_team: str
    classification: str
    compartments: tuple[str, ...]
    required_tools: tuple[str, ...]
    content_digest: str | None = None
    instructions: str | None = None


def build_agent_capability_router(
    *,
    authenticator: BearerAuthenticator,
    capabilities: AgentCapabilityService,
    skill_catalog: SkillCatalog | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["agent-capabilities"])

    @router.get("/agent-capabilities", response_model=AgentCapabilityReportView)
    async def agent_capabilities(
        request: Request,
        project_id: str | None = Query(default=None, min_length=1, max_length=128),
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentCapabilityReportView:
        if project_id is not None:
            directory = getattr(request.app.state, "project_directory_service", None)
            if directory is None:
                raise HTTPException(status_code=503, detail="项目协作服务不可用")
            await run_in_threadpool(
                directory.get_project,
                project_id=project_id,
                actor_id=authenticated.principal.principal_id,
            )
        report = await run_in_threadpool(
            capabilities.report,
            principal=authenticated.principal,
            project_id=project_id,
        )
        return AgentCapabilityReportView(
            project_id=project_id,
            tools=[
                AgentCapabilityToolView(
                    tool_id=item.tool_id,
                    version=item.version,
                    status=item.status,
                    reason=item.reason,
                    read_only=item.read_only,
                    modifies_external_system=item.modifies_external_system,
                )
                for item in report.tools
            ],
            skills=[
                AgentCapabilitySkillView(
                    name=item.name,
                    version=item.version,
                    description=item.description,
                    publisher_team=item.publisher_team,
                    required_tools=item.required_tools,
                    classification=item.classification,
                )
                for item in report.skills
            ],
            models=[
                AgentCapabilityModelView(
                    provider_id=item.provider_id,
                    model=item.model,
                    status=item.status,
                    reason=item.reason,
                )
                for item in report.models
            ],
        )

    if skill_catalog is None:
        return router

    @router.get("/skills", response_model=list[SkillVersionView])
    async def skills(
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[SkillVersionView]:
        manifests = await run_in_threadpool(skill_catalog.list_visible, authenticated.principal)
        return [
            SkillVersionView(
                name=manifest.name,
                version=manifest.version,
                description=manifest.description,
                publisher_team=manifest.tenant_id,
                classification=manifest.classification.name.lower(),
                compartments=tuple(sorted(manifest.compartments)),
                required_tools=tuple(sorted(manifest.required_tools)),
                content_digest="",
                instructions=None,
            )
            for manifest in manifests
        ]

    @router.get(
        "/skills/{name}/versions/{version}",
        response_model=SkillVersionView,
    )
    async def skill_version(
        name: str,
        version: str,
        include_instructions: bool = Query(default=False),
        authenticated: Authenticated = Depends(authenticator),
    ) -> SkillVersionView:
        def load():
            # ``available_tools`` is unrestricted here: listing metadata must
            # not depend on a run; expansion is still blocked at load time in
            # the loop, which passes only the authorized set.
            return skill_catalog.load(
                principal=authenticated.principal,
                name=name,
                version=version,
                available_tools=frozenset({"*"}),
            )

        try:
            skill = await run_in_threadpool(load)
        except SkillError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return SkillVersionView(
            name=skill.manifest.name,
            version=skill.manifest.version,
            description=skill.manifest.description,
            publisher_team=skill.manifest.tenant_id,
            classification=skill.manifest.classification.name.lower(),
            compartments=tuple(sorted(skill.manifest.compartments)),
            required_tools=tuple(sorted(skill.manifest.required_tools)),
            content_digest=skill.content_digest,
            instructions=skill.instructions if include_instructions else None,
        )

    return router
