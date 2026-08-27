"""Project conversation turn projection for terminal Agent Runs.

When a Durable Run reaches a terminal state, this projector hydrates the
owning project conversation:

- completed: the final assistant message is appended to the conversation and
  the Turn is completed; a planning turn additionally imports the structured
  plan draft exactly once.
- failed/cancelled: the Turn is terminated so the user can retry without
  losing their message.

All operations are idempotent; worker retries never duplicate messages.
"""
from __future__ import annotations

import json
import logging
import re
import secrets

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..agent_runs import DurableRunStatus
from ..errors import HarnessError
from .models import TurnStatus, TurnTriggerKind
from .repository import (
    ACCOUNTS,
    PROJECT_AGENT_RUNS,
    PROJECT_AGENT_TURNS,
    PROJECT_CONVERSATIONS,
)
from .workspace import ProjectWorkspaceService

logger = logging.getLogger("coifesp.product.turn_projection")

_TERMINAL_STATUSES = frozenset(
    {
        DurableRunStatus.COMPLETED,
        DurableRunStatus.FAILED,
        DurableRunStatus.CANCELLED,
    }
)


def _friendly_draft_message(purpose: str, summary: str) -> str:
    preview = " ".join((summary or "").split())[:200]
    return (
        f"已按对话起草一份跨团队共享草稿：{purpose}。"
        + (f"（{preview}）" if preview else "")
        + "请在协作面板查看、编辑并确认后发送。"
    )


class AgentTurnProjection:
    def __init__(
        self,
        engine: Engine,
        *,
        workspace: ProjectWorkspaceService,
        planning=None,
        exchange=None,
        run_reader=None,
        context_reader=None,
    ) -> None:
        self.engine = engine
        self.workspace = workspace
        self.planning = planning
        self.exchange = exchange
        self.run_reader = run_reader
        self.context_reader = context_reader

    def on_run_terminal(self, run) -> None:
        binding = self._binding(run.run_id)
        if binding is None:
            # A run may reach its terminal state before the conversation link
            # is written; fall back to the correlation id to avoid losing the
            # projection (and the turn staying active forever).
            binding = self._binding_from_correlation(run)
        if binding is None:
            return
        conversation_id, turn_id, project_id = binding
        self._heal_binding(
            conversation_id=conversation_id, turn_id=turn_id, run_id=run.run_id
        )
        try:
            turn = self.workspace.get_turn(
                conversation_id=conversation_id, turn_id=turn_id
            )
        except HarnessError:
            logger.warning("turn projection: unknown turn %s", turn_id)
            return
        if turn.status is not TurnStatus.ACTIVE:
            return
        if run.status is DurableRunStatus.COMPLETED:
            self._project_completed(
                run, conversation_id=conversation_id, turn_id=turn_id,
                project_id=project_id, turn=turn,
            )
        elif run.status is DurableRunStatus.FAILED:
            # Release the recipient drafting binding BEFORE terminating the
            # turn: if the process dies between the two steps the turn is
            # still ACTIVE and the startup replay finishes the cleanup.
            self._release_draft_turn(turn_id)
            self.workspace.fail_turn(conversation_id=conversation_id, turn_id=turn_id)
        elif run.status is DurableRunStatus.CANCELLED:
            self._release_draft_turn(turn_id)
            self.workspace.cancel_turn(conversation_id=conversation_id, turn_id=turn_id)

    def _heal_binding(self, *, conversation_id: str, turn_id: str, run_id: str) -> None:
        """Idempotently (re)write the turn->run binding when it was missed."""
        try:
            self.workspace.bind_turn_run(
                conversation_id=conversation_id, turn_id=turn_id, run_id=run_id
            )
        except (HarnessError, ValueError):
            pass

    def _release_draft_turn(self, turn_id: str) -> None:
        """Release a recipient drafting turn after its run failed/cancelled."""
        if self.exchange is None:
            return
        try:
            self.exchange.clear_response_draft_turn(turn_id=turn_id)
        except (ValueError, HarnessError) as exc:
            logger.warning("exchange draft turn release skipped: %s", exc)

    def _binding_from_correlation(self, run) -> tuple[str, str, str] | None:
        correlation = getattr(run, "correlation_id", "") or ""
        # Matches the conversation/turn segment in every correlation layout:
        # plain conversation runs (conv: prefix), exchange-draft runs (both the
        # current exchange-draft:conv: prefix and the legacy exchange-draft:
        # prefix written by older builds) and exchange reply runs (which carry
        # conv/turn after the exchange:...:recipient:... segment).
        match = re.search(r"(?:conv:|exchange-draft:)([^:]+):turn:([^:]+)", correlation)
        if match is None:
            return None
        conversation_id, turn_id = match.group(1), match.group(2)
        try:
            conversation = self.workspace.get_conversation_by_id(
                conversation_id=conversation_id
            )
        except HarnessError:
            return None
        return conversation.conversation_id, turn_id, conversation.project_id

    def _project_completed(
        self, run, *, conversation_id, turn_id, project_id, turn
    ) -> None:
        content = self._final_assistant_content(run)
        if content is None:
            # No usable reply: release any recipient drafting binding first so
            # a crash between the release and the termination leaves the turn
            # ACTIVE and replay_pending finishes the cleanup.
            self._release_draft_turn(turn_id)
            self.workspace.fail_turn(conversation_id=conversation_id, turn_id=turn_id)
            return
        display_content = content
        if (
            self.planning is not None
            and turn.trigger_kind is TurnTriggerKind.PLANNING
        ):
            # Import the plan while the turn is still active. If the process
            # dies between the import and the completion, the retry re-imports
            # idempotently (one draft per run) and then completes the turn, so
            # a crash can never leave the plan draft missing.
            self._import_plan(
                project_id=project_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run.run_id,
                content=content,
            )
        if self.exchange is not None and turn.trigger_kind is TurnTriggerKind.EXCHANGE:
            # Store the drafted reply before completing the turn; a retry
            # stores idempotently and then completes.
            self._store_exchange_draft(turn_id=turn_id, content=content)
        if (
            self.exchange is not None
            and turn.trigger_kind is TurnTriggerKind.EXCHANGE_DRAFT
        ):
            # Turn the assistant output into a DRAFTING exchange draft before
            # completing; a retry finds the existing draft and does not import
            # a second one.
            friendly = self._project_exchange_draft(
                run=run,
                project_id=project_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                content=content,
            )
            if friendly:
                # Keep the protocol JSON out of the chat; the draft itself
                # lives in the collaboration drawer.
                display_content = friendly
        self.workspace.complete_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            assistant_content=display_content,
            run_id=run.run_id,
        )

    def _import_plan(self, *, project_id, conversation_id, turn_id, run_id, content) -> None:
        actor_id = self._conversation_account(conversation_id)
        if actor_id is None:
            logger.warning("plan projection: conversation %s has no account", conversation_id)
            return
        try:
            self.planning.import_plan_draft(
                project_id=project_id,
                actor_id=actor_id,
                content=content,
                source_conversation_id=conversation_id,
                source_turn_id=turn_id,
                source_run_id=run_id,
            )
        except (ValueError, HarnessError) as exc:
            # The plan output failed validation; the assistant reply is still
            # delivered and the user can ask the Agent to retry.
            logger.warning("plan projection skipped: %s", exc)

    def _store_exchange_draft(self, *, turn_id: str, content: str) -> None:
        if self.exchange is None:
            return
        try:
            recipient = self.exchange.recipient_for_turn(turn_id=turn_id)
        except HarnessError:
            return
        if recipient is None:
            logger.warning("exchange draft projection: turn %s has no recipient", turn_id)
            return
        exchange_id, recipient_team_id = recipient
        try:
            self.exchange.store_response_draft(
                exchange_id=exchange_id,
                recipient_team_id=recipient_team_id,
                content=content,
            )
        except (ValueError, HarnessError) as exc:
            logger.warning("exchange draft projection skipped: %s", exc)
            # The drafted reply failed to store; release the drafting binding
            # so the team can ask for a fresh draft instead of being stuck on
            # "Agent is drafting…".
            self._release_draft_turn(turn_id)

    def _project_exchange_draft(
        self, *, run, project_id, conversation_id, turn_id, content
    ) -> str | None:
        """Project an exchange-draft turn; returns a friendly chat message.

        The assistant output (coifesp.exchange-draft.v1 JSON) becomes a
        DRAFTING exchange draft. The returned text replaces the raw JSON in
        the conversation; a retry or a concurrent terminal callback converges
        on the single draft and returns the same friendly message.
        """
        actor_id = self._conversation_account(conversation_id)
        if actor_id is None:
            logger.warning(
                "exchange-draft projection: conversation %s has no account",
                conversation_id,
            )
            return None
        # Terminal projection retries must never import two drafts per turn.
        existing = self.exchange.find_draft_by_turn(turn_id=turn_id)
        if existing is not None:
            return _friendly_draft_message(existing.purpose, existing.summary)
        intent = self._draft_intent(run, turn_id=turn_id)
        if intent is None:
            logger.warning("exchange-draft projection: turn %s has no intent", turn_id)
            return None
        try:
            payload = json.loads(content)
        except (ValueError, TypeError) as exc:
            logger.warning("exchange-draft projection skipped (invalid JSON): %s", exc)
            return None
        if payload.get("schema") != "coifesp.exchange-draft.v1":
            logger.warning("exchange-draft projection skipped (bad schema)")
            return None
        purpose = str(payload.get("purpose") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        request = str(payload.get("request") or "").strip()
        constraints = str(payload.get("constraints") or "").strip()
        if not purpose or not summary or not request:
            logger.warning("exchange-draft projection skipped (missing fields)")
            return None
        recipient_team_ids = tuple(intent.get("recipient_team_ids") or ())
        shared_resource_ids = tuple(intent.get("shared_resource_ids") or ())
        if not recipient_team_ids:
            logger.warning("exchange-draft projection skipped (no recipients)")
            return None
        try:
            self.exchange.create_draft(
                draft_id=f"draft-{secrets.token_hex(12)}",
                project_id=project_id,
                actor_id=actor_id,
                purpose=purpose,
                summary=summary,
                request=request,
                constraints=constraints,
                shared_resource_ids=shared_resource_ids,
                recipient_team_ids=recipient_team_ids,
                source_conversation_id=conversation_id,
                source_turn_id=turn_id,
            )
        except IntegrityError:
            # A concurrent terminal callback imported the draft first; do not
            # claim success blindly — verify the draft really exists. If it
            # does not (e.g. a foreign-key or check failure), re-raise so the
            # turn stays ACTIVE and the startup replay can retry it.
            existing = self.exchange.find_draft_by_turn(turn_id=turn_id)
            if existing is None:
                raise
            return _friendly_draft_message(existing.purpose, existing.summary)
        except (ValueError, HarnessError) as exc:
            # Invalid drafts stay logged; the assistant reply is still shown
            # so the user can ask the Agent to retry.
            logger.warning("exchange-draft projection skipped: %s", exc)
            return None
        return _friendly_draft_message(purpose, summary)

    def _draft_intent(self, run, *, turn_id: str) -> dict | None:
        """The recipient/shared intent the exchange-draft run was created with."""
        if self.context_reader is None:
            return None
        try:
            items = self.context_reader(run)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "exchange-draft intent read failed run_id=%s: %s", run.run_id, exc
            )
            return None
        target = None
        for item in items or ():
            item_id = getattr(item, "item_id", None)
            if item_id is None and isinstance(item, dict):
                item_id = item.get("item_id")
            if (item_id or "").startswith("exchange-draft-intent:"):
                target = item
                if item_id == f"exchange-draft-intent:{turn_id}":
                    break
        if target is None:
            return None
        content = getattr(target, "content", None)
        if content is None and isinstance(target, dict):
            content = target.get("content")
        try:
            payload = json.loads(content or "{}")
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def replay_pending(self, agent_run_service) -> int:
        """Replay terminal runs whose conversation turn is still active.

        Startup recovery: when the process dies between a run reaching a
        terminal state and the projection writing the result, the turn stays
        ACTIVE with nothing scheduled to replay it. Scanning at startup closes
        that gap; projections are idempotent so replaying twice is safe.
        """
        from ..security import Classification, Principal

        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        PROJECT_AGENT_TURNS.c.turn_id,
                        PROJECT_AGENT_TURNS.c.conversation_id,
                        PROJECT_AGENT_TURNS.c.run_id,
                    ).where(PROJECT_AGENT_TURNS.c.status == TurnStatus.ACTIVE.value)
                )
                .mappings()
                .all()
            )
        replayed = 0
        for row in rows:
            run_id = row["run_id"]
            if run_id is None:
                # The run finished (or the process died) before the binding
                # was written; correlate on the id to find the run anyway.
                run_id = self._find_unbound_run_id(
                    conversation_id=row["conversation_id"], turn_id=row["turn_id"]
                )
            if run_id is None:
                continue
            try:
                principal = self._replay_principal(row["conversation_id"])
                if principal is None:
                    continue
                run = agent_run_service.get(principal=principal, run_id=run_id)
                if run.status not in _TERMINAL_STATUSES:
                    continue
                self.on_run_terminal(run)
                replayed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("replay skipped run_id=%s: %s", run_id, exc)
        return replayed

    def _find_unbound_run_id(
        self, *, conversation_id: str, turn_id: str
    ) -> str | None:
        """Find a run that references this turn but was never bound to it.

        The correlation id carries the conversation/turn segment for every
        conversation-scoped run (user_message, exchange-draft and exchange
        reply), keyed by the same tenant as the conversation. The trailing
        :{conversation_id}:turn:{turn_id} segment matches every layout: the
        conv: prefix, the current exchange-draft:conv: prefix and the legacy
        exchange-draft: prefix written by older builds.
        """
        from ..agent_runs.repository import AGENT_RUNS as AGENT_RUNS_TABLE

        team_id = self._replay_team(conversation_id)
        if team_id is None:
            return None
        prefix = f":{conversation_id}:turn:{turn_id}"
        try:
            with self.engine.connect() as connection:
                return connection.execute(
                    select(AGENT_RUNS_TABLE.c.run_id).where(
                        and_(
                            AGENT_RUNS_TABLE.c.tenant_id == team_id,
                            AGENT_RUNS_TABLE.c.correlation_id.like(f"%{prefix}"),
                        )
                    )
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            return None

    def _replay_principal(self, conversation_id: str):
        from ..security import Classification, Principal

        account = self._replay_account(conversation_id)
        team_id = self._replay_team(conversation_id)
        if account is None or team_id is None:
            return None
        return Principal(
            account,
            team_id,
            frozenset({"agent_run_controller"}),
            Classification.RESTRICTED,
            frozenset(),
        )

    def _replay_account(self, conversation_id: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(PROJECT_CONVERSATIONS.c.account_id).where(
                    PROJECT_CONVERSATIONS.c.conversation_id == conversation_id
                )
            ).scalar_one_or_none()

    def _replay_team(self, conversation_id: str) -> str | None:
        account = self._replay_account(conversation_id)
        if account is None:
            return None
        with self.engine.connect() as connection:
            return connection.execute(
                select(ACCOUNTS.c.team_id).where(ACCOUNTS.c.account_id == account)
            ).scalar_one_or_none()

    def _final_assistant_content(self, run) -> str | None:
        if self.run_reader is None:
            return None
        try:
            messages = self.run_reader(run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("run conversation read failed run_id=%s: %s", run.run_id, exc)
            return None
        for message in reversed(messages or ()):
            if not isinstance(message, dict):
                continue
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            return content
        return None

    def _binding(self, run_id: str):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        PROJECT_AGENT_RUNS.c.conversation_id,
                        PROJECT_AGENT_RUNS.c.turn_id,
                        PROJECT_AGENT_RUNS.c.project_id,
                    ).where(PROJECT_AGENT_RUNS.c.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["conversation_id"] is None or row["turn_id"] is None:
            return None
        return row["conversation_id"], row["turn_id"], row["project_id"]

    def _conversation_account(self, conversation_id: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(PROJECT_CONVERSATIONS.c.account_id).where(
                    PROJECT_CONVERSATIONS.c.conversation_id == conversation_id
                )
            ).scalar_one_or_none()