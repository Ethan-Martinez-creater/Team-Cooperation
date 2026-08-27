from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Iterator
from typing import Any

from sqlalchemy import and_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import Connection

from ..agent_runs import (
    AGENT_RUNS,
    AgentRunPersistenceError,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from ..runtime import AgentRunResult
from .models import ToolJobStatus
from .repository import SQLAlchemyToolJobRepository, ToolJobError

_TERMINAL = frozenset({ToolJobStatus.SUCCEEDED, ToolJobStatus.FAILED, ToolJobStatus.CANCELLED})


class ToolBatchCoordinator:
    """Owns the atomic boundary between Agent checkpoints and Tool Jobs."""

    def __init__(
        self,
        *,
        engine: Engine,
        agent_runs: SQLAlchemyAgentRunRepository,
        tool_jobs: SQLAlchemyToolJobRepository,
        _bound_connection: Connection | None = None,
    ) -> None:
        if agent_runs.engine is not engine or tool_jobs.engine is not engine:
            raise ValueError("coordinator repositories must share one database engine")
        self.engine = engine
        self.agent_runs = agent_runs
        self.tool_jobs = tool_jobs
        self._bound_connection = _bound_connection

    def using_connection(self, connection: Connection) -> "ToolBatchCoordinator":
        return ToolBatchCoordinator(
            engine=self.engine,
            agent_runs=self.agent_runs,
            tool_jobs=self.tool_jobs,
            _bound_connection=connection,
        )

    def dispatch(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        checkpoint: dict[str, Any],
        result: AgentRunResult,
        max_attempts: int = 3,
    ) -> None:
        calls = self._pending_calls(checkpoint)
        if not calls:
            raise ToolJobError("awaiting-tool checkpoint contains no dispatches")
        authorization = checkpoint.get("tool_authorization")
        if authorization is not None:
            authorized_ids = {
                entry.get("tool_id") for entry in authorization.get("tools", [])
            }
            unauthorized = sorted(
                {tool_name for _, tool_name, _ in calls}.difference(authorized_ids)
            )
            if unauthorized:
                # Defense in depth: the loop already filters tools per run, so
                # reaching here means a checkpoint was tampered with or an
                # executor bypassed the run-scoped registry.
                raise ToolJobError(
                    "tool batch requests tools outside the run authorization: "
                    + ", ".join(unauthorized)
                )
        with self._transaction() as connection:
            agent_runs = self.agent_runs.using_connection(connection)
            tool_jobs = self.tool_jobs.using_connection(connection)
            # The checkpoint transition locks and fences the parent Run. Any
            # failure below rolls it back together with every Tool Job.
            agent_runs.checkpoint(
                tenant_id=tenant_id,
                run_id=run_id,
                worker_id=worker_id,
                lease_token=lease_token,
                target=DurableRunStatus.AWAITING_TOOL,
                checkpoint=checkpoint,
                turns=result.turns,
                tool_calls=result.tool_calls,
                total_tokens=result.total_tokens,
                model_cost_microusd=result.model_cost_microusd,
                applied_control_sequences=result.applied_control_sequences,
            )
            for call_id, tool_name, arguments in calls:
                tool_jobs.enqueue(
                    tenant_id=tenant_id,
                    actor_id=worker_id,
                    job_id=self._job_id(run_id, call_id),
                    run_id=run_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    idempotency_key=f"{run_id}:{call_id}",
                    arguments=arguments,
                    max_attempts=max_attempts,
                )

    def wake_if_complete(self, *, tenant_id: str, run_id: str, actor_id: str) -> bool:
        """Inject a complete batch once and atomically requeue its parent Run."""
        with self._transaction() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
                    {"tenant": tenant_id},
                )
            row = (
                connection.execute(
                    select(AGENT_RUNS)
                    .where(
                        and_(
                            AGENT_RUNS.c.tenant_id == tenant_id,
                            AGENT_RUNS.c.run_id == run_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["status"] != DurableRunStatus.AWAITING_TOOL.value:
                return False
            tool_jobs = self.tool_jobs.using_connection(connection)
            agent_runs = self.agent_runs.using_connection(connection)
            checkpoint = agent_runs.load_checkpoint(tenant_id=tenant_id, run_id=run_id)
            expected = {call_id for call_id, _, _ in self._pending_calls(checkpoint)}
            if not expected:
                raise ToolJobError("awaiting Agent has no pending Tool batch")
            all_jobs = tool_jobs.list_for_run(
                tenant_id=tenant_id,
                run_id=run_id,
                include_payloads=True,
                connection=connection,
            )
            by_call = {job.call_id: job for job in all_jobs}
            if not expected.issubset(by_call):
                raise ToolJobError("tool batch differs from the Agent checkpoint")
            jobs = tuple(by_call[call_id] for call_id in expected)
            if any(job.status not in _TERMINAL for job in jobs):
                return False
            for message in checkpoint["messages"]:
                call_id = message.get("tool_call_id")
                if message.get("role") != "tool" or call_id not in expected:
                    continue
                job = by_call[call_id]
                payload = json.loads(message["content"])
                if payload.get("payload", {}).get("status") != "dispatch_required":
                    raise ToolJobError("tool result placeholder is malformed")
                payload["payload"] = {
                    "status": ("succeeded" if job.status is ToolJobStatus.SUCCEEDED else "failed"),
                    "output": job.result if job.status is ToolJobStatus.SUCCEEDED else None,
                    "error": None if job.status is ToolJobStatus.SUCCEEDED else job.error_code,
                    "approval_id": None,
                    "request_digest": job.request_digest,
                }
                message["content"] = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            try:
                agent_runs.requeue_after_tools(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    actor_id=actor_id,
                    expected_version=int(row["version"]),
                    checkpoint=checkpoint,
                )
            except AgentRunPersistenceError as exc:
                raise ToolJobError("Agent tool wake-up failed") from exc
            return True

    def reconcile(self, *, tenant_id: str, actor_id: str, limit: int = 100) -> int:
        """Repair the safe crash window between Tool completion and Run wake-up."""
        if not 1 <= limit <= 1000:
            raise ToolJobError("tool batch reconciliation limit is invalid")
        with self._transaction() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
                    {"tenant": tenant_id},
                )
            run_ids = tuple(
                connection.execute(
                    select(AGENT_RUNS.c.run_id)
                    .where(
                        and_(
                            AGENT_RUNS.c.tenant_id == tenant_id,
                            AGENT_RUNS.c.status == DurableRunStatus.AWAITING_TOOL.value,
                        )
                    )
                    .order_by(AGENT_RUNS.c.updated_at)
                    .limit(limit)
                ).scalars()
            )
        return sum(
            self.wake_if_complete(tenant_id=tenant_id, run_id=run_id, actor_id=actor_id)
            for run_id in run_ids
        )

    @staticmethod
    def _pending_calls(checkpoint: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        definitions: dict[str, tuple[str, dict[str, Any]]] = {}
        for message in checkpoint.get("messages", []):
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls", []):
                definitions[call["call_id"]] = (call["name"], call["arguments"])
        pending: list[tuple[str, str, dict[str, Any]]] = []
        for message in checkpoint.get("messages", []):
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message["content"])["payload"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ToolJobError("tool checkpoint message is malformed") from exc
            if payload.get("status") != "dispatch_required":
                continue
            call_id = message.get("tool_call_id")
            if call_id not in definitions or any(item[0] == call_id for item in pending):
                raise ToolJobError("tool checkpoint dispatch identity is invalid")
            tool_name, arguments = definitions[call_id]
            pending.append((call_id, tool_name, arguments))
        return pending

    @staticmethod
    def _job_id(run_id: str, call_id: str) -> str:
        digest = hashlib.sha256(f"{run_id}\0{call_id}".encode()).hexdigest()
        return f"tool-job-{digest[:48]}"

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if self._bound_connection is not None:
            yield self._bound_connection
            return
        with self.engine.begin() as connection:
            yield connection
