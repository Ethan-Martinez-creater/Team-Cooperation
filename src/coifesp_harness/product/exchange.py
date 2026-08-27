"""Team Agent exchanges: draft -> human confirmation -> immutable shared package.

Two-phase boundary enforcement:

- Drafting may use any context the source team owns, including team-private
  resources, because nothing leaves the team until approval.
- Approval re-validates every shared resource server-side: a shared context
  package can never reference a ``team_private`` resource, and every
  recipient gets an independently computed context snapshot so it only sees
  project-shared material plus its own team-private resources.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..errors import GovernanceConflictError, ResourceNotFound
from .models import (
    AgentExchange,
    AgentExchangeDraft,
    AgentExchangeRecipient,
    AgentExchangeResponse,
    DataPropagation,
    ExchangeDraftStatus,
    ExchangeRecipientStatus,
    ExchangeStatus,
)
from .repository import (
    AGENT_EXCHANGE_DRAFTS,
    AGENT_EXCHANGE_RECIPIENTS,
    AGENT_EXCHANGE_RESPONSES,
    AGENT_EXCHANGES,
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
)
from .service import ProductAccountService

_MESSAGE_LIMIT = 50_000


def _exchange_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentExchangeService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------
    def create_draft(
        self,
        *,
        draft_id: str,
        project_id: str,
        actor_id: str,
        purpose: str,
        summary: str,
        request: str,
        constraints: str = "",
        shared_resource_ids: tuple[str, ...] = (),
        recipient_team_ids: tuple[str, ...] = (),
        source_conversation_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> AgentExchangeDraft:
        self._require_participant(project_id=project_id, actor_id=actor_id)
        if not purpose.strip() or not summary.strip() or not request.strip():
            raise ValueError("purpose, summary and request are required")
        if not recipient_team_ids:
            raise ValueError("at least one recipient team is required")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            team_id = ProductAccountService._account_row(connection, actor_id)["team_id"]
            self._validate_recipients(
                connection, project_id=project_id, source_team_id=team_id,
                recipient_team_ids=recipient_team_ids,
            )
            payload = self._payload(
                purpose=purpose, summary=summary, request=request, constraints=constraints,
                shared_resource_ids=shared_resource_ids,
                recipient_team_ids=recipient_team_ids,
            )
            connection.execute(
                AGENT_EXCHANGE_DRAFTS.insert().values(
                    draft_id=draft_id,
                    project_id=project_id,
                    source_team_id=team_id,
                    source_conversation_id=source_conversation_id,
                    source_turn_id=source_turn_id,
                    purpose=purpose.strip(),
                    summary=summary.strip(),
                    request=request.strip(),
                    constraints=constraints.strip(),
                    shared_resource_ids=json.dumps(list(shared_resource_ids), ensure_ascii=False),
                    recipient_team_ids=json.dumps(list(recipient_team_ids), ensure_ascii=False),
                    content_sha256=_exchange_sha256(payload),
                    status=ExchangeDraftStatus.DRAFTING.value,
                    version=1,
                    created_by=actor_id,
                    created_at=now,
                    updated_at=now,
                    approved_at=None,
                    rejection_reason="",
                )
            )
            return self._draft_row(connection, draft_id=draft_id)

    def update_draft(
        self,
        *,
        project_id: str,
        draft_id: str,
        actor_id: str,
        expected_version: int,
        purpose: str,
        summary: str,
        request: str,
        constraints: str = "",
        shared_resource_ids: tuple[str, ...] = (),
        recipient_team_ids: tuple[str, ...] = (),
    ) -> AgentExchangeDraft:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            draft = self._draft_row(connection, draft_id=draft_id)
            if draft.project_id != project_id:
                raise ResourceNotFound("exchange draft is unavailable")
            actor = ProductAccountService._account_row(connection, actor_id)
            if draft.source_team_id != actor["team_id"]:
                raise ResourceNotFound("exchange draft is unavailable")
            if draft.status is not ExchangeDraftStatus.DRAFTING:
                raise GovernanceConflictError("exchange draft is no longer editable")
            if draft.version != expected_version:
                raise GovernanceConflictError("exchange draft version is stale")
            if not purpose.strip() or not summary.strip() or not request.strip():
                raise ValueError("purpose, summary and request are required")
            if not recipient_team_ids:
                raise ValueError("at least one recipient team is required")
            self._validate_recipients(
                connection, project_id=project_id, source_team_id=draft.source_team_id,
                recipient_team_ids=recipient_team_ids,
            )
            payload = self._payload(
                purpose=purpose, summary=summary, request=request, constraints=constraints,
                shared_resource_ids=shared_resource_ids,
                recipient_team_ids=recipient_team_ids,
            )
            connection.execute(
                AGENT_EXCHANGE_DRAFTS.update()
                .where(AGENT_EXCHANGE_DRAFTS.c.draft_id == draft_id)
                .values(
                    purpose=purpose.strip(),
                    summary=summary.strip(),
                    request=request.strip(),
                    constraints=constraints.strip(),
                    shared_resource_ids=json.dumps(list(shared_resource_ids), ensure_ascii=False),
                    recipient_team_ids=json.dumps(list(recipient_team_ids), ensure_ascii=False),
                    content_sha256=_exchange_sha256(payload),
                    version=draft.version + 1,
                    updated_at=now,
                )
            )
            return self._draft_row(connection, draft_id=draft_id)

    def reject_draft(
        self, *, project_id: str, draft_id: str, actor_id: str, reason: str
    ) -> AgentExchangeDraft:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            draft = self._draft_row(connection, draft_id=draft_id)
            if draft.project_id != project_id:
                raise ResourceNotFound("exchange draft is unavailable")
            actor = ProductAccountService._account_row(connection, actor_id)
            if draft.source_team_id != actor["team_id"]:
                raise ResourceNotFound("exchange draft is unavailable")
            if draft.status is not ExchangeDraftStatus.DRAFTING:
                raise GovernanceConflictError("exchange draft is no longer open")
            connection.execute(
                AGENT_EXCHANGE_DRAFTS.update()
                .where(AGENT_EXCHANGE_DRAFTS.c.draft_id == draft_id)
                .values(
                    status=ExchangeDraftStatus.REJECTED.value,
                    rejection_reason=(reason or "").strip(),
                    updated_at=now,
                )
            )
            return self._draft_row(connection, draft_id=draft_id)

    def list_drafts(self, *, project_id: str, actor_id: str) -> tuple[AgentExchangeDraft, ...]:
        self._require_participant(project_id=project_id, actor_id=actor_id)
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            rows = (
                connection.execute(
                    select(AGENT_EXCHANGE_DRAFTS)
                    .where(
                        and_(
                            AGENT_EXCHANGE_DRAFTS.c.project_id == project_id,
                            AGENT_EXCHANGE_DRAFTS.c.source_team_id == actor["team_id"],
                        )
                    )
                    .order_by(AGENT_EXCHANGE_DRAFTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return tuple(self._draft(row) for row in rows)

    # ------------------------------------------------------------------
    # Approval (publication boundary)
    # ------------------------------------------------------------------
    def approve_draft(
        self, *, project_id: str, draft_id: str, actor_id: str, expected_version: int
    ) -> AgentExchange:
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                draft = self._draft_row(connection, draft_id=draft_id)
                if draft.project_id != project_id:
                    raise ResourceNotFound("exchange draft is unavailable")
                actor = ProductAccountService._account_row(connection, actor_id)
                if draft.source_team_id != actor["team_id"]:
                    raise ResourceNotFound("exchange draft is unavailable")
                if draft.status is not ExchangeDraftStatus.DRAFTING:
                    raise GovernanceConflictError("exchange draft is no longer open")
                if draft.version != expected_version:
                    raise GovernanceConflictError("exchange draft version is stale")
                # Server-side deterministic leak boundary: no team-private
                # resource ID may ever travel inside a shared package.
                self._assert_no_private_resources(
                    connection, project_id=project_id,
                    resource_ids=draft.shared_resource_ids,
                )
                self._validate_recipients(
                    connection, project_id=project_id, source_team_id=draft.source_team_id,
                    recipient_team_ids=draft.recipient_team_ids,
                )
                exchange_id = f"exchange-{secrets.token_hex(12)}"
                connection.execute(
                    AGENT_EXCHANGES.insert().values(
                        exchange_id=exchange_id,
                        project_id=project_id,
                        source_team_id=draft.source_team_id,
                        source_conversation_id=draft.source_conversation_id,
                        source_turn_id=draft.source_turn_id,
                        purpose=draft.purpose,
                        summary=draft.summary,
                        request=draft.request,
                        constraints=draft.constraints,
                        content_sha256=draft.content_sha256,
                        status=ExchangeStatus.SENT.value,
                        approved_by=actor_id,
                        approved_at=now,
                        created_at=now,
                    )
                )
                for team_id in draft.recipient_team_ids:
                    snapshot = self._recipient_snapshot(
                        connection, project_id=project_id, team_id=team_id
                    )
                    connection.execute(
                        AGENT_EXCHANGE_RECIPIENTS.insert().values(
                            exchange_id=exchange_id,
                            recipient_team_id=team_id,
                            context_snapshot=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                            status=ExchangeRecipientStatus.PENDING.value,
                            response_id=None,
                            responded_at=None,
                            created_at=now,
                        )
                    )
                connection.execute(
                    AGENT_EXCHANGE_DRAFTS.update()
                    .where(AGENT_EXCHANGE_DRAFTS.c.draft_id == draft_id)
                    .values(
                        status=ExchangeDraftStatus.APPROVED.value,
                        approved_at=now,
                        updated_at=now,
                    )
                )
                return self._exchange_row(connection, exchange_id=exchange_id)
        except IntegrityError:
            raise GovernanceConflictError("exchange could not be created")

    def list_exchanges(self, *, project_id: str, actor_id: str) -> tuple[AgentExchange, ...]:
        self._require_participant(project_id=project_id, actor_id=actor_id)
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            team_id = actor["team_id"]
            rows = (
                connection.execute(
                    select(AGENT_EXCHANGES)
                    .where(
                        and_(
                            AGENT_EXCHANGES.c.project_id == project_id,
                            (
                                (AGENT_EXCHANGES.c.source_team_id == team_id)
                                | AGENT_EXCHANGES.c.exchange_id.in_(
                                    select(AGENT_EXCHANGE_RECIPIENTS.c.exchange_id).where(
                                        AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == team_id
                                    )
                                )
                            ),
                        )
                    )
                    .order_by(AGENT_EXCHANGES.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return tuple(self._exchange(row) for row in rows)

    def get_exchange(self, *, project_id: str, exchange_id: str, actor_id: str) -> AgentExchange:
        with self.engine.connect() as connection:
            exchange = self._exchange_row(connection, exchange_id=exchange_id)
            if exchange.project_id != project_id:
                raise ResourceNotFound("exchange is unavailable")
            actor = ProductAccountService._account_row(connection, actor_id)
            if not self._team_involved(
                connection, exchange, team_id=actor["team_id"]
            ):
                raise ResourceNotFound("exchange is unavailable")
        return exchange

    def get_recipient(
        self, *, exchange_id: str, recipient_team_id: str
    ) -> AgentExchangeRecipient:
        """A single recipient row, including reply draft state."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(AGENT_EXCHANGE_RECIPIENTS).where(
                        and_(
                            AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                            AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == recipient_team_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFound("exchange is not addressed to this team")
        return self._recipient(row)

    def list_recipients(
        self, *, project_id: str, exchange_id: str, actor_id: str
    ) -> tuple[AgentExchangeRecipient, ...]:
        with self.engine.connect() as connection:
            exchange = self._exchange_row(connection, exchange_id=exchange_id)
            if exchange.project_id != project_id:
                raise ResourceNotFound("exchange is unavailable")
            actor = ProductAccountService._account_row(connection, actor_id)
            team_id = actor["team_id"]
            if not self._team_involved(connection, exchange, team_id=team_id):
                raise ResourceNotFound("exchange is unavailable")
            query = select(AGENT_EXCHANGE_RECIPIENTS).where(
                AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id
            )
            if team_id != exchange.source_team_id:
                # A recipient team may only read its own snapshot, never the
                # private context snapshot of another recipient team.
                query = query.where(
                    AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == team_id
                )
            rows = (
                connection.execute(
                    query.order_by(AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id)
                )
                .mappings()
                .all()
            )
        return tuple(self._recipient(row) for row in rows)

    def list_responses(
        self, *, project_id: str, exchange_id: str, actor_id: str
    ) -> tuple[AgentExchangeResponse, ...]:
        with self.engine.connect() as connection:
            exchange = self._exchange_row(connection, exchange_id=exchange_id)
            if exchange.project_id != project_id:
                raise ResourceNotFound("exchange is unavailable")
            actor = ProductAccountService._account_row(connection, actor_id)
            team_id = actor["team_id"]
            if not self._team_involved(connection, exchange, team_id=team_id):
                raise ResourceNotFound("exchange is unavailable")
            query = select(AGENT_EXCHANGE_RESPONSES).where(
                AGENT_EXCHANGE_RESPONSES.c.exchange_id == exchange_id
            )
            if team_id != exchange.source_team_id:
                query = query.where(
                    AGENT_EXCHANGE_RESPONSES.c.recipient_team_id == team_id
                )
            rows = (
                connection.execute(
                    query.order_by(AGENT_EXCHANGE_RESPONSES.c.created_at)
                )
                .mappings()
                .all()
            )
        return tuple(self._response(row) for row in rows)

    # ------------------------------------------------------------------
    # Responses
    # ------------------------------------------------------------------
    def submit_response(
        self,
        *,
        project_id: str,
        exchange_id: str,
        actor_id: str,
        content: str,
        turn_id: str | None = None,
    ) -> AgentExchangeResponse:
        content = (content or "").strip()
        if len(content.encode("utf-8")) > _MESSAGE_LIMIT:
            raise ValueError("response content is too long")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            exchange = self._exchange_row(connection, exchange_id=exchange_id)
            if exchange.project_id != project_id:
                raise ResourceNotFound("exchange is unavailable")
            if exchange.status is ExchangeStatus.CLOSED:
                raise GovernanceConflictError("exchange is closed")
            actor = ProductAccountService._account_row(connection, actor_id)
            team_id = actor["team_id"]
            recipient = (
                connection.execute(
                    select(AGENT_EXCHANGE_RECIPIENTS).where(
                        and_(
                            AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                            AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == team_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if recipient is None:
                raise ResourceNotFound("exchange is not addressed to this team")
            if recipient["status"] == ExchangeRecipientStatus.RESPONDED.value:
                raise GovernanceConflictError("this team has already responded")
            if not content:
                # Confirming an Agent-drafted reply without retyping it.
                content = (recipient.get("draft_content") or "").strip()
            if not content:
                raise ValueError("response content is required")
            response_id = f"response-{secrets.token_hex(12)}"
            digest = _exchange_sha256({"content": content.strip()})
            connection.execute(
                AGENT_EXCHANGE_RESPONSES.insert().values(
                    response_id=response_id,
                    exchange_id=exchange_id,
                    recipient_team_id=team_id,
                    content=content.strip(),
                    content_sha256=digest,
                    approved_by=actor_id,
                    approved_at=now,
                    turn_id=turn_id,
                    created_at=now,
                )
            )
            connection.execute(
                AGENT_EXCHANGE_RECIPIENTS.update()
                .where(
                    and_(
                        AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                        AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == team_id,
                    )
                )
                .values(
                    status=ExchangeRecipientStatus.RESPONDED.value,
                    response_id=response_id,
                    responded_at=now,
                )
            )
            remaining = connection.execute(
                select(AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id).where(
                    and_(
                        AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                        AGENT_EXCHANGE_RECIPIENTS.c.status.in_(
                            [
                                ExchangeRecipientStatus.PENDING.value,
                                ExchangeRecipientStatus.DRAFTING.value,
                            ]
                        ),
                    )
                )
            ).scalar_one_or_none()
            if remaining is None:
                connection.execute(
                    AGENT_EXCHANGES.update()
                    .where(AGENT_EXCHANGES.c.exchange_id == exchange_id)
                    .values(status=ExchangeStatus.RESPONDED.value)
                )
            return self._response_row(connection, response_id=response_id)

    # ------------------------------------------------------------------
    # Recipient-side reply drafts: Agent drafts, human confirms
    # ------------------------------------------------------------------
    def record_response_draft_turn(
        self, *, exchange_id: str, recipient_team_id: str, turn_id: str
    ) -> None:
        """Bind a recipient team's drafting turn to its exchange recipient row.

        Terminal projection uses the stored turn id to route the assistant
        reply into ``draft_content``. Internal bookkeeping; no actor check by
        design (called right after an approval from the source side).
        """
        with self.engine.begin() as connection:
            connection.execute(
                AGENT_EXCHANGE_RECIPIENTS.update()
                .where(
                    and_(
                        AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                        AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == recipient_team_id,
                    )
                )
                .values(draft_turn_id=turn_id)
            )

    def store_response_draft(
        self, *, exchange_id: str, recipient_team_id: str, content: str
    ) -> None:
        """Store an Agent-drafted reply that still needs human confirmation."""
        with self.engine.begin() as connection:
            recipient = (
                connection.execute(
                    select(AGENT_EXCHANGE_RECIPIENTS.c.status).where(
                        and_(
                            AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                            AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == recipient_team_id,
                        )
                    )
                )
                .scalar_one_or_none()
            )
            if recipient is None:
                raise ResourceNotFound("exchange is not addressed to this team")
            if recipient == ExchangeRecipientStatus.RESPONDED.value:
                return  # already submitted; a retry must not overwrite it
            connection.execute(
                AGENT_EXCHANGE_RECIPIENTS.update()
                .where(
                    and_(
                        AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                        AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == recipient_team_id,
                    )
                )
                .values(
                    draft_content=content.strip(),
                    status=ExchangeRecipientStatus.DRAFTING.value,
                    # Release the drafting turn so the user can re-draft from
                    # the UI; the draft state lives in draft_content now.
                    draft_turn_id=None,
                )
            )

    def clear_response_draft_turn(self, *, turn_id: str) -> None:
        """Release a failed/cancelled drafting turn so the team can retry.

        Keeps the recipient PENDING with no bound turn; the UI falls back to
        the "let the Agent draft" entry instead of hanging on "drafting…".
        """
        with self.engine.begin() as connection:
            connection.execute(
                AGENT_EXCHANGE_RECIPIENTS.update()
                .where(AGENT_EXCHANGE_RECIPIENTS.c.draft_turn_id == turn_id)
                .values(draft_turn_id=None)
            )

    def recipient_for_turn(self, *, turn_id: str) -> tuple[str, str] | None:
        """Map a drafting turn back to (exchange_id, recipient_team_id)."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        AGENT_EXCHANGE_RECIPIENTS.c.exchange_id,
                        AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id,
                    ).where(AGENT_EXCHANGE_RECIPIENTS.c.draft_turn_id == turn_id)
                )
                .mappings()
                .one_or_none()
            )
        return (row["exchange_id"], row["recipient_team_id"]) if row is not None else None

    def exchange_context_for_recipient(
        self, *, exchange_id: str, recipient_team_id: str
    ) -> dict:
        """Assemble the context a recipient team Agent needs to draft a reply."""
        with self.engine.connect() as connection:
            exchange = self._exchange_row(connection, exchange_id=exchange_id)
            snapshot = (
                connection.execute(
                    select(AGENT_EXCHANGE_RECIPIENTS.c.context_snapshot).where(
                        and_(
                            AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange_id,
                            AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == recipient_team_id,
                        )
                    )
                )
                .scalar_one_or_none()
            )
        if snapshot is None:
            raise ResourceNotFound("exchange is not addressed to this team")
        return {
            "exchange_id": exchange_id,
            "project_id": exchange.project_id,
            "source_team_id": exchange.source_team_id,
            "purpose": exchange.purpose,
            "summary": exchange.summary,
            "request": exchange.request,
            "constraints": exchange.constraints,
            "context_snapshot": json.loads(snapshot),
        }

    def assert_conversation_owned(
        self, *, project_id: str, conversation_id: str, actor_id: str
    ) -> None:
        """A source conversation may only feed drafts for its own project/owner.

        Guards the conversation-generated draft endpoint against referencing an
        arbitrary conversation id from a different project or account.
        """
        from .repository import PROJECT_CONVERSATIONS

        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        PROJECT_CONVERSATIONS.c.project_id,
                        PROJECT_CONVERSATIONS.c.account_id,
                    ).where(PROJECT_CONVERSATIONS.c.conversation_id == conversation_id)
                )
                .mappings()
                .one_or_none()
            )
        if (
            row is None
            or row["project_id"] != project_id
            or row["account_id"] != actor_id
        ):
            raise ResourceNotFound("source conversation is unavailable")

    def find_draft_by_turn(self, *, turn_id: str) -> AgentExchangeDraft | None:
        """The draft already generated by an Agent turn, if any.

        Terminal projection retries must never produce a second draft for the
        same turn.
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(AGENT_EXCHANGE_DRAFTS).where(
                        AGENT_EXCHANGE_DRAFTS.c.source_turn_id == turn_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._draft(row) if row is not None else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _payload(*, purpose, summary, request, constraints, shared_resource_ids, recipient_team_ids):
        return {
            "purpose": purpose.strip(),
            "summary": summary.strip(),
            "request": request.strip(),
            "constraints": constraints.strip(),
            "shared_resource_ids": sorted(shared_resource_ids),
            "recipient_team_ids": sorted(recipient_team_ids),
        }

    @staticmethod
    def _validate_recipients(connection, *, project_id, source_team_id, recipient_team_ids):
        if not recipient_team_ids:
            raise ValueError("at least one recipient team is required")
        if source_team_id in recipient_team_ids:
            raise GovernanceConflictError("an exchange cannot be sent to the source team")
        if len(set(recipient_team_ids)) != len(recipient_team_ids):
            raise ValueError("recipient teams must be unique")
        for team_id in recipient_team_ids:
            present = connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        PROJECT_TEAMS.c.team_id == team_id,
                    )
                )
            ).scalar_one_or_none()
            if present is None:
                raise GovernanceConflictError(
                    f"recipient team is not part of the project: {team_id}"
                )

    def _assert_no_private_resources(self, connection, *, project_id, resource_ids):
        for resource_id in resource_ids:
            row = (
                connection.execute(
                    select(
                        PROJECT_RESOURCES.c.propagation,
                        PROJECT_RESOURCES.c.owner_team_id,
                    ).where(
                        and_(
                            PROJECT_RESOURCES.c.resource_id == resource_id,
                            PROJECT_RESOURCES.c.project_id == project_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise GovernanceConflictError(
                    f"shared resource is not part of the project: {resource_id}"
                )
            if row["propagation"] == DataPropagation.TEAM_PRIVATE.value:
                raise GovernanceConflictError(
                    "shared context packages cannot reference team-private resources"
                )

    def _recipient_snapshot(self, connection, *, project_id, team_id) -> dict:
        rows = (
            connection.execute(
                select(
                    PROJECT_RESOURCES.c.resource_id,
                    PROJECT_RESOURCES.c.propagation,
                    PROJECT_RESOURCES.c.owner_team_id,
                ).where(PROJECT_RESOURCES.c.project_id == project_id)
            )
            .mappings()
            .all()
        )
        shared = [
            row["resource_id"]
            for row in rows
            if row["propagation"] != DataPropagation.TEAM_PRIVATE.value
        ]
        own_private = [
            row["resource_id"]
            for row in rows
            if row["propagation"] == DataPropagation.TEAM_PRIVATE.value
            and row["owner_team_id"] == team_id
        ]
        return {
            "project_id": project_id,
            "shared_resource_ids": sorted(shared),
            "own_team_private_resource_ids": sorted(own_private),
        }

    @staticmethod
    def _team_involved(connection, exchange, *, team_id: str) -> bool:
        if exchange.source_team_id == team_id:
            return True
        return (
            connection.execute(
                select(AGENT_EXCHANGE_RECIPIENTS.c.exchange_id).where(
                    and_(
                        AGENT_EXCHANGE_RECIPIENTS.c.exchange_id == exchange.exchange_id,
                        AGENT_EXCHANGE_RECIPIENTS.c.recipient_team_id == team_id,
                    )
                )
            ).scalar_one_or_none()
            is not None
        )

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

    @staticmethod
    def _draft_row(connection, *, draft_id: str):
        row = (
            connection.execute(
                select(AGENT_EXCHANGE_DRAFTS).where(
                    AGENT_EXCHANGE_DRAFTS.c.draft_id == draft_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("exchange draft is unavailable")
        return AgentExchangeService._draft(row)

    @staticmethod
    def _draft(row) -> AgentExchangeDraft:
        return AgentExchangeDraft(
            row["draft_id"],
            row["project_id"],
            row["source_team_id"],
            row["source_conversation_id"],
            row["source_turn_id"],
            row["purpose"],
            row["summary"],
            row["request"],
            row["constraints"],
            tuple(json.loads(row["shared_resource_ids"])),
            tuple(json.loads(row["recipient_team_ids"])),
            row["content_sha256"],
            ExchangeDraftStatus(row["status"]),
            int(row["version"]),
            row["created_by"],
            ProductAccountService._aware(row["created_at"]),
            ProductAccountService._aware(row["updated_at"]),
            ProductAccountService._aware(row["approved_at"]) if row["approved_at"] else None,
            row["rejection_reason"],
        )

    @staticmethod
    def _exchange_row(connection, *, exchange_id: str):
        row = (
            connection.execute(
                select(AGENT_EXCHANGES).where(AGENT_EXCHANGES.c.exchange_id == exchange_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("exchange is unavailable")
        return AgentExchangeService._exchange(row)

    @staticmethod
    def _exchange(row) -> AgentExchange:
        return AgentExchange(
            row["exchange_id"],
            row["project_id"],
            row["source_team_id"],
            row["source_conversation_id"],
            row["source_turn_id"],
            row["purpose"],
            row["summary"],
            row["request"],
            row["constraints"],
            row["content_sha256"],
            ExchangeStatus(row["status"]),
            row["approved_by"],
            ProductAccountService._aware(row["approved_at"]) if row["approved_at"] else None,
            ProductAccountService._aware(row["created_at"]),
        )

    @staticmethod
    def _recipient(row) -> AgentExchangeRecipient:
        return AgentExchangeRecipient(
            row["exchange_id"],
            row["recipient_team_id"],
            json.loads(row["context_snapshot"]),
            ExchangeRecipientStatus(row["status"]),
            row["response_id"],
            ProductAccountService._aware(row["responded_at"]) if row["responded_at"] else None,
            ProductAccountService._aware(row["created_at"]),
            row.get("draft_content"),
            row.get("draft_turn_id"),
        )

    @staticmethod
    def _response_row(connection, *, response_id: str):
        row = (
            connection.execute(
                select(AGENT_EXCHANGE_RESPONSES).where(
                    AGENT_EXCHANGE_RESPONSES.c.response_id == response_id
                )
            )
            .mappings()
            .one()
        )
        return AgentExchangeService._response(row)

    @staticmethod
    def _response(row) -> AgentExchangeResponse:
        return AgentExchangeResponse(
            row["response_id"],
            row["exchange_id"],
            row["recipient_team_id"],
            row["content"],
            row["content_sha256"],
            row["approved_by"],
            ProductAccountService._aware(row["approved_at"]),
            row["turn_id"],
            ProductAccountService._aware(row["created_at"]),
        )
