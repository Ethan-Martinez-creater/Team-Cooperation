from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..errors import HarnessError
from ..product.models import PlanDraftStatus
from .auth import Authenticated, BearerAuthenticator


class ProjectPlanDraftView(BaseModel):
    draft_id: str
    project_id: str
    source_conversation_id: str | None
    source_turn_id: str | None
    source_run_id: str | None
    schema_version: str
    plan_payload: dict
    goals: str
    scope: str
    phases: list[dict]
    milestones: list[dict]
    risks: list[dict]
    dependencies: list[dict]
    acceptance_criteria: list[str]
    content_sha256: str
    status: PlanDraftStatus
    version: int
    created_by: str
    created_at: str
    updated_at: str
    approved_at: str | None
    rejection_reason: str


class ProjectTeamRequirementDraftView(BaseModel):
    requirement_id: str
    project_id: str
    plan_draft_id: str | None
    team_category: str
    team_count: int
    rationale: str
    status: PlanDraftStatus
    created_by: str
    created_at: str
    approved_at: str | None


class PlanImportBody(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    source_conversation_id: str | None = Field(default=None, max_length=128)
    source_turn_id: str | None = Field(default=None, max_length=128)
    source_run_id: str | None = Field(default=None, max_length=128)


class PlanDecideBody(BaseModel):
    reason: str = Field(default="", max_length=10_000)


def build_planning_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(tags=["project-planning"])

    @router.get(
        "/v1/projects/{project_id}/plan-drafts",
        response_model=list[ProjectPlanDraftView],
    )
    async def list_plan_drafts(
        project_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[ProjectPlanDraftView]:
        service = _service(request)
        values = await run_in_threadpool(
            service.list_plan_drafts,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )
        return [_plan_view(value) for value in values]

    @router.get(
        "/v1/projects/{project_id}/team-requirement-drafts",
        response_model=list[ProjectTeamRequirementDraftView],
    )
    async def list_team_requirements(
        project_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[ProjectTeamRequirementDraftView]:
        service = _service(request)
        values = await run_in_threadpool(
            service.list_team_requirements,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )
        return [_requirement_view(value) for value in values]

    @router.post(
        "/v1/projects/{project_id}/plan-drafts:import",
        response_model=ProjectPlanDraftView,
        status_code=201,
    )
    async def import_plan_draft(
        project_id: str,
        body: PlanImportBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ProjectPlanDraftView:
        service = _service(request)
        value = await run_in_threadpool(
            service.import_plan_draft,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            content=body.content,
            source_conversation_id=body.source_conversation_id,
            source_turn_id=body.source_turn_id,
            source_run_id=body.source_run_id,
        )
        return _plan_view(value)

    @router.post(
        "/v1/projects/{project_id}/plan-drafts/{draft_id}:approve",
        response_model=ProjectPlanDraftView,
    )
    async def approve_plan_draft(
        project_id: str,
        draft_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ProjectPlanDraftView:
        service = _service(request)
        value = await run_in_threadpool(
            service.approve_plan_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
        )
        return _plan_view(value)

    @router.post(
        "/v1/projects/{project_id}/plan-drafts/{draft_id}:reject",
        response_model=ProjectPlanDraftView,
    )
    async def reject_plan_draft(
        project_id: str,
        draft_id: str,
        body: PlanDecideBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ProjectPlanDraftView:
        service = _service(request)
        value = await run_in_threadpool(
            service.reject_plan_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            reason=body.reason,
        )
        return _plan_view(value)

    return router


def _service(request: Request):
    service = getattr(request.app.state, "project_planning_service", None)
    if service is None:
        raise HarnessError("project planning service is unavailable")
    return service


def _plan_view(value) -> ProjectPlanDraftView:
    return ProjectPlanDraftView(
        draft_id=value.draft_id,
        project_id=value.project_id,
        source_conversation_id=value.source_conversation_id,
        source_turn_id=value.source_turn_id,
        source_run_id=value.source_run_id,
        schema_version=value.schema_version,
        plan_payload=dict(value.plan_payload),
        goals=value.goals,
        scope=value.scope,
        phases=list(value.phases),
        milestones=list(value.milestones),
        risks=list(value.risks),
        dependencies=list(value.dependencies),
        acceptance_criteria=list(value.acceptance_criteria),
        content_sha256=value.content_sha256,
        status=value.status,
        version=value.version,
        created_by=value.created_by,
        created_at=value.created_at.isoformat(),
        updated_at=value.updated_at.isoformat(),
        approved_at=value.approved_at.isoformat() if value.approved_at else None,
        rejection_reason=value.rejection_reason,
    )


def _requirement_view(value) -> ProjectTeamRequirementDraftView:
    return ProjectTeamRequirementDraftView(
        requirement_id=value.requirement_id,
        project_id=value.project_id,
        plan_draft_id=value.plan_draft_id,
        team_category=value.team_category,
        team_count=value.team_count,
        rationale=value.rationale,
        status=value.status,
        created_by=value.created_by,
        created_at=value.created_at.isoformat(),
        approved_at=value.approved_at.isoformat() if value.approved_at else None,
    )
