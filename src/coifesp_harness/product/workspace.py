"""Persistent per-user project conversations and team project agents.

A project workspace is a continuous ChatGPT-style conversation:

- ``TeamProjectAgent``: one logical Agent per participating team. It holds the
  team's role inside the project and is the only agentic boundary allowed to
  read team-private context.
- ``ProjectConversation``: one conversation per (project, account). It is
  created idempotently on first open and never duplicated by the user.
- ``ProjectAgentTurn``: a short-lived unit of work triggered by a user
  message. Long-lived state lives on the conversation; budget, retries and
  tool calls live on each Durable Run.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..errors import GovernanceConflictError, ResourceNotFound
from .models import (
    ConversationMessageKind,
    ConversationStatus,
    DataPropagation,
    Project,
    ProjectAgentTurn,
    ProjectConversation,
    ProjectConversationMessage,
    ProjectTeam,
    ProjectTeamKind,
    ProjectWorkspaceSnapshot,
    ResourceAction,
    TeamProjectAgent,
    TeamProjectAgentStatus,
    TurnStatus,
    TurnTriggerKind,
)
from .repository import (
    ACCOUNTS,
    COLLABORATION_ACTION_DRAFTS,
    PROJECT_ACTIVITIES,
    PROJECT_ACTIVITY_CURSORS,
    PROJECT_AGENT_RUNS,
    PROJECT_AGENT_TURNS,
    PROJECT_CONVERSATION_MESSAGES,
    PROJECT_CONVERSATIONS,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    PROJECTS,
    TEAM_AGENT_PROFILES,
    TEAM_PROJECT_AGENTS,
    TEAM_TASKS,
)
from .service import ProductAccountService, ProjectDataPolicy

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MESSAGE_CONTENT_MAX = 50_000


class ProjectWorkspaceService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._policy = ProjectDataPolicy(engine)

    # ------------------------------------------------------------------
    # Team project agents
    # ------------------------------------------------------------------
    def ensure_team_project_agent(
        self, *, project_id: str, team_id: str
    ) -> TeamProjectAgent:
        now = datetime.now(UTC)
        profile = self.ensure_team_agent_profile(team_id=team_id)
        try:
            with self.engine.begin() as connection:
                agent_id = f"agent-{secrets.token_hex(12)}"
                connection.execute(
                    TEAM_PROJECT_AGENTS.insert().values(
                        agent_id=agent_id,
                        project_id=project_id,
                        team_id=team_id,
                        status=TeamProjectAgentStatus.ACTIVE.value,
                        memory_version=1,
                        profile_id=profile.profile_id,
                        profile_version=profile.version,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return self._team_agent_row(
                    connection, project_id=project_id, team_id=team_id
                )
        except IntegrityError:
            return self.get_team_project_agent(project_id=project_id, team_id=team_id)

    def ensure_team_agent_profile(self, *, team_id: str):
        from .models import TeamAgentProfile

        now = datetime.now(UTC)
        values = {
            "profile_id": team_id,
            "version": 1,
            "team_id": team_id,
            "display_name": f"{team_id} Agent",
            "tool_policy_id": "default",
            "skill_policy_id": "default",
            "model_policy_id": "default",
            "memory_policy_id": "default",
            "autonomy_level": "bounded",
            "max_run_budget_profile": {
                "max_turns": 20,
                "max_tool_calls": 50,
                "max_total_tokens": 100000,
                "max_model_cost_microusd": 10000000,
            },
            "created_at": now,
        }
        try:
            with self.engine.begin() as connection:
                connection.execute(TEAM_AGENT_PROFILES.insert().values(**values))
        except IntegrityError:
            pass
        with self.engine.connect() as connection:
            row = connection.execute(
                select(TEAM_AGENT_PROFILES).where(
                    and_(
                        TEAM_AGENT_PROFILES.c.profile_id == team_id,
                        TEAM_AGENT_PROFILES.c.version == 1,
                    )
                )
            ).mappings().one()
        return TeamAgentProfile(
            row["profile_id"],
            row["team_id"],
            row["version"],
            row["display_name"],
            row["tool_policy_id"],
            row["skill_policy_id"],
            row["model_policy_id"],
            row["memory_policy_id"],
            row["autonomy_level"],
            dict(row["max_run_budget_profile"]),
            ProductAccountService._aware(row["created_at"]),
        )

    def get_team_project_agent(
        self, *, project_id: str, team_id: str
    ) -> TeamProjectAgent:
        with self.engine.connect() as connection:
            return self._team_agent_row(
                connection, project_id=project_id, team_id=team_id
            )

    def list_team_project_agents(
        self, *, project_id: str
    ) -> tuple[TeamProjectAgent, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(TEAM_PROJECT_AGENTS)
                    .where(
                        and_(
                            TEAM_PROJECT_AGENTS.c.project_id == project_id,
                            TEAM_PROJECT_AGENTS.c.status
                            == TeamProjectAgentStatus.ACTIVE.value,
                        )
                    )
                    .order_by(TEAM_PROJECT_AGENTS.c.created_at)
                )
                .mappings()
                .all()
            )
        return tuple(self._team_agent(row) for row in rows)

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    def ensure_conversation(
        self, *, project_id: str, actor_id: str
    ) -> ProjectConversation:
        self._require_participant(project_id=project_id, actor_id=actor_id)
        existing = self._find_conversation(project_id=project_id, actor_id=actor_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        with self.engine.connect() as connection:
            team_id = ProductAccountService._account_row(connection, actor_id)["team_id"]
        agent = self.ensure_team_project_agent(project_id=project_id, team_id=team_id)
        try:
            with self.engine.begin() as connection:
                conversation_id = f"conv-{secrets.token_hex(12)}"
                connection.execute(
                    PROJECT_CONVERSATIONS.insert().values(
                        conversation_id=conversation_id,
                        project_id=project_id,
                        team_agent_id=agent.agent_id,
                        account_id=actor_id,
                        status=ConversationStatus.ACTIVE.value,
                        last_message_sequence=0,
                        created_at=now,
                        updated_at=now,
                        archived_at=None,
                    )
                )
                return self._conversation_row(
                    connection, conversation_id=conversation_id
                )
        except IntegrityError:
            # Concurrent first-open: the unique (project_id, account_id)
            # constraint guarantees every caller converges on one row.
            existing = self._find_conversation(project_id=project_id, actor_id=actor_id)
            if existing is None:
                raise
            return existing

    def get_conversation(self, *, project_id: str, actor_id: str) -> ProjectConversation:
        self._require_participant(project_id=project_id, actor_id=actor_id)
        conversation = self._find_conversation(project_id=project_id, actor_id=actor_id)
        if conversation is None:
            raise ResourceNotFound("conversation is unavailable")
        return conversation

    def archive_conversation(self, *, project_id: str, actor_id: str) -> ProjectConversation:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            conversation = self._conversation_for_account(
                connection, project_id=project_id, actor_id=actor_id
            )
            if conversation.status is not ConversationStatus.ACTIVE:
                raise GovernanceConflictError("conversation is already archived")
            connection.execute(
                PROJECT_CONVERSATIONS.update()
                .where(PROJECT_CONVERSATIONS.c.conversation_id == conversation.conversation_id)
                .values(status=ConversationStatus.ARCHIVED.value, archived_at=now, updated_at=now)
            )
        return self.get_conversation(project_id=project_id, actor_id=actor_id)

    def reactivate_conversation(self, *, project_id: str, actor_id: str) -> ProjectConversation:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            conversation = self._conversation_for_account(
                connection, project_id=project_id, actor_id=actor_id
            )
            if conversation.status is not ConversationStatus.ARCHIVED:
                raise GovernanceConflictError("conversation is not archived")
            connection.execute(
                PROJECT_CONVERSATIONS.update()
                .where(PROJECT_CONVERSATIONS.c.conversation_id == conversation.conversation_id)
                .values(status=ConversationStatus.ACTIVE.value, archived_at=None, updated_at=now)
            )
        return self.get_conversation(project_id=project_id, actor_id=actor_id)

    # ------------------------------------------------------------------
    # Messages and turns
    # ------------------------------------------------------------------
    def list_messages(
        self,
        *,
        conversation_id: str,
        actor_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[ProjectConversationMessage, ...]:
        with self.engine.connect() as connection:
            self._conversation_row(
                connection,
                conversation_id=conversation_id,
                actor_id=actor_id,
            )
            rows = (
                connection.execute(
                    select(PROJECT_CONVERSATION_MESSAGES)
                    .where(
                        and_(
                            PROJECT_CONVERSATION_MESSAGES.c.conversation_id == conversation_id,
                            PROJECT_CONVERSATION_MESSAGES.c.sequence > after_sequence,
                        )
                    )
                    .order_by(PROJECT_CONVERSATION_MESSAGES.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return tuple(self._message(row) for row in rows)

    def append_user_message(
        self,
        *,
        conversation_id: str,
        actor_id: str,
        content: str,
        idempotency_key: str,
        expected_last_sequence: int | None = None,
        attachment_resource_ids: tuple[str, ...] = (),
        trigger_kind: TurnTriggerKind = TurnTriggerKind.USER_MESSAGE,
        message_kind: ConversationMessageKind = ConversationMessageKind.TEXT,
    ) -> tuple[ProjectConversationMessage, ProjectAgentTurn]:
        if not content.strip() and not attachment_resource_ids:
            raise ValueError("message content or at least one attachment is required")
        if len(content.encode("utf-8")) > _MESSAGE_CONTENT_MAX:
            raise ValueError("message content is too long")
        if not _ID.fullmatch(idempotency_key):
            raise ValueError("idempotency key is invalid")
        now = datetime.now(UTC)
        turn_id = f"turn-{secrets.token_hex(12)}"
        try:
            with self.engine.begin() as connection:
                conversation = self._conversation_row(
                    connection,
                    conversation_id=conversation_id,
                    actor_id=actor_id,
                )
                if conversation.status is not ConversationStatus.ACTIVE:
                    raise GovernanceConflictError("conversation is archived")
                locked = self._conversation_row_for_update(
                    connection, conversation_id=conversation_id
                )
                if (
                    expected_last_sequence is not None
                    and locked.last_message_sequence != expected_last_sequence
                ):
                    raise GovernanceConflictError(
                        "conversation has new messages; reload before sending"
                    )
                active_turn = (
                    connection.execute(
                        select(PROJECT_AGENT_TURNS.c.turn_id).where(
                            and_(
                                PROJECT_AGENT_TURNS.c.conversation_id == conversation_id,
                                PROJECT_AGENT_TURNS.c.status == TurnStatus.ACTIVE.value,
                            )
                        )
                    ).scalar_one_or_none()
                )
                if active_turn is not None:
                    raise GovernanceConflictError(
                        "an Agent turn is already running for this conversation"
                    )
                for resource_id in attachment_resource_ids:
                    access = self._policy.decide(
                        account_id=actor_id,
                        resource_id=resource_id,
                        action=ResourceAction.AGENT_USE,
                        project_id=conversation.project_id,
                    )
                    if not access.allowed:
                        raise GovernanceConflictError(
                            f"attachment resource is not usable by this team: {resource_id}"
                        )
                sequence = locked.last_message_sequence + 1
                # The turn_id is generated first and written on the user
                # message so message and turn share a bidirectional link in
                # the same transaction.
                connection.execute(
                    PROJECT_CONVERSATION_MESSAGES.insert().values(
                        conversation_id=conversation_id,
                        sequence=sequence,
                        role="user",
                        content=content.strip(),
                        turn_id=turn_id,
                        run_id=None,
                        message_kind=message_kind.value,
                        attachment_resource_ids=json.dumps(
                            list(attachment_resource_ids), ensure_ascii=False
                        ),
                        created_at=now,
                    )
                )
                connection.execute(
                    PROJECT_CONVERSATIONS.update()
                    .where(PROJECT_CONVERSATIONS.c.conversation_id == conversation_id)
                    .values(last_message_sequence=sequence, updated_at=now)
                )
                connection.execute(
                    PROJECT_AGENT_TURNS.insert().values(
                        turn_id=turn_id,
                        conversation_id=conversation_id,
                        user_message_sequence=sequence,
                        assistant_message_sequence=None,
                        run_id=None,
                        trigger_kind=trigger_kind.value,
                        status=TurnStatus.ACTIVE.value,
                        idempotency_key=idempotency_key,
                        created_at=now,
                        completed_at=None,
                    )
                )
                message = self._message_row(
                    connection,
                    conversation_id=conversation_id,
                    sequence=sequence,
                )
                turn = self._turn_row(connection, turn_id=turn_id)
                return message, turn
        except IntegrityError:
            raise GovernanceConflictError(
                "message idempotency key was already used for this conversation"
            )

    def get_turn(self, *, conversation_id: str, turn_id: str) -> ProjectAgentTurn:
        with self.engine.connect() as connection:
            turn = self._turn_row(connection, turn_id=turn_id)
            if turn.conversation_id != conversation_id:
                raise ResourceNotFound("turn is unavailable")
            return turn

    def get_conversation_by_id(self, *, conversation_id: str) -> ProjectConversation:
        with self.engine.connect() as connection:
            return self._conversation_row(connection, conversation_id=conversation_id)

    def conversation_for_team(
        self, *, project_id: str, team_id: str
    ) -> ProjectConversation | None:
        """The most recently updated active conversation in a team.

        Used to host recipient-side drafting turns on the team's own project
        conversation without inventing a new account.
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(PROJECT_CONVERSATIONS)
                    .join(
                        ACCOUNTS,
                        ACCOUNTS.c.account_id == PROJECT_CONVERSATIONS.c.account_id,
                    )
                    .where(
                        and_(
                            PROJECT_CONVERSATIONS.c.project_id == project_id,
                            ACCOUNTS.c.team_id == team_id,
                            PROJECT_CONVERSATIONS.c.status == ConversationStatus.ACTIVE.value,
                        )
                    )
                    .order_by(PROJECT_CONVERSATIONS.c.updated_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return self._conversation(row) if row is not None else None

    def conversation_messages_for_context(
        self,
        *,
        conversation_id: str,
        limit: int = 20,
    ) -> tuple[ProjectConversationMessage, ...]:
        """Recent user/assistant history for multi-turn context assembly.

        The window is aligned to a complete turn boundary: when the most
        recent slice would start mid-turn with an orphaned assistant message,
        one older message is pulled in so the slice begins with the user
        message that started that turn.
        """
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(PROJECT_CONVERSATION_MESSAGES)
                    .where(PROJECT_CONVERSATION_MESSAGES.c.conversation_id == conversation_id)
                    .order_by(PROJECT_CONVERSATION_MESSAGES.c.sequence.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        messages = [self._message(row) for row in reversed(rows)]
        if messages and messages[0].role == "assistant":
            older = self._messages_before(
                conversation_id=conversation_id,
                before_sequence=messages[0].sequence,
                limit=1,
            )
            messages = list(older) + messages
        return tuple(messages)

    def _messages_before(
        self, *, conversation_id: str, before_sequence: int, limit: int
    ) -> tuple[ProjectConversationMessage, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(PROJECT_CONVERSATION_MESSAGES)
                    .where(
                        and_(
                            PROJECT_CONVERSATION_MESSAGES.c.conversation_id == conversation_id,
                            PROJECT_CONVERSATION_MESSAGES.c.sequence < before_sequence,
                        )
                    )
                    .order_by(PROJECT_CONVERSATION_MESSAGES.c.sequence.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return tuple(self._message(row) for row in reversed(rows))

    def active_turn(self, *, conversation_id: str) -> ProjectAgentTurn | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(PROJECT_AGENT_TURNS)
                    .where(
                        and_(
                            PROJECT_AGENT_TURNS.c.conversation_id == conversation_id,
                            PROJECT_AGENT_TURNS.c.status == TurnStatus.ACTIVE.value,
                        )
                    )
                    .order_by(PROJECT_AGENT_TURNS.c.created_at)
                )
                .mappings()
                .one_or_none()
            )
        return self._turn(row) if row is not None else None

    def complete_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        assistant_content: str,
        run_id: str | None = None,
    ) -> ProjectConversationMessage:
        if not assistant_content.strip():
            raise ValueError("assistant content is required")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            conversation = self._conversation_row(
                connection, conversation_id=conversation_id
            )
            turn = self._turn_row(connection, turn_id=turn_id)
            if turn.conversation_id != conversation_id:
                raise ResourceNotFound("turn is unavailable")
            if turn.status is not TurnStatus.ACTIVE:
                # Idempotent projection: a completed turn returns the message
                # it already produced instead of appending a duplicate.
                if turn.status is TurnStatus.COMPLETED and turn.assistant_message_sequence is not None:
                    return self._message_row(
                        connection,
                        conversation_id=conversation_id,
                        sequence=turn.assistant_message_sequence,
                    )
                raise GovernanceConflictError("turn is no longer active")
            locked = self._conversation_row_for_update(
                connection, conversation_id=conversation_id
            )
            sequence = locked.last_message_sequence + 1
            connection.execute(
                PROJECT_CONVERSATION_MESSAGES.insert().values(
                    conversation_id=conversation_id,
                    sequence=sequence,
                    role="assistant",
                    content=assistant_content.strip(),
                    turn_id=turn_id,
                    run_id=run_id,
                    message_kind=ConversationMessageKind.TEXT.value,
                    attachment_resource_ids="[]",
                    created_at=now,
                )
            )
            connection.execute(
                PROJECT_CONVERSATIONS.update()
                .where(PROJECT_CONVERSATIONS.c.conversation_id == conversation_id)
                .values(last_message_sequence=sequence, updated_at=now)
            )
            connection.execute(
                PROJECT_AGENT_TURNS.update()
                .where(PROJECT_AGENT_TURNS.c.turn_id == turn_id)
                .values(
                    assistant_message_sequence=sequence,
                    run_id=run_id,
                    status=TurnStatus.COMPLETED.value,
                    completed_at=now,
                )
            )
            return self._message_row(
                connection, conversation_id=conversation_id, sequence=sequence
            )

    def fail_turn(self, *, conversation_id: str, turn_id: str) -> ProjectAgentTurn:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            turn = self._turn_row(connection, turn_id=turn_id)
            if turn.conversation_id != conversation_id:
                raise ResourceNotFound("turn is unavailable")
            if turn.status is TurnStatus.FAILED:
                return turn
            if turn.status is not TurnStatus.ACTIVE:
                raise GovernanceConflictError("turn is no longer active")
            connection.execute(
                PROJECT_AGENT_TURNS.update()
                .where(PROJECT_AGENT_TURNS.c.turn_id == turn_id)
                .values(status=TurnStatus.FAILED.value, completed_at=now)
            )
            return self._turn_row(connection, turn_id=turn_id)

    def cancel_turn(self, *, conversation_id: str, turn_id: str) -> ProjectAgentTurn:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            turn = self._turn_row(connection, turn_id=turn_id)
            if turn.conversation_id != conversation_id:
                raise ResourceNotFound("turn is unavailable")
            if turn.status is TurnStatus.CANCELLED:
                return turn
            if turn.status is not TurnStatus.ACTIVE:
                raise GovernanceConflictError("turn is no longer active")
            connection.execute(
                PROJECT_AGENT_TURNS.update()
                .where(PROJECT_AGENT_TURNS.c.turn_id == turn_id)
                .values(status=TurnStatus.CANCELLED.value, completed_at=now)
            )
            return self._turn_row(connection, turn_id=turn_id)

    def bind_turn_run(
        self, *, conversation_id: str, turn_id: str, run_id: str
    ) -> ProjectAgentTurn:
        with self.engine.begin() as connection:
            turn = self._turn_row(connection, turn_id=turn_id)
            if turn.conversation_id != conversation_id:
                raise ResourceNotFound("turn is unavailable")
            connection.execute(
                PROJECT_AGENT_TURNS.update()
                .where(PROJECT_AGENT_TURNS.c.turn_id == turn_id)
                .values(run_id=run_id)
            )
            connection.execute(
                PROJECT_AGENT_RUNS.update()
                .where(PROJECT_AGENT_RUNS.c.run_id == run_id)
                .values(conversation_id=conversation_id, turn_id=turn_id)
            )
            return self._turn_row(connection, turn_id=turn_id)

    # ------------------------------------------------------------------
    # Workspace aggregation
    # ------------------------------------------------------------------
    def workspace(self, *, project_id: str, actor_id: str) -> ProjectWorkspaceSnapshot:
        self._require_participant(project_id=project_id, actor_id=actor_id)
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            project = (
                connection.execute(
                    select(PROJECTS).where(PROJECTS.c.project_id == project_id)
                )
                .mappings()
                .one()
            )
            teams = (
                connection.execute(
                    select(PROJECT_TEAMS)
                    .where(PROJECT_TEAMS.c.project_id == project_id)
                    .order_by(PROJECT_TEAMS.c.name)
                )
                .mappings()
                .all()
            )
            task_count = connection.execute(
                select(func.count()).select_from(TEAM_TASKS).where(
                    TEAM_TASKS.c.project_id == project_id
                )
            ).scalar_one()
            resource_count = connection.execute(
                select(func.count()).select_from(PROJECT_RESOURCES).where(
                    PROJECT_RESOURCES.c.project_id == project_id
                )
            ).scalar_one()
            pending_draft_count = connection.execute(
                select(func.count()).select_from(COLLABORATION_ACTION_DRAFTS).where(
                    and_(
                        COLLABORATION_ACTION_DRAFTS.c.project_id == project_id,
                        COLLABORATION_ACTION_DRAFTS.c.team_id == actor["team_id"],
                        COLLABORATION_ACTION_DRAFTS.c.status == "pending",
                    )
                )
            ).scalar_one()
            unread_activity_count = self._unread_activity_count(
                connection, project_id=project_id, account_id=actor_id
            )
        return ProjectWorkspaceSnapshot(
            project=Project(
                project["project_id"],
                project["name"],
                project["description"],
                project["owner_team_id"],
                project["created_by"],
                ProductAccountService._aware(project["created_at"]),
            ),
            teams=tuple(
                ProjectTeam(
                    row["team_id"],
                    row["project_id"],
                    row["name"],
                    ProjectTeamKind(row["kind"]),
                    row["assigned_by"],
                )
                for row in teams
            ),
            conversation=self._find_conversation(project_id=project_id, actor_id=actor_id),
            task_count=task_count,
            resource_count=resource_count,
            pending_draft_count=pending_draft_count,
            unread_activity_count=unread_activity_count,
        )

    def update_resource_propagation(
        self,
        *,
        project_id: str,
        resource_id: str,
        actor_id: str,
        requested_propagation: DataPropagation,
        expected_propagation: DataPropagation,
    ) -> None:
        """Change resource visibility with an optimistic-lock on the current value."""
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            row = (
                connection.execute(
                    select(
                        PROJECT_RESOURCES.c.propagation,
                        PROJECT_RESOURCES.c.owner_team_id,
                    ).where(
                        and_(
                            PROJECT_RESOURCES.c.project_id == project_id,
                            PROJECT_RESOURCES.c.resource_id == resource_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResourceNotFound("project resource is unavailable")
            if row["owner_team_id"] != actor["team_id"]:
                raise ResourceNotFound("project resource is unavailable")
            if row["propagation"] != expected_propagation.value:
                raise GovernanceConflictError("resource propagation has changed; reload and retry")
            if requested_propagation in (
                DataPropagation.TEAM_PRIVATE,
                DataPropagation.PROJECT_READONLY,
            ):
                connection.execute(
                    PROJECT_RESOURCES.update()
                    .where(
                        and_(
                            PROJECT_RESOURCES.c.project_id == project_id,
                            PROJECT_RESOURCES.c.resource_id == resource_id,
                        )
                    )
                    .values(propagation=requested_propagation.value)
                )
                return
            raise ValueError("unsupported propagation target")

    def list_project_resources(
        self,
        *,
        project_id: str,
        actor_id: str,
        scope: str | None = None,
    ) -> tuple:
        """List resources visible to the actor, optionally filtered by scope."""
        self._require_participant(project_id=project_id, actor_id=actor_id)
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            query = select(PROJECT_RESOURCES).where(PROJECT_RESOURCES.c.project_id == project_id)
            if scope == "team_private":
                query = query.where(
                    and_(
                        PROJECT_RESOURCES.c.propagation == DataPropagation.TEAM_PRIVATE.value,
                        PROJECT_RESOURCES.c.owner_team_id == actor["team_id"],
                    )
                )
            elif scope == "project_shared":
                query = query.where(
                    PROJECT_RESOURCES.c.propagation != DataPropagation.TEAM_PRIVATE.value
                )
            rows = connection.execute(query.order_by(PROJECT_RESOURCES.c.created_at.desc())).mappings().all()
        return tuple(
            {
                "resource_id": row["resource_id"],
                "project_id": row["project_id"],
                "owner_team_id": row["owner_team_id"],
                "title": row["title"],
                "media_type": row["media_type"],
                "propagation": row["propagation"],
                "created_at": ProductAccountService._aware(row["created_at"]).isoformat(),
            }
            for row in rows
        )

    def list_workspace_projects(self, actor_id: str) -> tuple[Project, ...]:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            rows = (
                connection.execute(
                    select(PROJECTS)
                    .join(
                        PROJECT_TEAMS,
                        PROJECT_TEAMS.c.project_id == PROJECTS.c.project_id,
                    )
                    .where(PROJECT_TEAMS.c.team_id == actor["team_id"])
                    .order_by(PROJECTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        seen: dict[str, Project] = {}
        for row in rows:
            project = Project(
                row["project_id"],
                row["name"],
                row["description"],
                row["owner_team_id"],
                row["created_by"],
                ProductAccountService._aware(row["created_at"]),
            )
            seen.setdefault(project.project_id, project)
        return tuple(seen.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _require_participant(self, *, project_id: str, actor_id: str) -> None:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            visible = connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        PROJECT_TEAMS.c.team_id == actor["team_id"],
                    )
                )
            ).scalar_one_or_none()
        if visible is None:
            raise ResourceNotFound("project is unavailable")

    def _find_conversation(
        self, *, project_id: str, actor_id: str
    ) -> ProjectConversation | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(PROJECT_CONVERSATIONS).where(
                        and_(
                            PROJECT_CONVERSATIONS.c.project_id == project_id,
                            PROJECT_CONVERSATIONS.c.account_id == actor_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._conversation(row) if row is not None else None

    @staticmethod
    def _conversation_for_account(
        connection, *, project_id: str, actor_id: str
    ) -> ProjectConversation:
        row = (
            connection.execute(
                select(PROJECT_CONVERSATIONS).where(
                    and_(
                        PROJECT_CONVERSATIONS.c.project_id == project_id,
                        PROJECT_CONVERSATIONS.c.account_id == actor_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("conversation is unavailable")
        return ProjectWorkspaceService._conversation(row)

    @staticmethod
    def _conversation_row(connection, *, conversation_id: str, actor_id: str | None = None):
        query = select(PROJECT_CONVERSATIONS).where(
            PROJECT_CONVERSATIONS.c.conversation_id == conversation_id
        )
        if actor_id is not None:
            query = query.where(PROJECT_CONVERSATIONS.c.account_id == actor_id)
        row = connection.execute(query).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("conversation is unavailable")
        return ProjectWorkspaceService._conversation(row)

    @staticmethod
    def _conversation_row_for_update(connection, *, conversation_id: str):
        row = (
            connection.execute(
                select(PROJECT_CONVERSATIONS)
                .where(PROJECT_CONVERSATIONS.c.conversation_id == conversation_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("conversation is unavailable")
        return ProjectWorkspaceService._conversation(row)

    @staticmethod
    def _conversation(row) -> ProjectConversation:
        return ProjectConversation(
            row["conversation_id"],
            row["project_id"],
            row["team_agent_id"],
            row["account_id"],
            ConversationStatus(row["status"]),
            int(row["last_message_sequence"]),
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["updated_at"]),
            ProductAccountService._aware(row["archived_at"]) if row["archived_at"] else None,
        )

    @staticmethod
    def _message_row(connection, *, conversation_id: str, sequence: int):
        row = (
            connection.execute(
                select(PROJECT_CONVERSATION_MESSAGES).where(
                    and_(
                        PROJECT_CONVERSATION_MESSAGES.c.conversation_id == conversation_id,
                        PROJECT_CONVERSATION_MESSAGES.c.sequence == sequence,
                    )
                )
            )
            .mappings()
            .one()
        )
        return ProjectWorkspaceService._message(row)

    @staticmethod
    def _message(row) -> ProjectConversationMessage:
        return ProjectConversationMessage(
            row["conversation_id"],
            int(row["sequence"]),
            row["role"],
            row["content"],
            row["turn_id"],
            row["run_id"],
            ConversationMessageKind(row["message_kind"]),
            ProductAccountService._aware(row["created_at"]),
            tuple(json.loads(row["attachment_resource_ids"] or "[]")),
        )

    @staticmethod
    def _turn_row(connection, *, turn_id: str):
        row = (
            connection.execute(
                select(PROJECT_AGENT_TURNS).where(PROJECT_AGENT_TURNS.c.turn_id == turn_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("turn is unavailable")
        return ProjectWorkspaceService._turn(row)

    @staticmethod
    def _turn(row) -> ProjectAgentTurn:
        return ProjectAgentTurn(
            row["turn_id"],
            row["conversation_id"],
            int(row["user_message_sequence"]),
            int(row["assistant_message_sequence"]) if row["assistant_message_sequence"] is not None else None,
            row["run_id"],
            TurnTriggerKind(row["trigger_kind"]),
            TurnStatus(row["status"]),
            row["idempotency_key"],
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["completed_at"]) if row["completed_at"] else None,
        )

    @staticmethod
    def _team_agent_row(connection, *, project_id: str, team_id: str):
        row = (
            connection.execute(
                select(TEAM_PROJECT_AGENTS).where(
                    and_(
                        TEAM_PROJECT_AGENTS.c.project_id == project_id,
                        TEAM_PROJECT_AGENTS.c.team_id == team_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("team project agent is unavailable")
        return ProjectWorkspaceService._team_agent(row)

    @staticmethod
    def _team_agent(row) -> TeamProjectAgent:
        return TeamProjectAgent(
            row["agent_id"],
            row["project_id"],
            row["team_id"],
            TeamProjectAgentStatus(row["status"]),
            int(row["memory_version"]),
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["updated_at"]),
            row["profile_id"],
            int(row["profile_version"]),
        )

    @staticmethod
    def _unread_activity_count(connection, *, project_id: str, account_id: str) -> int:
        cursor = (
            connection.execute(
                select(PROJECT_ACTIVITY_CURSORS.c.last_read_sequence).where(
                    and_(
                        PROJECT_ACTIVITY_CURSORS.c.account_id == account_id,
                        PROJECT_ACTIVITY_CURSORS.c.project_id == project_id,
                    )
                )
            ).scalar_one_or_none()
        )
        last_read = int(cursor) if cursor is not None else 0
        return int(
            connection.execute(
                select(func.count()).select_from(PROJECT_ACTIVITIES).where(
                    and_(
                        PROJECT_ACTIVITIES.c.project_id == project_id,
                        PROJECT_ACTIVITIES.c.sequence > last_read,
                    )
                )
            ).scalar_one()
        )
