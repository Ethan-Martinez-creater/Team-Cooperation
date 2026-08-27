from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from ..errors import GovernanceConflictError, HarnessError
from ..product.models import (
    ConversationMessageKind,
    ExchangeRecipientStatus,
    TurnTriggerKind,
)
from .auth import Authenticated, BearerAuthenticator
from .conversation_routes import start_exchange_draft_run, start_exchange_reply_turn
from .exchange_models import (
    AgentExchangeDraftApproveBody,
    AgentExchangeDraftBody,
    AgentExchangeDraftRejectBody,
    AgentExchangeDraftUpdateBody,
    AgentExchangeDraftView,
    AgentExchangeGenerateBody,
    AgentExchangeRecipientView,
    AgentExchangeResponseBody,
    AgentExchangeResponseView,
    AgentExchangeView,
    ExchangeGenerateResult,
)


def build_exchange_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(tags=["agent-exchanges"])

    @router.get(
        "/v1/projects/{project_id}/agent-exchanges",
        response_model=list[AgentExchangeView],
    )
    async def list_exchanges(
        project_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[AgentExchangeView]:
        service = _service(request)
        values = await run_in_threadpool(
            service.list_exchanges,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )
        return [AgentExchangeView.from_exchange(value) for value in values]

    @router.get(
        "/v1/projects/{project_id}/agent-exchanges/{exchange_id}",
        response_model=AgentExchangeView,
    )
    async def get_exchange(
        project_id: str,
        exchange_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeView:
        service = _service(request)
        value = await run_in_threadpool(
            service.get_exchange,
            project_id=project_id,
            exchange_id=exchange_id,
            actor_id=authenticated.principal.principal_id,
        )
        return AgentExchangeView.from_exchange(value)

    @router.get(
        "/v1/projects/{project_id}/agent-exchanges/{exchange_id}/recipients",
        response_model=list[AgentExchangeRecipientView],
    )
    async def list_recipients(
        project_id: str,
        exchange_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[AgentExchangeRecipientView]:
        service = _service(request)
        values = await run_in_threadpool(
            service.list_recipients,
            project_id=project_id,
            exchange_id=exchange_id,
            actor_id=authenticated.principal.principal_id,
        )
        caller_team = authenticated.principal.tenant_id
        views = []
        for value in values:
            own = caller_team == value.recipient_team_id
            snapshot = dict(value.context_snapshot)
            if not own:
                # The source team never sees a recipient's private resource
                # IDs; only the recipient team's own snapshot carries them.
                snapshot = {
                    key: item
                    for key, item in snapshot.items()
                    if key != "own_team_private_resource_ids"
                }
            views.append(
                AgentExchangeRecipientView(
                    exchange_id=value.exchange_id,
                    recipient_team_id=value.recipient_team_id,
                    context_snapshot=snapshot,
                    status=value.status,
                    response_id=value.response_id,
                    responded_at=(
                        value.responded_at.isoformat() if value.responded_at else None
                    ),
                    created_at=value.created_at.isoformat(),
                    # A reply draft is only visible to the team that drafted
                    # it; the source team sees it only after submission.
                    draft_content=value.draft_content if own else None,
                )
            )
        return views

    @router.get(
        "/v1/projects/{project_id}/agent-exchanges/{exchange_id}/responses",
        response_model=list[AgentExchangeResponseView],
    )
    async def list_responses(
        project_id: str,
        exchange_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[AgentExchangeResponseView]:
        service = _service(request)
        values = await run_in_threadpool(
            service.list_responses,
            project_id=project_id,
            exchange_id=exchange_id,
            actor_id=authenticated.principal.principal_id,
        )
        return [AgentExchangeResponseView.from_response(value) for value in values]

    @router.get(
        "/v1/projects/{project_id}/agent-exchange-drafts",
        response_model=list[AgentExchangeDraftView],
    )
    async def list_drafts(
        project_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[AgentExchangeDraftView]:
        service = _service(request)
        values = await run_in_threadpool(
            service.list_drafts,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )
        return [AgentExchangeDraftView.from_draft(value) for value in values]

    @router.post(
        "/v1/projects/{project_id}/agent-exchange-drafts",
        response_model=AgentExchangeDraftView,
        status_code=201,
    )
    async def create_draft(
        project_id: str,
        body: AgentExchangeDraftBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeDraftView:
        service = _service(request)
        value = await run_in_threadpool(
            service.create_draft,
            draft_id=f"draft-{secrets.token_hex(12)}",
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            purpose=body.purpose,
            summary=body.summary,
            request=body.request,
            constraints=body.constraints,
            shared_resource_ids=body.shared_resource_ids,
            recipient_team_ids=body.recipient_team_ids,
            source_conversation_id=body.source_conversation_id,
            source_turn_id=body.source_turn_id,
        )
        return AgentExchangeDraftView.from_draft(value)

    @router.patch(
        "/v1/projects/{project_id}/agent-exchange-drafts/{draft_id}",
        response_model=AgentExchangeDraftView,
    )
    async def update_draft(
        project_id: str,
        draft_id: str,
        body: AgentExchangeDraftUpdateBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeDraftView:
        service = _service(request)
        value = await run_in_threadpool(
            service.update_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            expected_version=body.expected_version,
            purpose=body.purpose,
            summary=body.summary,
            request=body.request,
            constraints=body.constraints,
            shared_resource_ids=body.shared_resource_ids,
            recipient_team_ids=body.recipient_team_ids,
        )
        return AgentExchangeDraftView.from_draft(value)

    @router.post(
        "/v1/projects/{project_id}/agent-exchange-drafts/{draft_id}:approve",
        response_model=AgentExchangeView,
    )
    async def approve_draft(
        project_id: str,
        draft_id: str,
        body: AgentExchangeDraftApproveBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeView:
        service = _service(request)
        value = await run_in_threadpool(
            service.approve_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            expected_version=body.expected_version,
        )
        # Best effort: every recipient team that already has an open project
        # conversation gets its own Agent turn drafting a reply. Teams without
        # a conversation yet can trigger the turn manually from the UI.
        await launch_recipient_draft_turns(
            request=request,
            project_id=project_id,
            exchange_id=value.exchange_id,
            actor_id=authenticated.principal.principal_id,
        )
        return AgentExchangeView.from_exchange(value)

    @router.post(
        "/v1/projects/{project_id}/agent-exchanges/{exchange_id}:draft-turn",
        response_model=AgentExchangeView,
    )
    async def draft_reply_turn(
        project_id: str,
        exchange_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeView:
        """Let the calling recipient team's Agent draft a reply to an exchange."""
        service = _service(request)
        actor = authenticated.principal
        recipient = await run_in_threadpool(
            service.get_recipient,
            exchange_id=exchange_id,
            recipient_team_id=actor.tenant_id,
        )
        if recipient.status is ExchangeRecipientStatus.RESPONDED:
            raise GovernanceConflictError("this team has already responded")
        if recipient.draft_turn_id:
            raise GovernanceConflictError("this team is already drafting a reply")
        workspace = getattr(request.app.state, "project_workspace_service", None)
        agent_run_service = getattr(request.app.state, "agent_run_service", None)
        if workspace is None or agent_run_service is None:
            raise HarnessError("agent run service is unavailable")
        conversation = await run_in_threadpool(
            workspace.conversation_for_team,
            project_id=project_id,
            team_id=actor.tenant_id,
        )
        if conversation is None:
            raise HarnessError("this team has no open project conversation yet")
        message, turn = await run_in_threadpool(
            workspace.append_user_message,
            conversation_id=conversation.conversation_id,
            actor_id=actor.principal_id,
            content="（收到跨团队 Agent 共享请求，请本团队 Agent 起草回复草案）",
            idempotency_key=f"exchange-reply-trigger:{exchange_id}:{actor.tenant_id}",
            trigger_kind=TurnTriggerKind.EXCHANGE,
            message_kind=ConversationMessageKind.SYSTEM,
        )
        try:
            await start_exchange_reply_turn(
                request=request,
                project_id=project_id,
                exchange_id=exchange_id,
                recipient_team_id=actor.tenant_id,
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                actor_id=actor.principal_id,
            )
        except Exception:
            await run_in_threadpool(
                workspace.fail_turn,
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
            )
            raise
        value = await run_in_threadpool(
            service.get_exchange,
            project_id=project_id,
            exchange_id=exchange_id,
            actor_id=actor.principal_id,
        )
        return AgentExchangeView.from_exchange(value)

    @router.post(
        "/v1/projects/{project_id}/agent-exchange-drafts:generate",
        response_model=ExchangeGenerateResult,
        status_code=202,
    )
    async def generate_draft_from_conversation(
        project_id: str,
        body: AgentExchangeGenerateBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> ExchangeGenerateResult:
        """Have the source team's Agent draft an exchange from the conversation.

        The endpoint only schedules the Agent turn; terminal projection turns
        the assistant output into a DRAFTING exchange draft the user still has
        to edit and approve. The source conversation is verified to belong to
        this project and account before any turn is created.
        """
        service = _service(request)
        actor = authenticated.principal
        if not body.recipient_team_ids:
            raise ValueError("at least one recipient team is required")
        if body.source_conversation_id is None:
            raise ValueError("source conversation is required")
        await run_in_threadpool(
            service.assert_conversation_owned,
            project_id=project_id,
            conversation_id=body.source_conversation_id,
            actor_id=actor.principal_id,
        )
        workspace = getattr(request.app.state, "project_workspace_service", None)
        agent_run_service = getattr(request.app.state, "agent_run_service", None)
        if workspace is None or agent_run_service is None:
            raise HarnessError("agent run service is unavailable")
        message, turn = await run_in_threadpool(
            workspace.append_user_message,
            conversation_id=body.source_conversation_id,
            actor_id=actor.principal_id,
            content="（请本团队 Agent 根据对话起草跨团队共享草稿）",
            idempotency_key=f"exchange-draft:{body.source_conversation_id}:{uuid.uuid4().hex}",
            trigger_kind=TurnTriggerKind.EXCHANGE_DRAFT,
            message_kind=ConversationMessageKind.SYSTEM,
        )
        try:
            await start_exchange_draft_run(
                request=request,
                project_id=project_id,
                conversation_id=body.source_conversation_id,
                turn_id=turn.turn_id,
                actor_id=actor.principal_id,
                principal=actor,
                recipient_team_ids=body.recipient_team_ids,
                shared_resource_ids=body.shared_resource_ids,
            )
        except Exception:
            await run_in_threadpool(
                workspace.fail_turn,
                conversation_id=body.source_conversation_id,
                turn_id=turn.turn_id,
            )
            raise
        return ExchangeGenerateResult(
            turn_id=turn.turn_id,
            message="Agent 正在根据对话起草共享草稿，完成后会出现在协作面板",
        )

    @router.post(
        "/v1/projects/{project_id}/agent-exchange-drafts/{draft_id}:reject",
        response_model=AgentExchangeDraftView,
    )
    async def reject_draft(
        project_id: str,
        draft_id: str,
        body: AgentExchangeDraftRejectBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeDraftView:
        service = _service(request)
        value = await run_in_threadpool(
            service.reject_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            reason=body.reason,
        )
        return AgentExchangeDraftView.from_draft(value)

    @router.post(
        "/v1/projects/{project_id}/agent-exchanges/{exchange_id}/responses",
        response_model=AgentExchangeResponseView,
        status_code=201,
    )
    async def submit_response(
        project_id: str,
        exchange_id: str,
        body: AgentExchangeResponseBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ) -> AgentExchangeResponseView:
        service = _service(request)
        value = await run_in_threadpool(
            service.submit_response,
            project_id=project_id,
            exchange_id=exchange_id,
            actor_id=authenticated.principal.principal_id,
            content=body.content,
            turn_id=body.turn_id,
        )
        return AgentExchangeResponseView.from_response(value)

    return router


def _service(request: Request):
    service = getattr(request.app.state, "agent_exchange_service", None)
    if service is None:
        raise HarnessError("agent exchange service is unavailable")
    return service


async def launch_recipient_draft_turns(
    *, request: Request, project_id: str, exchange_id: str, actor_id: str
) -> None:
    """Start reply-drafting turns for every recipient team with a conversation.

    Best effort by design: teams without an open project conversation, or with
    a busy conversation, are skipped and can trigger the turn from the UI.
    """
    service = _service(request)
    workspace = getattr(request.app.state, "project_workspace_service", None)
    if workspace is None:
        return
    recipients = await run_in_threadpool(
        service.list_recipients,
        project_id=project_id,
        exchange_id=exchange_id,
        actor_id=actor_id,
    )
    for recipient in recipients:
        if recipient.status is not ExchangeRecipientStatus.PENDING:
            continue
        if recipient.draft_turn_id:
            continue
        conversation = await run_in_threadpool(
            workspace.conversation_for_team,
            project_id=project_id,
            team_id=recipient.recipient_team_id,
        )
        if conversation is None:
            continue
        turn = None
        try:
            _, turn = await run_in_threadpool(
                workspace.append_user_message,
                conversation_id=conversation.conversation_id,
                actor_id=conversation.account_id,
                content="（收到跨团队 Agent 共享请求，请本团队 Agent 起草回复草案）",
                idempotency_key=f"exchange-reply:{exchange_id}:{recipient.recipient_team_id}:{uuid.uuid4().hex}",
                trigger_kind=TurnTriggerKind.EXCHANGE,
                message_kind=ConversationMessageKind.SYSTEM,
            )
            await start_exchange_reply_turn(
                request=request,
                project_id=project_id,
                exchange_id=exchange_id,
                recipient_team_id=recipient.recipient_team_id,
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                actor_id=conversation.account_id,
            )
        except Exception:  # noqa: BLE001
            # A turn already written must be terminated, otherwise the
            # recipient's conversation locks on an active turn that will never
            # run. The recipient can still trigger the drafting from the UI.
            if turn is not None:
                try:
                    await run_in_threadpool(
                        workspace.fail_turn,
                        conversation_id=conversation.conversation_id,
                        turn_id=turn.turn_id,
                    )
                except Exception:  # noqa: BLE001
                    pass
            continue
