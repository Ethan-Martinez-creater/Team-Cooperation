from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ..artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance
from ..errors import AuthenticationError
from ..agent_runs import DurableRunStatus
from ..product import (
    NotificationCategory,
    NotificationPreference,
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    DataPropagation,
    ProjectResourceService,
    ResourceAction,
    TaskPriority,
    TeamCollaborationService,
    TurnTriggerKind,
    compute_team_task_schedule,
)
from ..security import Classification, ResourceLabel
logger = logging.getLogger("coifesp.product_routes")

from .auth import Authenticated, BearerAuthenticator
from .conversation_routes import launch_conversation_turn_run
from .product_models import (
    AccountRegistrationBody,
    AccountRegistrationDecisionBody,
    AccountRegistrationView,
    AgentProjectBriefView,
    CollaborationDraftDecisionBody,
    CollaborationDraftUpdateBody,
    CollaborationDraftView,
    CollaborationInboxActionView,
    CollaborationInboxActivityView,
    CollaborationInboxTaskView,
    CollaborationInboxView,
    AccountView,
    InitialPasswordChangeBody,
    InboxAgentRunView,
    NotificationIdsBody,
    NotificationPageView,
    NotificationPreferenceView,
    NotificationView,
    ProjectCreateBody,
    ProjectCreateResultView,
    ProjectTeamAddBody,
    ProjectActivityView,
    ProjectDetailView,
    ProjectMessageCreateBody,
    ProjectMessageView,
    ProjectNotificationView,
    ProjectTopicContributionBody,
    ProjectTopicContributionView,
    ProjectTopicCreateBody,
    ProjectTopicDecisionBody,
    ProjectTopicView,
    ProjectAgentRunView,
    ProjectResourceCreateBody,
    ProjectResourceShareBody,
    ProjectResourceView,
    ProjectTeamView,
    ProjectView,
    SessionCreateBody,
    SessionView,
    TaskScheduleChangeBody,
    TaskScheduleChangeResultView,
    TaskScheduleProposalDecisionBody,
    TaskScheduleProposalView,
    TeamRegistrationBody,
    TeamRegistrationView,
    TeamDirectoryEntryView,
    TeamDirectoryPageView,
    TeamTaskAssignBody,
    TeamTaskCreateBody,
    TeamTaskDecisionBody,
    TeamTaskReviewBody,
    TeamTaskSubmitBody,
    TeamTaskView,
    TeamRelationCreateBody,
    TeamRelationSummaryView,
    TeamRelationView,
    TeamView,
    TeamRelationDecisionBody,
)


def build_product_router(
    *,
    authenticator: BearerAuthenticator,
    accounts: ProductAccountService,
    directory: ProjectDirectoryService,
    resources: ProjectResourceService | None = None,
    collaboration: TeamCollaborationService | None = None,
    notifications: NotificationService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["product"])

    @router.post("/teams/register", response_model=TeamRegistrationView, status_code=201)
    async def register_team(body: TeamRegistrationBody) -> TeamRegistrationView:
        team, administrator = accounts.register_team(
            team_id=f"team-{secrets.token_hex(12)}", team_handle=body.handle, team_name=body.name
        )
        return TeamRegistrationView(
            team=TeamView(
                team_id=team.team_id, handle=team.handle, name=team.name, created_at=team.created_at
            ),
            administrator_username=administrator.username,
            administrator_initial_password=administrator.initial_password,
        )

    @router.get("/teams", response_model=list[TeamView])
    async def teams() -> list[TeamView]:
        return [
            TeamView(
                team_id=item.team_id, handle=item.handle, name=item.name, created_at=item.created_at
            )
            for item in accounts.list_teams()
        ]

    @router.get("/team-directory", response_model=TeamDirectoryPageView)
    async def team_directory(
        q: str = Query(default="", max_length=256),
        after_handle: str | None = Query(
            default=None,
            min_length=3,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$",
        ),
        limit: int = Query(default=50, ge=1, le=100),
        authenticated: Authenticated = Depends(authenticator),
    ):
        page = await run_in_threadpool(
            accounts.search_team_directory,
            actor_id=authenticated.principal.principal_id,
            query=q,
            after_handle=after_handle,
            limit=limit,
        )
        return TeamDirectoryPageView(
            items=[
                TeamDirectoryEntryView(
                    team=TeamView(
                        team_id=item.team.team_id,
                        handle=item.team.handle,
                        name=item.team.name,
                        created_at=item.team.created_at,
                    ),
                    relationship=item.relationship,
                )
                for item in page.items
            ],
            next_after_handle=page.next_after_handle,
        )

    @router.post("/accounts/register", response_model=AccountRegistrationView, status_code=202)
    async def register(body: AccountRegistrationBody):
        account_id = f"acct-{secrets.token_hex(12)}"
        common = dict(
            account_id=account_id,
            username=body.username,
            display_name=body.display_name,
            email=body.email,
            password=body.password,
        )
        return accounts.request_account_registration(**common, team_id=body.team_id)

    @router.post("/accounts/change-initial-password", status_code=204)
    async def change_initial_password(body: InitialPasswordChangeBody, response: Response) -> None:
        accounts.change_initial_password(
            login=body.login, current_password=body.current_password, new_password=body.new_password
        )
        response.status_code = 204

    @router.post("/sessions", response_model=SessionView)
    async def login(body: SessionCreateBody) -> SessionView:
        return _session(accounts.login(login=body.login, password=body.password))

    @router.delete("/sessions/current", status_code=204)
    async def logout(response: Response, authorization: str | None = Header(default=None)) -> None:
        token = _bearer(authorization)
        accounts.authenticate(token)
        accounts.logout(token)
        response.status_code = 204

    @router.post("/sessions/current:renew", response_model=SessionView)
    async def renew_session(authorization: str | None = Header(default=None)) -> SessionView:
        token = _bearer(authorization)
        return _session(accounts.renew_session(token))

    @router.post("/sessions:revoke-all", status_code=204)
    async def revoke_all_sessions(response: Response,
                                  authenticated: Authenticated = Depends(authenticator)) -> None:
        accounts.revoke_all_sessions(authenticated.principal.principal_id)
        response.status_code = 204

    @router.get("/accounts/me", response_model=AccountView)
    async def me(authenticated: Authenticated = Depends(authenticator)) -> AccountView:
        return _account(accounts.get_account(authenticated.principal.principal_id))

    @router.get(
        "/teams/current/account-registrations", response_model=list[AccountRegistrationView]
    )
    async def pending_registrations(authenticated: Authenticated = Depends(authenticator)):
        return accounts.list_pending_registrations(authenticated.principal.principal_id)

    @router.get("/teams/current/accounts", response_model=list[AccountView])
    async def team_accounts(
        authenticated: Authenticated = Depends(authenticator),
    ) -> list[AccountView]:
        return [
            _account(item)
            for item in accounts.list_team_accounts(authenticated.principal.principal_id)
        ]

    @router.post(
        "/teams/current/account-registrations/{account_id}:decide",
        response_model=AccountRegistrationView,
    )
    async def decide_registration(
        account_id: str,
        body: AccountRegistrationDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return accounts.decide_account_registration(
            actor_id=authenticated.principal.principal_id, account_id=account_id, accept=body.accept
        )

    @router.post("/team-relations/requests", response_model=TeamRelationView, status_code=201)
    async def request_relation(
        body: TeamRelationCreateBody, authenticated: Authenticated = Depends(authenticator)
    ):
        return accounts.send_team_relation_request(
            request_id=f"relation-{secrets.token_hex(12)}",
            actor_id=authenticated.principal.principal_id,
            recipient_team_handle=body.recipient_team_handle,
            message=body.message,
        )

    @router.get("/team-relations/requests", response_model=list[TeamRelationView])
    async def relation_requests(authenticated: Authenticated = Depends(authenticator)):
        return accounts.list_team_relation_requests(authenticated.principal.principal_id)

    @router.get("/team-relations", response_model=list[TeamRelationSummaryView])
    async def relations(authenticated: Authenticated = Depends(authenticator)):
        return accounts.list_team_relations(authenticated.principal.principal_id)

    @router.post("/team-relations/requests/{request_id}:decide", response_model=TeamRelationView)
    async def decide_relation(
        request_id: str,
        body: TeamRelationDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return accounts.decide_team_relation_request(
            request_id=request_id, actor_id=authenticated.principal.principal_id, accept=body.accept
        )

    @router.post("/projects", response_model=ProjectCreateResultView, status_code=201)
    async def create_project(
        body: ProjectCreateBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        actor_id = authenticated.principal.principal_id
        project = directory.create_project(
            project_id=f"project-{secrets.token_hex(12)}",
            name=body.name,
            description=body.description,
            actor_id=actor_id,
            owner_assignment_name=body.owner_assignment_name,
            owner_kind=body.owner_kind,
        )
        workspace = getattr(request.app.state, "project_workspace_service", None)
        if workspace is None:
            # Workspace service is optional for project creation so callers
            # without the persistent conversation layer keep working.
            return ProjectCreateResultView(
                project_id=project.project_id,
                name=project.name,
                description=project.description,
                owner_team_id=project.owner_team_id,
                created_by=project.created_by,
                created_at=project.created_at,
                conversation_id=None,
            )
        conversation = await run_in_threadpool(
            workspace.ensure_conversation,
            project_id=project.project_id,
            actor_id=actor_id,
        )
        if body.initial_brief:
            message, turn = await run_in_threadpool(
                workspace.append_user_message,
                conversation_id=conversation.conversation_id,
                actor_id=actor_id,
                content=body.initial_brief,
                idempotency_key=f"create-brief-{project.project_id}",
                expected_last_sequence=0,
                trigger_kind=TurnTriggerKind.PLANNING,
            )
            try:
                await launch_conversation_turn_run(
                    request=request,
                    authenticated=authenticated,
                    project_id=project.project_id,
                    conversation_id=conversation.conversation_id,
                    turn_id=turn.turn_id,
                    user_message=message.content,
                    user_message_sequence=message.sequence,
                    trigger_kind="planning",
                )
            except Exception:
                # The project and conversation are already persisted; a failed
                # planning run must not surface as a project creation failure.
                logger.exception(
                    "planning run launch failed project_id=%s turn_id=%s",
                    project.project_id,
                    turn.turn_id,
                )
        return ProjectCreateResultView(
            project_id=project.project_id,
            name=project.name,
            description=project.description,
            owner_team_id=project.owner_team_id,
            created_by=project.created_by,
            created_at=project.created_at,
            conversation_id=conversation.conversation_id,
        )

    @router.get("/projects", response_model=list[ProjectView])
    async def projects(authenticated: Authenticated = Depends(authenticator)):
        return directory.list_projects(authenticated.principal.principal_id)

    @router.get("/projects/{project_id}", response_model=ProjectDetailView)
    async def project(project_id: str, authenticated: Authenticated = Depends(authenticator)):
        return directory.get_project(
            project_id=project_id, actor_id=authenticated.principal.principal_id
        )

    @router.post("/projects/{project_id}/teams", response_model=ProjectTeamView, status_code=201)
    async def add_project_team(
        project_id: str,
        body: ProjectTeamAddBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return directory.add_team(
            project_id=project_id,
            team_id=body.team_id,
            name=body.assignment_name,
            kind=body.kind,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post(
        "/projects/{project_id}/resources", response_model=ProjectResourceView, status_code=201
    )
    async def publish_resource(
        project_id: str,
        body: ProjectResourceCreateBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        service = _resources(resources)
        return await run_in_threadpool(
            service.publish,
            resource_id=f"resource-{secrets.token_hex(12)}",
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            title=body.title,
            propagation=body.propagation,
            artifact_id=body.artifact_id,
            artifact_sha256=body.artifact_sha256,
        )

    @router.post(
        "/projects/{project_id}/resources:upload",
        response_model=ProjectResourceView,
        status_code=201,
    )
    async def upload_project_resource(
        project_id: str,
        request: Request,
        response: Response,
        title: str = Form(..., min_length=1, max_length=256),
        propagation: DataPropagation = Form(...),
        content: UploadFile = File(...),
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
        authenticated: Authenticated = Depends(authenticator),
    ):
        """Upload immutable bytes and bind them to a project in one recoverable command."""
        resource_service = _resources(resources)
        content_service = getattr(request.app.state, "artifact_content_service", None)
        if content_service is None:
            from ..config import ConfigurationError

            raise ConfigurationError("artifact content storage is not configured")
        principal = authenticated.principal
        team_id = await run_in_threadpool(
            resource_service.publishing_team, project_id=project_id, actor_id=principal.principal_id
        )
        if team_id != principal.tenant_id:
            from ..errors import PolicyDenied

            raise PolicyDenied("authenticated team does not match the product account")
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("project resource title is required")

        digest, size = hashlib.sha256(), 0
        maximum = request.app.state.settings.artifact_max_upload_bytes
        while chunk := await content.read(1_048_576):
            size += len(chunk)
            if size > maximum:
                raise ValueError("project resource exceeds the configured upload limit")
            digest.update(chunk)
        await content.seek(0)
        command_digest = hashlib.sha256(
            f"{team_id}\0{project_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
        artifact_id = f"project-file-{command_digest[:32]}"
        resource_id = f"resource-{command_digest[:32]}"
        storage_key = f"project-upload-{command_digest[:32]}"
        media_type = (content.content_type or "application/octet-stream").lower()
        metadata_digest = hashlib.sha256(
            f"{project_id}\0{clean_title}\0{propagation.value}".encode("utf-8")
        ).hexdigest()
        manifest = ArtifactManifest(
            artifact_id,
            _artifact_kind(content.filename, media_type),
            media_type,
            f"artifact-store://{team_id}/pending",
            digest.hexdigest(),
            size,
            ResourceLabel(team_id, Classification.INTERNAL, frozenset(), f"artifact:{artifact_id}"),
            ArtifactProvenance(
                principal.principal_id,
                team_id,
                "workspace.project-upload",
                f"1:{metadata_digest}",
                datetime.now(UTC),
            ),
            frozenset({team_id}),
        )

        def chunks():
            while value := content.file.read(1_048_576):
                yield value

        published, duplicate = await run_in_threadpool(
            content_service.publish,
            principal=principal,
            idempotency_key=storage_key,
            manifest=manifest,
            chunks=chunks(),
        )
        result = await run_in_threadpool(
            resource_service.publish,
            resource_id=resource_id,
            project_id=project_id,
            actor_id=principal.principal_id,
            title=clean_title,
            propagation=propagation,
            artifact_id=published.artifact_id,
            artifact_sha256=published.sha256,
            allow_existing=True,
        )
        if duplicate:
            response.status_code = 200
        return result

    @router.get("/projects/{project_id}/resources", response_model=list[ProjectResourceView])
    async def project_resources(
        project_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _resources(resources).list_visible,
            actor_id=authenticated.principal.principal_id,
            project_id=project_id,
        )

    @router.get(
        "/projects/{project_id}/resources/{resource_id}", response_model=ProjectResourceView
    )
    async def project_resource(
        project_id: str, resource_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _resources(resources).get_authorized,
            actor_id=authenticated.principal.principal_id,
            resource_id=resource_id,
            action=ResourceAction.VIEW,
            project_id=project_id,
        )

    @router.get("/projects/{project_id}/resources/{resource_id}/content")
    async def download_resource(
        project_id: str,
        resource_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        resource = await run_in_threadpool(
            _resources(resources).get_authorized,
            actor_id=authenticated.principal.principal_id,
            resource_id=resource_id,
            action=ResourceAction.DOWNLOAD,
            project_id=project_id,
        )
        content_service = getattr(request.app.state, "artifact_content_service", None)
        if content_service is None:
            from ..config import ConfigurationError

            raise ConfigurationError("artifact content storage is not configured")
        stream = await run_in_threadpool(
            content_service.open_policy_authorized,
            owner_tenant_id=resource.artifact_owner_team_id,
            sha256=resource.artifact_sha256,
        )
        return StreamingResponse(
            stream,
            media_type=resource.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{resource.artifact_id}"',
                "Digest": f"sha-256={resource.artifact_sha256}",
            },
        )

    @router.get("/projects/{project_id}/resources/{resource_id}/preview")
    async def preview_resource(
        project_id: str,
        resource_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        resource = await run_in_threadpool(
            _resources(resources).get_authorized,
            actor_id=authenticated.principal.principal_id,
            resource_id=resource_id,
            action=ResourceAction.VIEW,
            project_id=project_id,
        )
        content_service = getattr(request.app.state, "artifact_content_service", None)
        if content_service is None:
            from ..config import ConfigurationError

            raise ConfigurationError("artifact content storage is not configured")
        stream = await run_in_threadpool(
            content_service.open_policy_authorized,
            owner_tenant_id=resource.artifact_owner_team_id,
            sha256=resource.artifact_sha256,
        )
        return StreamingResponse(
            stream,
            media_type=resource.media_type,
            headers={
                "Content-Disposition": "inline",
                "Digest": f"sha-256={resource.artifact_sha256}",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @router.post(
        "/projects/{project_id}/resources/{resource_id}:save", response_model=ProjectResourceView
    )
    async def save_resource(
        project_id: str, resource_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _resources(resources).save_to_library,
            actor_id=authenticated.principal.principal_id,
            resource_id=resource_id,
            project_id=project_id,
        )

    @router.post(
        "/projects/{project_id}/resources/{resource_id}:share", response_model=ProjectResourceView
    )
    async def share_resource(
        project_id: str,
        resource_id: str,
        body: ProjectResourceShareBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _resources(resources).share_with_team,
            share_id=f"share-{secrets.token_hex(12)}",
            actor_id=authenticated.principal.principal_id,
            resource_id=resource_id,
            recipient_team_id=body.recipient_team_id,
            project_id=project_id,
        )

    @router.get("/projects/{project_id}/messages", response_model=list[ProjectMessageView])
    async def messages(project_id: str, authenticated: Authenticated = Depends(authenticator)):
        return await run_in_threadpool(
            _collaboration(collaboration).list_messages,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post(
        "/projects/{project_id}/messages", response_model=ProjectMessageView, status_code=201
    )
    async def send_message(
        project_id: str,
        body: ProjectMessageCreateBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).send_message,
            message_id=f"message-{secrets.token_hex(12)}",
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            target_team_id=body.target_team_id,
            content=body.content,
        )

    @router.get("/projects/{project_id}/tasks", response_model=list[TeamTaskView])
    async def team_tasks(project_id: str, authenticated: Authenticated = Depends(authenticator)):
        return await run_in_threadpool(
            _collaboration(collaboration).list_tasks,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post("/projects/{project_id}/tasks", response_model=TeamTaskView, status_code=201)
    async def create_team_task(
        project_id: str,
        body: TeamTaskCreateBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return _task_view(await run_in_threadpool(
            _collaboration(collaboration).create_task,
            task_id=f"task-{secrets.token_hex(12)}",
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            target_team_id=body.target_team_id,
            title=body.title,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            priority=body.priority,
            due_at=body.due_at,
        ))

    @router.post("/projects/{project_id}/tasks/{task_id}:respond", response_model=TeamTaskView)
    async def respond_team_task(
        project_id: str,
        task_id: str,
        body: TeamTaskDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return _task_view(await run_in_threadpool(
            _collaboration(collaboration).respond_task,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
            accept=body.accept,
        ))

    @router.post("/projects/{project_id}/tasks/{task_id}:assign", response_model=TeamTaskView)
    async def assign_team_task(
        project_id: str,
        task_id: str,
        body: TeamTaskAssignBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return _task_view(await run_in_threadpool(
            _collaboration(collaboration).assign_internal,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
            account_id=body.account_id,
        ))

    @router.post("/projects/{project_id}/tasks/{task_id}:start", response_model=TeamTaskView)
    async def start_team_task(
        project_id: str, task_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return _task_view(await run_in_threadpool(
            _collaboration(collaboration).start_task,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
        ))

    @router.post("/projects/{project_id}/tasks/{task_id}:submit", response_model=TeamTaskView)
    async def submit_team_task(
        project_id: str,
        task_id: str,
        body: TeamTaskSubmitBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return _task_view(await run_in_threadpool(
            _collaboration(collaboration).submit_task,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
            resource_ids=body.resource_ids,
        ))

    @router.post("/projects/{project_id}/tasks/{task_id}:review", response_model=TeamTaskView)
    async def review_team_task(
        project_id: str,
        task_id: str,
        body: TeamTaskReviewBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return _task_view(await run_in_threadpool(
            _collaboration(collaboration).review_task,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
            accept=body.accept,
            note=body.note,
        ))

    @router.patch(
        "/projects/{project_id}/tasks/{task_id}/schedule",
        response_model=TaskScheduleChangeResultView,
    )
    async def change_task_schedule(
        project_id: str,
        task_id: str,
        body: TaskScheduleChangeBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        try:
            outcome, value = await run_in_threadpool(
                _collaboration(collaboration).change_task_schedule,
                project_id=project_id,
                task_id=task_id,
                actor_id=authenticated.principal.principal_id,
                priority=body.priority,
                due_at=body.due_at,
                clear_due_at=body.clear_due_at,
                expected_schedule_version=body.expected_schedule_version,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if outcome == "updated":
            return TaskScheduleChangeResultView(result="updated", task=_task_view(value))
        return TaskScheduleChangeResultView(result="proposed", proposal=_proposal_view(value))

    @router.get(
        "/projects/{project_id}/tasks/{task_id}/schedule-proposals",
        response_model=list[TaskScheduleProposalView],
    )
    async def task_schedule_proposals(
        project_id: str,
        task_id: str,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_schedule_proposals,
            project_id=project_id,
            task_id=task_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post(
        "/projects/{project_id}/tasks/{task_id}/schedule-proposals/{proposal_id}:decide",
        response_model=TaskScheduleProposalView,
    )
    async def decide_task_schedule_proposal(
        project_id: str,
        task_id: str,
        proposal_id: str,
        body: TaskScheduleProposalDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        try:
            return await run_in_threadpool(
                _collaboration(collaboration).decide_schedule_proposal,
                project_id=project_id,
                task_id=task_id,
                proposal_id=proposal_id,
                actor_id=authenticated.principal.principal_id,
                accept=body.accept,
                reason=body.reason,
                expected_proposal_version=body.expected_proposal_version,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/activities", response_model=list[ProjectActivityView])
    async def activities(
        project_id: str,
        after_sequence: int = 0,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_activities,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            after_sequence=after_sequence,
        )

    @router.get("/projects/{project_id}/agent-brief", response_model=AgentProjectBriefView)
    async def agent_brief(project_id: str, authenticated: Authenticated = Depends(authenticator)):
        brief = await run_in_threadpool(
            _collaboration(collaboration).agent_project_brief,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )
        return AgentProjectBriefView(project_id=project_id, brief=brief)

    @router.get("/project-notifications", response_model=list[ProjectNotificationView])
    async def project_notifications(authenticated: Authenticated = Depends(authenticator)):
        return await run_in_threadpool(
            _collaboration(collaboration).notification_summaries,
            actor_id=authenticated.principal.principal_id,
        )

    @router.get("/collaboration-inbox", response_model=CollaborationInboxView)
    async def collaboration_inbox(
        limit: int = Query(default=100, ge=1, le=500),
        priority: TaskPriority | None = Query(default=None),
        due_before: datetime | None = Query(default=None),
        due_within_hours: int | None = Query(default=None, ge=1, le=336),
        overdue_only: bool = Query(default=False),
        assigned_only: bool = Query(default=False),
        project_id: str | None = Query(default=None, min_length=1, max_length=128),
        authenticated: Authenticated = Depends(authenticator),
    ):
        value = await run_in_threadpool(
            _collaboration(collaboration).collaboration_inbox,
            actor_id=authenticated.principal.principal_id,
            limit=limit,
            priority=priority,
            due_before=due_before,
            due_within_hours=due_within_hours,
            overdue_only=overdue_only,
            assigned_only=assigned_only,
            project_id=project_id,
        )
        now = datetime.now(UTC)
        actions = []
        for item in value.actions:
            schedule = compute_team_task_schedule(
                status=item.task.status,
                priority=item.task.priority,
                due_at=item.task.due_at,
                schedule_version=item.task.schedule_version,
                now=now,
            )
            actions.append(
                CollaborationInboxActionView(
                    project_id=item.task.project_id,
                    project_name=item.project_name,
                    action=item.action,
                    task=CollaborationInboxTaskView(
                        task_id=item.task.task_id,
                        project_id=item.task.project_id,
                        source_team_id=item.task.source_team_id,
                        target_team_id=item.task.target_team_id,
                        title=item.task.title,
                        description=item.task.description,
                        acceptance_criteria=item.task.acceptance_criteria,
                        status=item.task.status,
                        assigned_to_me=(
                            item.task.assigned_account_id == authenticated.principal.principal_id
                        ),
                        updated_at=item.task.updated_at,
                        priority=item.task.priority,
                        due_at=item.task.due_at,
                        is_overdue=schedule.is_overdue,
                        is_due_soon=schedule.is_due_soon,
                        due_in_seconds=schedule.due_in_seconds,
                        schedule_version=item.task.schedule_version,
                    ),
                )
            )
        unread = [
            CollaborationInboxActivityView(
                project_id=item.activity.project_id,
                project_name=item.project_name,
                sequence=item.activity.sequence,
                actor_team_id=item.activity.actor_team_id,
                event_type=item.activity.event_type,
                subject_id=item.activity.subject_id,
                target_team_id=item.activity.target_team_id,
                summary=item.activity.summary,
                created_at=item.activity.created_at,
            )
            for item in value.unread_activities
        ]
        return CollaborationInboxView(
            action_count=value.action_count,
            unread_count=value.unread_count,
            actions=actions,
            unread_activities=unread,
        )

    @router.get("/collaboration-inbox/agent-runs", response_model=list[InboxAgentRunView])
    async def inbox_agent_runs(
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_inbox_agent_runs,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post(
        "/projects/{project_id}/notifications:mark-read", response_model=ProjectNotificationView
    )
    async def mark_project_notifications_read(
        project_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).mark_project_read,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.get("/projects/{project_id}/topics", response_model=list[ProjectTopicView])
    async def project_topics(
        project_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_topics,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post("/projects/{project_id}/topics", response_model=ProjectTopicView, status_code=201)
    async def create_project_topic(
        project_id: str,
        body: ProjectTopicCreateBody,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        if body.source_agent_run_id is not None:
            agent_runs = getattr(request.app.state, "agent_run_service", None)
            if agent_runs is None:
                raise HTTPException(status_code=503, detail="Agent run service is unavailable")
            await run_in_threadpool(
                agent_runs.get, principal=authenticated.principal, run_id=body.source_agent_run_id
            )
            await run_in_threadpool(
                _collaboration(collaboration).assert_project_agent_run,
                project_id=project_id,
                run_id=body.source_agent_run_id,
                actor_id=authenticated.principal.principal_id,
            )
        return await run_in_threadpool(
            _collaboration(collaboration).create_topic,
            topic_id=f"topic-{secrets.token_hex(12)}",
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
            title=body.title,
            context=body.context,
            source_agent_run_id=body.source_agent_run_id,
        )

    @router.get(
        "/projects/{project_id}/topics/{topic_id}/contributions",
        response_model=list[ProjectTopicContributionView],
    )
    async def topic_contributions(
        project_id: str, topic_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_topic_contributions,
            project_id=project_id,
            topic_id=topic_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.post(
        "/projects/{project_id}/topics/{topic_id}/contributions",
        response_model=ProjectTopicContributionView,
        status_code=201,
    )
    async def contribute_project_topic(
        project_id: str,
        topic_id: str,
        body: ProjectTopicContributionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).contribute_topic,
            contribution_id=f"contribution-{secrets.token_hex(12)}",
            project_id=project_id,
            topic_id=topic_id,
            actor_id=authenticated.principal.principal_id,
            content=body.content,
        )

    @router.post("/projects/{project_id}/topics/{topic_id}:decide", response_model=ProjectTopicView)
    async def decide_project_topic(
        project_id: str,
        topic_id: str,
        body: ProjectTopicDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).decide_topic,
            project_id=project_id,
            topic_id=topic_id,
            actor_id=authenticated.principal.principal_id,
            decision=body.decision,
        )

    @router.post(
        "/projects/{project_id}/agent-runs/{run_id}:import-drafts",
        response_model=list[CollaborationDraftView],
        status_code=201,
    )
    async def import_agent_drafts(
        project_id: str,
        run_id: str,
        request: Request,
        authenticated: Authenticated = Depends(authenticator),
    ):
        service = getattr(request.app.state, "agent_run_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Agent run service is unavailable")
        run = await run_in_threadpool(service.get, principal=authenticated.principal, run_id=run_id)
        if run.status is not DurableRunStatus.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail="Agent run must complete before importing collaboration drafts",
            )
        await run_in_threadpool(
            _collaboration(collaboration).assert_project_agent_run,
            project_id=project_id,
            run_id=run_id,
            actor_id=authenticated.principal.principal_id,
        )
        messages = await run_in_threadpool(
            service.conversation, principal=authenticated.principal, run_id=run_id
        )
        assistant = next((item for item in reversed(messages) if item["role"] == "assistant"), None)
        if assistant is None:
            raise HTTPException(
                status_code=422, detail="Agent run has no assistant collaboration action message"
            )
        try:
            return await run_in_threadpool(
                _collaboration(collaboration).import_action_drafts,
                project_id=project_id,
                actor_id=authenticated.principal.principal_id,
                run_id=run_id,
                message_sequence=assistant["sequence"],
                content=assistant["content"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get(
        "/projects/{project_id}/collaboration-drafts", response_model=list[CollaborationDraftView]
    )
    async def collaboration_drafts(
        project_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_action_drafts,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.get("/projects/{project_id}/agent-runs", response_model=list[ProjectAgentRunView])
    async def project_agent_runs(
        project_id: str, authenticated: Authenticated = Depends(authenticator)
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).list_project_agent_runs,
            project_id=project_id,
            actor_id=authenticated.principal.principal_id,
        )

    @router.put(
        "/projects/{project_id}/collaboration-drafts/{draft_id}",
        response_model=CollaborationDraftView,
    )
    async def update_collaboration_draft(
        project_id: str,
        draft_id: str,
        body: CollaborationDraftUpdateBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).update_action_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            expected_version=body.expected_version,
            payload=body.payload,
        )

    @router.post(
        "/projects/{project_id}/collaboration-drafts/{draft_id}:execute",
        response_model=CollaborationDraftView,
    )
    async def execute_collaboration_draft(
        project_id: str,
        draft_id: str,
        body: CollaborationDraftDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).execute_action_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            expected_version=body.expected_version,
        )

    @router.post(
        "/projects/{project_id}/collaboration-drafts/{draft_id}:reject",
        response_model=CollaborationDraftView,
    )
    async def reject_collaboration_draft(
        project_id: str,
        draft_id: str,
        body: CollaborationDraftDecisionBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        return await run_in_threadpool(
            _collaboration(collaboration).reject_action_draft,
            project_id=project_id,
            draft_id=draft_id,
            actor_id=authenticated.principal.principal_id,
            expected_version=body.expected_version,
            reason=body.reason,
        )

    @router.get("/notification-preferences", response_model=NotificationPreferenceView)
    async def get_notification_preferences(
        authenticated: Authenticated = Depends(authenticator),
    ):
        preference, quiet_now = await run_in_threadpool(
            _notifications(notifications).preferences_with_state,
            account_id=authenticated.principal.principal_id,
        )
        return NotificationPreferenceView(
            notify_tasks=preference.notify_tasks,
            notify_messages=preference.notify_messages,
            notify_resources=preference.notify_resources,
            notify_topics=preference.notify_topics,
            notify_agent_events=preference.notify_agent_events,
            notify_due_soon=preference.notify_due_soon,
            notify_overdue=preference.notify_overdue,
            due_soon_hours=preference.due_soon_hours,
            time_zone=preference.time_zone,
            quiet_start_minute=preference.quiet_start_minute,
            quiet_end_minute=preference.quiet_end_minute,
            updated_at=preference.updated_at,
            quiet_now=quiet_now,
        )

    @router.put("/notification-preferences", response_model=NotificationPreferenceView)
    async def put_notification_preferences(
        body: NotificationPreferenceView,
        authenticated: Authenticated = Depends(authenticator),
    ):
        try:
            preference = await run_in_threadpool(
                _notifications(notifications).put_preferences,
                account_id=authenticated.principal.principal_id,
                preference=NotificationPreference(
                    account_id=authenticated.principal.principal_id,
                    notify_tasks=body.notify_tasks,
                    notify_messages=body.notify_messages,
                    notify_resources=body.notify_resources,
                    notify_topics=body.notify_topics,
                    notify_agent_events=body.notify_agent_events,
                    notify_due_soon=body.notify_due_soon,
                    notify_overdue=body.notify_overdue,
                    due_soon_hours=body.due_soon_hours,
                    time_zone=body.time_zone,
                    quiet_start_minute=body.quiet_start_minute,
                    quiet_end_minute=body.quiet_end_minute,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _, quiet_now = await run_in_threadpool(
            _notifications(notifications).preferences_with_state,
            account_id=authenticated.principal.principal_id,
        )
        return NotificationPreferenceView(
            notify_tasks=preference.notify_tasks,
            notify_messages=preference.notify_messages,
            notify_resources=preference.notify_resources,
            notify_topics=preference.notify_topics,
            notify_agent_events=preference.notify_agent_events,
            notify_due_soon=preference.notify_due_soon,
            notify_overdue=preference.notify_overdue,
            due_soon_hours=preference.due_soon_hours,
            time_zone=preference.time_zone,
            quiet_start_minute=preference.quiet_start_minute,
            quiet_end_minute=preference.quiet_end_minute,
            updated_at=preference.updated_at,
            quiet_now=quiet_now,
        )

    @router.get("/notifications", response_model=NotificationPageView)
    async def notification_center(
        unread_only: bool = Query(default=False),
        archived_only: bool = Query(default=False),
        category: NotificationCategory | None = Query(default=None),
        project_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=256),
        authenticated: Authenticated = Depends(authenticator),
    ):
        page = await run_in_threadpool(
            _notifications(notifications).list_notifications,
            account_id=authenticated.principal.principal_id,
            unread_only=unread_only,
            archived_only=archived_only,
            category=category,
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )
        return NotificationPageView(
            items=[
                NotificationView(
                    notification_id=item.notification_id,
                    account_id=item.account_id,
                    project_id=item.project_id,
                    activity_sequence=item.activity_sequence,
                    category=item.category,
                    title=item.title,
                    summary=item.summary,
                    subject_id=item.subject_id,
                    read_at=item.read_at,
                    archived_at=item.archived_at,
                    created_at=item.created_at,
                )
                for item in page.items
            ],
            unread_count=page.unread_count,
            next_cursor=page.next_cursor,
        )

    @router.post("/notifications/{notification_id}:mark-read", response_model=NotificationView)
    async def mark_notification_read(
        notification_id: str,
        authenticated: Authenticated = Depends(authenticator),
    ):
        item = await run_in_threadpool(
            _notifications(notifications).mark_read,
            account_id=authenticated.principal.principal_id,
            notification_id=notification_id,
        )
        return _notification_view(item)

    @router.post("/notifications/{notification_id}:archive", response_model=NotificationView)
    async def archive_notification(
        notification_id: str,
        authenticated: Authenticated = Depends(authenticator),
    ):
        item = await run_in_threadpool(
            _notifications(notifications).archive,
            account_id=authenticated.principal.principal_id,
            notification_id=notification_id,
        )
        return _notification_view(item)

    @router.post("/notifications/{notification_id}:restore", response_model=NotificationView)
    async def restore_notification(
        notification_id: str,
        authenticated: Authenticated = Depends(authenticator),
    ):
        item = await run_in_threadpool(
            _notifications(notifications).restore,
            account_id=authenticated.principal.principal_id,
            notification_id=notification_id,
        )
        return _notification_view(item)

    @router.post("/notifications:mark-page-read", response_model=list[NotificationView])
    async def mark_notification_page_read(
        body: NotificationIdsBody,
        authenticated: Authenticated = Depends(authenticator),
    ):
        items = await run_in_threadpool(
            _notifications(notifications).mark_page_read,
            account_id=authenticated.principal.principal_id,
            notification_ids=body.notification_ids,
        )
        return [_notification_view(item) for item in items]

    return router


def _session(session) -> SessionView:
    return SessionView(
        access_token=session.token, expires_at=session.expires_at, account=_account(session.account)
    )


def _account(account) -> AccountView:
    return AccountView(
        account_id=account.account_id,
        username=account.username,
        display_name=account.display_name,
        email=account.email,
        team_id=account.team_id,
        team_role=account.team_role,
        registration_status=account.registration_status,
        must_change_password=account.must_change_password,
        created_at=account.created_at,
    )


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or not value[7:]:
        raise AuthenticationError("invalid_token")
    return value[7:]


def _resources(value: ProjectResourceService | None) -> ProjectResourceService:
    if value is None:
        from ..config import ConfigurationError

        raise ConfigurationError("project resource service is unavailable")
    return value


def _collaboration(value: TeamCollaborationService | None) -> TeamCollaborationService:
    if value is None:
        from ..config import ConfigurationError

        raise ConfigurationError("team collaboration service is unavailable")
    return value


def _notifications(value: NotificationService | None) -> NotificationService:
    if value is None:
        from ..config import ConfigurationError

        raise ConfigurationError("notification service is unavailable")
    return value


def _notification_view(item) -> NotificationView:
    return NotificationView(
        notification_id=item.notification_id,
        account_id=item.account_id,
        project_id=item.project_id,
        activity_sequence=item.activity_sequence,
        category=item.category,
        title=item.title,
        summary=item.summary,
        subject_id=item.subject_id,
        read_at=item.read_at,
        archived_at=item.archived_at,
        created_at=item.created_at,
    )


def _proposal_view(proposal) -> TaskScheduleProposalView:
    return TaskScheduleProposalView(
        proposal_id=proposal.proposal_id,
        project_id=proposal.project_id,
        task_id=proposal.task_id,
        proposed_by=proposal.proposed_by,
        proposed_by_team_id=proposal.proposed_by_team_id,
        decided_by_team_id=proposal.decided_by_team_id,
        old_priority=proposal.old_priority,
        new_priority=proposal.new_priority,
        old_due_at=proposal.old_due_at,
        new_due_at=proposal.new_due_at,
        reason=proposal.reason,
        decision_reason=proposal.decision_reason,
        status=proposal.status,
        version=proposal.version,
        schedule_version=proposal.schedule_version,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
    )


def _task_view(task) -> TeamTaskView:
    schedule = compute_team_task_schedule(
        status=task.status,
        priority=task.priority,
        due_at=task.due_at,
        schedule_version=task.schedule_version,
        now=datetime.now(UTC),
    )
    return TeamTaskView(
        task_id=task.task_id,
        project_id=task.project_id,
        source_team_id=task.source_team_id,
        target_team_id=task.target_team_id,
        created_by=task.created_by,
        title=task.title,
        description=task.description,
        acceptance_criteria=task.acceptance_criteria,
        status=task.status,
        assigned_account_id=task.assigned_account_id,
        artifact_resource_ids=task.artifact_resource_ids,
        review_note=task.review_note,
        created_at=task.created_at,
        updated_at=task.updated_at,
        priority=task.priority,
        due_at=task.due_at,
        schedule_version=task.schedule_version,
        due_changed_at=task.due_changed_at,
        due_changed_by=task.due_changed_by,
        completed_at=task.completed_at,
        is_overdue=schedule.is_overdue,
        is_due_soon=schedule.is_due_soon,
        due_in_seconds=schedule.due_in_seconds,
    )


def _artifact_kind(filename: str | None, media_type: str) -> ArtifactKind:
    suffix = (filename or "").lower().rsplit(".", 1)[-1]
    if media_type == "application/pdf" or suffix == "pdf":
        return ArtifactKind.PDF
    if suffix in {"xlsx", "xls", "csv", "ods"}:
        return ArtifactKind.SPREADSHEET
    if suffix in {"pptx", "ppt", "odp"}:
        return ArtifactKind.PRESENTATION
    if suffix in {"py", "js", "ts", "tsx", "java", "go", "rs", "c", "cpp", "h"}:
        return ArtifactKind.SOURCE_CODE
    if media_type.startswith("text/") or suffix in {
        "doc",
        "docx",
        "md",
        "txt",
        "odt",
        "json",
        "yaml",
        "yml",
    }:
        return ArtifactKind.DOCUMENT
    return ArtifactKind.GENERIC
