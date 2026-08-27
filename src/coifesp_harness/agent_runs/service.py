from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable

from ..approvals import ApprovalService, ApprovalStatus
from ..errors import IntegrityError, PolicyDenied, ResourceNotFound
from ..runtime import RunUsage
from ..security import Principal
from .checkpoint import AgentRunCheckpointCodec
from .control_models import AgentControlCommand, AgentControlType
from .models import (
    AgentRunLease,
    DurableAgentEvent,
    DurableAgentRun,
    DurableRunStatus,
    TERMINAL_RUN_STATES,
)
from .repository import SQLAlchemyAgentRunRepository

logger = logging.getLogger("coifesp.agent_runs")


class AgentRunService:
    def __init__(
        self,
        repository: SQLAlchemyAgentRunRepository,
        *,
        approval_service: ApprovalService | None = None,
        checkpoint_codec: AgentRunCheckpointCodec | None = None,
        terminal_callback: Callable[[DurableAgentRun], object] | None = None,
    ) -> None:
        self.repository = repository
        self.approval_service = approval_service
        self.checkpoint_codec = checkpoint_codec or AgentRunCheckpointCodec()
        self.terminal_callback = terminal_callback

    def create(
        self,
        *,
        principal: Principal,
        run_id: str,
        correlation_id: str,
        idempotency_key: str,
        checkpoint: dict[str, Any],
        max_failures: int = 3,
    ) -> DurableAgentRun:
        self._validate_checkpoint(
            checkpoint,
            turns=0,
            tool_calls=0,
            total_tokens=0,
            model_cost_microusd=0,
        )
        return self.repository.create(
            tenant_id=principal.tenant_id,
            owner_principal_id=principal.principal_id,
            run_id=run_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            checkpoint=checkpoint,
            max_failures=max_failures,
        )

    def get(self, *, principal: Principal, run_id: str) -> DurableAgentRun:
        run = self.repository.get(tenant_id=principal.tenant_id, run_id=run_id)
        self._authorize_read(principal, run)
        return run

    def list_runs(self, *, principal: Principal, limit: int = 100) -> tuple[DurableAgentRun, ...]:
        can_read_all = bool(principal.roles.intersection(
            {"agent_run_controller", "platform_administrator"}
        ))
        return self.repository.list_runs(
            tenant_id=principal.tenant_id,
            owner_principal_id=None if can_read_all else principal.principal_id,
            limit=limit,
        )

    def load_checkpoint(self, *, principal: Principal, run_id: str) -> dict[str, Any]:
        """Return the decrypted checkpoint after run-level authorization."""
        self.get(principal=principal, run_id=run_id)
        return self.repository.load_checkpoint(
            tenant_id=principal.tenant_id,
            run_id=run_id,
        )

    def conversation(self, *, principal: Principal, run_id: str) -> tuple[dict[str, Any], ...]:
        """Return the user-visible conversation without system prompts or tool payloads."""
        self.get(principal=principal, run_id=run_id)
        checkpoint = self.repository.load_checkpoint(
            tenant_id=principal.tenant_id,
            run_id=run_id,
        )
        decoded = self.checkpoint_codec.decode(checkpoint)
        visible: list[dict[str, Any]] = []
        for message in decoded["messages"]:
            if message.role not in {"user", "assistant"}:
                continue
            content = message.content
            if message.role == "user":
                try:
                    control = json.loads(content)
                except json.JSONDecodeError:
                    control = None
                if (
                    isinstance(control, dict)
                    and control.get("schema") == "coifesp.agent-control.v1"
                    and isinstance(control.get("content"), str)
                ):
                    content = control["content"]
            visible.append(
                {
                    "sequence": len(visible) + 1,
                    "role": message.role,
                    "content": content,
                    "name": message.name,
                }
            )
        return tuple(visible)

    def events(
        self,
        *,
        principal: Principal,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> tuple[DurableAgentEvent, ...]:
        self.get(principal=principal, run_id=run_id)
        return self.repository.list_events(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def submit_control(
        self,
        *,
        principal: Principal,
        run_id: str,
        command_id: str,
        command_type: AgentControlType,
        content: str,
        expected_run_version: int,
    ) -> AgentControlCommand:
        run = self.get(principal=principal, run_id=run_id)
        self._authorize_control(principal, run)
        return self.repository.submit_control(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            actor_id=principal.principal_id,
            command_id=command_id,
            command_type=command_type,
            content=content,
            expected_run_version=expected_run_version,
        )

    def list_control(
        self,
        *,
        principal: Principal,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[AgentControlCommand, ...]:
        run = self.get(principal=principal, run_id=run_id)
        self._authorize_control(principal, run)
        return self.repository.list_control(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def pending_control(
        self,
        *,
        worker: Principal,
        run_id: str,
        after_sequence: int,
        limit: int = 100,
    ) -> tuple[AgentControlCommand, ...]:
        self._require_worker(worker)
        return self.repository.list_control(
            tenant_id=worker.tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
            pending_only=True,
        )

    def resume_approval(
        self,
        *,
        principal: Principal,
        run_id: str,
        approval_id: str,
        expected_version: int,
    ) -> DurableAgentRun:
        run = self.get(principal=principal, run_id=run_id)
        if run.owner_principal_id != principal.principal_id:
            raise PolicyDenied("only the run owner may resume an approval checkpoint")
        if self.approval_service is None:
            raise PolicyDenied("durable approval verification is not configured")
        approval = self.approval_service.get(
            principal=principal,
            approval_id=approval_id,
        )
        if approval.status is not ApprovalStatus.APPROVED:
            raise PolicyDenied("approval is not approved")
        if approval.origin != "tool_managed":
            raise PolicyDenied("only tool-managed approvals may resume an agent run")
        checkpoint = self.repository.load_checkpoint(
            tenant_id=principal.tenant_id,
            run_id=run_id,
        )
        self._validate_checkpoint(
            checkpoint,
            turns=run.turns,
            tool_calls=run.tool_calls,
            total_tokens=run.total_tokens,
            model_cost_microusd=run.model_cost_microusd,
        )
        bindings = checkpoint.setdefault("approval_bindings", [])
        if not isinstance(bindings, list):
            raise PolicyDenied("agent checkpoint approval bindings are invalid")
        binding = {"call_id": run.pending_call_id, "approval_id": approval_id}
        if binding not in bindings:
            bindings.append(binding)
        return self.repository.requeue_after_approval(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            actor_id=principal.principal_id,
            approval_id=approval_id,
            expected_version=expected_version,
            checkpoint=checkpoint,
        )

    def claim(
        self,
        *,
        worker: Principal,
        lease_seconds: int = 60,
    ) -> AgentRunLease | None:
        self._require_worker(worker)
        return self.repository.claim_next(
            tenant_id=worker.tenant_id,
            worker_id=worker.principal_id,
            lease_seconds=lease_seconds,
        )

    def start(
        self,
        *,
        worker: Principal,
        run_id: str,
        lease_token: str,
    ) -> DurableAgentRun:
        self._require_worker(worker)
        return self.repository.start(
            tenant_id=worker.tenant_id,
            run_id=run_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
        )

    def heartbeat(
        self,
        *,
        worker: Principal,
        run_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> datetime:
        self._require_worker(worker)
        return self.repository.heartbeat(
            tenant_id=worker.tenant_id,
            run_id=run_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
        )

    def checkpoint(
        self,
        *,
        worker: Principal,
        run_id: str,
        lease_token: str,
        target: DurableRunStatus,
        checkpoint: dict[str, Any],
        turns: int,
        tool_calls: int,
        total_tokens: int,
        model_cost_microusd: int = 0,
        pending_call_id: str | None = None,
        pending_approval_id: str | None = None,
        failure_code: str | None = None,
        applied_control_sequences: tuple[int, ...] = (),
    ) -> DurableAgentRun:
        self._require_worker(worker)
        self._validate_checkpoint(
            checkpoint,
            turns=turns,
            tool_calls=tool_calls,
            total_tokens=total_tokens,
            model_cost_microusd=model_cost_microusd,
        )
        run = self.repository.checkpoint(
            tenant_id=worker.tenant_id,
            run_id=run_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            target=target,
            checkpoint=checkpoint,
            turns=turns,
            tool_calls=tool_calls,
            total_tokens=total_tokens,
            model_cost_microusd=model_cost_microusd,
            pending_call_id=pending_call_id,
            pending_approval_id=pending_approval_id,
            failure_code=failure_code,
            applied_control_sequences=applied_control_sequences,
        )
        self._notify_terminal(run)
        return run

    def retry(
        self,
        *,
        worker: Principal,
        run_id: str,
        lease_token: str,
        error_code: str,
        delay_seconds: int,
    ) -> DurableAgentRun:
        self._require_worker(worker)
        return self.repository.retry(
            tenant_id=worker.tenant_id,
            run_id=run_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            error_code=error_code,
            delay_seconds=delay_seconds,
        )

    def abort(
        self,
        *,
        worker: Principal,
        run_id: str,
        lease_token: str,
        error_code: str,
    ) -> DurableAgentRun:
        self._require_worker(worker)
        run = self.repository.abort(
            tenant_id=worker.tenant_id,
            run_id=run_id,
            worker_id=worker.principal_id,
            lease_token=lease_token,
            error_code=error_code,
        )
        self._notify_terminal(run)
        return run

    def _notify_terminal(self, run: DurableAgentRun) -> None:
        """Fire the terminal projection callback without breaking the worker."""
        if self.terminal_callback is None or run.status not in TERMINAL_RUN_STATES:
            return
        try:
            result = self.terminal_callback(run)
            if hasattr(result, "__await__"):
                import asyncio

                asyncio.get_event_loop().create_task(result)
        except Exception:
            logger.exception("terminal projection failed run_id=%s", run.run_id)

    @staticmethod
    def _authorize_read(principal: Principal, run: DurableAgentRun) -> None:
        if run.owner_principal_id != principal.principal_id and not principal.roles.intersection(
            {"agent_run_controller", "platform_administrator"}
        ):
            raise ResourceNotFound("agent run is absent or hidden")

    @staticmethod
    def _authorize_control(principal: Principal, run: DurableAgentRun) -> None:
        if run.owner_principal_id != principal.principal_id and not principal.roles.intersection(
            {"agent_run_controller", "platform_administrator"}
        ):
            raise PolicyDenied("principal cannot control this agent run")

    @staticmethod
    def _require_worker(principal: Principal) -> None:
        if not principal.is_service or "agent_worker" not in principal.roles:
            raise PolicyDenied("agent run lease operations require an agent worker identity")

    def _validate_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        turns: int,
        tool_calls: int,
        total_tokens: int,
        model_cost_microusd: int,
    ) -> None:
        try:
            self.checkpoint_codec.validate(
                checkpoint,
                usage=RunUsage(turns, tool_calls, total_tokens, model_cost_microusd),
            )
        except (IntegrityError, ValueError) as exc:
            raise PolicyDenied("agent checkpoint validation failed") from exc
