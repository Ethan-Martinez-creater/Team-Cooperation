from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.concurrency import run_in_threadpool

from ..collaboration import GovernanceService
from ..collaboration.governance_models import BoardMember
from ..errors import GovernanceError
from ..security import Classification
from .auth import Authenticated, BearerAuthenticator
from .governance_models import (
    AssignmentCreateBody,
    AssignmentResponseBody,
    AssignmentReviewBody,
    AssignmentSubmissionBody,
    AssignmentView,
    AssignmentSummaryView,
    DiscussionItemCreateBody,
    DiscussionItemView,
    DiscussionResolutionBody,
    GovernanceCommandResponse,
    MemberAddBody,
    MemberView,
    PlanCreateBody,
    PlanView,
    ProgramCreateBody,
    ProgramSummaryView,
    ProgramView,
)

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
IfMatch = Annotated[
    str,
    Header(
        alias="If-Match",
        min_length=3,
        max_length=24,
        pattern=r'^"[1-9][0-9]*"$',
    ),
]


def build_governance_router(
    *,
    authenticator: BearerAuthenticator,
) -> APIRouter:
    router = APIRouter(prefix="/v1/governance/programs", tags=["governance"])

    @router.get("", response_model=list[ProgramSummaryView])
    async def list_programs(
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[ProgramSummaryView]:
        rows = await run_in_threadpool(
            _service(request).list_programs,
            principal=authenticated.principal,
            limit=100,
        )
        return [ProgramSummaryView(
            program_id=row["program_id"], owner_tenant_id=row["owner_tenant_id"],
            title=row["title"], classification=ClassificationName(
                Classification(int(row["classification"])).name.lower()
            ), aggregate_version=int(row["aggregate_version"]), role=row["role"],
            updated_at=row["updated_at"].isoformat(),
        ) for row in rows]

    @router.get("/-/assignments", response_model=list[AssignmentSummaryView])
    async def list_assignments(
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[AssignmentSummaryView]:
        rows = await run_in_threadpool(
            _service(request).list_assignments,
            principal=authenticated.principal,
            limit=200,
        )
        return [AssignmentSummaryView(**{
            **row,
            "aggregate_version": int(row["aggregate_version"]),
            "updated_at": row["updated_at"].isoformat(),
        }) for row in rows]

    @router.post(
        "",
        response_model=GovernanceCommandResponse,
        status_code=201,
    )
    async def create_program(
        body: ProgramCreateBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).create_program,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=body.program_id,
            title=body.title,
            objective=body.objective,
            classification=Classification[body.classification.name],
            compartments=frozenset(body.compartments),
        )
        return _command_response(result, response, created=True)

    @router.get(
        "/{program_id}",
        response_model=ProgramView,
    )
    async def read_program(
        program_id: str,
        request: Request,
        response: Response,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ProgramView:
        board = await run_in_threadpool(
            _service(request).read_program,
            principal=authenticated.principal,
            program_id=program_id,
        )
        response.headers["ETag"] = _etag(board.aggregate_version)
        return _program_view(board)

    @router.post(
        "/{program_id}/members",
        response_model=GovernanceCommandResponse,
        status_code=201,
    )
    async def add_member(
        program_id: str,
        body: MemberAddBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).add_member,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            member=BoardMember(
                principal_id=body.principal_id,
                tenant_id=body.tenant_id,
                role=body.role,
            ),
        )
        return _command_response(result, response, created=True)

    @router.post(
        "/{program_id}/plans",
        response_model=GovernanceCommandResponse,
        status_code=201,
    )
    async def create_plan(
        program_id: str,
        body: PlanCreateBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).create_plan,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            plan_id=body.plan_id,
            version=body.version,
            title=body.title,
            objective=body.objective,
            deliverables=tuple(body.deliverables),
            required_approvers=frozenset(body.required_approvers),
            visible_to_tenants=frozenset(body.visible_to_tenants),
        )
        return _command_response(result, response, created=True)

    @router.post(
        "/{program_id}/plans/{plan_id}:open-discussion",
        response_model=GovernanceCommandResponse,
    )
    async def open_discussion(
        program_id: str,
        plan_id: str,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).open_discussion,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            plan_id=plan_id,
        )
        return _command_response(result, response)

    @router.post(
        "/{program_id}/plans/{plan_id}/discussion-items",
        response_model=GovernanceCommandResponse,
        status_code=201,
    )
    async def add_discussion_item(
        program_id: str,
        plan_id: str,
        body: DiscussionItemCreateBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).add_discussion_item,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            plan_id=plan_id,
            item_id=body.item_id,
            kind=body.kind,
            content=body.content,
            blocking=body.blocking,
        )
        return _command_response(result, response, created=True)

    @router.post(
        "/{program_id}/plans/{plan_id}/discussion-items/{item_id}:resolve",
        response_model=GovernanceCommandResponse,
    )
    async def resolve_discussion_item(
        program_id: str,
        plan_id: str,
        item_id: str,
        body: DiscussionResolutionBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).resolve_discussion_item,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            plan_id=plan_id,
            item_id=item_id,
            resolution=body.resolution,
        )
        return _command_response(result, response)

    @router.post(
        "/{program_id}/plans/{plan_id}:approve",
        response_model=GovernanceCommandResponse,
    )
    async def approve_plan(
        program_id: str,
        plan_id: str,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).approve_plan,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            plan_id=plan_id,
        )
        return _command_response(result, response)

    @router.post(
        "/{program_id}/assignments",
        response_model=GovernanceCommandResponse,
        status_code=201,
    )
    async def propose_assignment(
        program_id: str,
        body: AssignmentCreateBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).propose_assignment,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            assignment_id=body.assignment_id,
            plan_id=body.plan_id,
            assignee_id=body.assignee_id,
            title=body.title,
            description=body.description,
            deliverable_contract=body.deliverable_contract,
            dependencies=tuple(body.dependencies),
            visible_to_tenants=frozenset(body.visible_to_tenants),
        )
        return _command_response(result, response, created=True)

    @router.post(
        "/{program_id}/assignments/{assignment_id}:respond",
        response_model=GovernanceCommandResponse,
    )
    async def respond_to_assignment(
        program_id: str,
        assignment_id: str,
        body: AssignmentResponseBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).respond_to_assignment,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            assignment_id=assignment_id,
            accept=body.accept,
            reason=body.reason,
        )
        return _command_response(result, response)

    @router.post(
        "/{program_id}/assignments/{assignment_id}:start",
        response_model=GovernanceCommandResponse,
    )
    async def start_assignment(
        program_id: str,
        assignment_id: str,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).start_assignment,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            assignment_id=assignment_id,
        )
        return _command_response(result, response)

    @router.post(
        "/{program_id}/assignments/{assignment_id}:submit",
        response_model=GovernanceCommandResponse,
    )
    async def submit_assignment(
        program_id: str,
        assignment_id: str,
        body: AssignmentSubmissionBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).submit_assignment,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            assignment_id=assignment_id,
            artifact_refs=tuple(body.artifact_refs),
        )
        return _command_response(result, response)

    @router.post(
        "/{program_id}/assignments/{assignment_id}:review",
        response_model=GovernanceCommandResponse,
    )
    async def review_assignment(
        program_id: str,
        assignment_id: str,
        body: AssignmentReviewBody,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        authenticated: Authenticated = Depends(authenticator),
    ) -> GovernanceCommandResponse:
        result = await run_in_threadpool(
            _service(request).review_assignment,
            principal=authenticated.principal,
            idempotency_key=idempotency_key,
            program_id=program_id,
            expected_version=_version(if_match),
            assignment_id=assignment_id,
            accept=body.accept,
            note=body.note,
        )
        return _command_response(result, response)

    return router


def _service(request: Request) -> GovernanceService:
    service = getattr(request.app.state, "governance_service", None)
    if service is None:
        raise GovernanceError("governance service is not configured")
    return service


def _version(if_match: str) -> int:
    return int(if_match[1:-1])


def _etag(version: int) -> str:
    return f'"{version}"'


def _command_response(result, response: Response, *, created: bool = False):
    response.headers["ETag"] = _etag(result.aggregate_version)
    if result.duplicate:
        response.status_code = 200
    elif created:
        response.status_code = 201
    return GovernanceCommandResponse(
        program_id=result.program_id,
        aggregate_version=result.aggregate_version,
        duplicate=result.duplicate,
    )


def _program_view(board) -> ProgramView:
    return ProgramView(
        program_id=board.program_id,
        owner_tenant_id=board.owner_tenant_id,
        title=board.title,
        objective=board.objective,
        classification=board.classification.name.lower(),
        compartments=sorted(board.compartments),
        aggregate_version=board.aggregate_version,
        members=[
            MemberView(
                principal_id=member.principal_id,
                tenant_id=member.tenant_id,
                role=member.role,
            )
            for member in sorted(
                board.members.values(),
                key=lambda item: item.principal_id,
            )
        ],
        plans=[
            PlanView(
                plan_id=plan.plan_id,
                version=plan.version,
                title=plan.title,
                objective=plan.objective,
                deliverables=list(plan.deliverables),
                lead_id=plan.lead_id,
                required_approvers=sorted(plan.required_approvers),
                approvals=sorted(plan.approvals),
                visible_to_tenants=sorted(plan.visible_to_tenants),
                content_digest=plan.content_digest,
                state=plan.state.value,
                discussion_items=[
                    DiscussionItemView(
                        item_id=item.item_id,
                        author_id=item.author_id,
                        kind=item.kind,
                        content=item.content,
                        blocking=item.blocking,
                        resolved=item.resolved,
                        resolved_by=item.resolved_by,
                        resolution=item.resolution,
                    )
                    for item in plan.discussion_items
                ],
            )
            for plan in sorted(
                board.plans.values(),
                key=lambda item: item.plan_id,
            )
        ],
        assignments=[
            AssignmentView(
                assignment_id=assignment.assignment_id,
                plan_id=assignment.plan_id,
                plan_digest=assignment.plan_digest,
                title=assignment.title,
                description=assignment.description,
                deliverable_contract=assignment.deliverable_contract,
                proposed_by=assignment.proposed_by,
                assignee_id=assignment.assignee_id,
                dependencies=list(assignment.dependencies),
                visible_to_tenants=sorted(assignment.visible_to_tenants),
                state=assignment.state.value,
                response_reason=assignment.response_reason,
                artifact_refs=list(assignment.artifact_refs),
                verification_note=assignment.verification_note,
            )
            for assignment in sorted(
                board.assignments.values(),
                key=lambda item: item.assignment_id,
            )
        ],
    )
