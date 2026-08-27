from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from ..context import SemanticCheckpointService, SemanticSummary
from ..runtime.models import Message, ToolCall
from ..security import Classification
from .auth import Authenticated, BearerAuthenticator
from .models import (SemanticCheckpointCreateBody, SemanticCheckpointResponse,
    SemanticCheckpointResumeResponse, SemanticCheckpointReviewBody,
    SemanticSummaryBody)


def build_checkpoint_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/context/checkpoints", tags=["context"])

    @router.post("", response_model=SemanticCheckpointResponse, status_code=201)
    async def propose(body: SemanticCheckpointCreateBody, request: Request,
                      authenticated: Authenticated = Depends(authenticator)):
        messages = tuple(Message(role=item.role, content=item.content, name=item.name,
            tool_call_id=item.tool_call_id, tool_calls=tuple(ToolCall(call_id=call.call_id,
                name=call.name, arguments=call.arguments) for call in item.tool_calls))
            for item in body.messages)
        value = await run_in_threadpool(_service(request).propose,
            principal=authenticated.principal, checkpoint_id=body.checkpoint_id,
            conversation_id=body.conversation_id, messages=messages,
            summary=_summary(body.summary),
            classification=Classification[body.classification.name],
            compartments=frozenset(body.compartments))
        return _response(value)

    @router.get("/{checkpoint_id}/review", response_model=SemanticCheckpointResponse)
    async def read_for_review(checkpoint_id: str, request: Request,
                              authenticated: Authenticated = Depends(authenticator)):
        return _response(await run_in_threadpool(_service(request).read_for_review,
            principal=authenticated.principal, checkpoint_id=checkpoint_id))

    @router.post("/{checkpoint_id}/review", response_model=SemanticCheckpointResponse)
    async def review(checkpoint_id: str, body: SemanticCheckpointReviewBody,
                     request: Request,
                     authenticated: Authenticated = Depends(authenticator)):
        return _response(await run_in_threadpool(_service(request).review,
            principal=authenticated.principal, checkpoint_id=checkpoint_id,
            expected_version=body.expected_version, approve=body.approve,
            reason=body.reason))

    @router.post("/{checkpoint_id}:resume",
                 response_model=SemanticCheckpointResumeResponse)
    async def resume(checkpoint_id: str, request: Request,
                     authenticated: Authenticated = Depends(authenticator)):
        value = await run_in_threadpool(_service(request).resume_message,
            principal=authenticated.principal, checkpoint_id=checkpoint_id)
        return SemanticCheckpointResumeResponse(role=value.role, content=value.content)

    return router


def _service(request: Request) -> SemanticCheckpointService:
    value = getattr(request.app.state, "semantic_checkpoint_service", None)
    if value is None:
        raise RuntimeError("semantic checkpoint service is not configured")
    return value


def _summary(value: SemanticSummaryBody) -> SemanticSummary:
    return SemanticSummary(objective=value.objective,
        constraints=tuple(value.constraints), decisions=tuple(value.decisions),
        open_items=tuple(value.open_items), verified_facts=tuple(value.verified_facts))


def _response(value) -> SemanticCheckpointResponse:
    return SemanticCheckpointResponse(checkpoint_id=value.checkpoint_id,
        conversation_id=value.conversation_id, status=value.status,
        version=value.version, source_digest=value.source_digest,
        summary=SemanticSummaryBody(**SemanticCheckpointService._summary_json(value.summary)))
