from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..errors import (
    AuthenticationError,
    ContextAssemblyError,
    HarnessError,
    IdentityProviderUnavailable,
    IntegrityError,
    PolicyDenied,
)
from ..runtime import AgentLoop, AgentRunResult, ControlDirective, RuntimeControlSource
from ..runtime.providers import ModelRoutingError, ProviderInvocationError
from ..security import Principal
from .checkpoint import AgentRunCheckpointCodec
from .models import AgentRunLease, DurableAgentRun, DurableRunStatus
from .repository import AgentRunPersistenceError
from .service import AgentRunService


class PrincipalResolver(Protocol):
    async def resolve(self, *, tenant_id: str, principal_id: str) -> Principal:
        """Resolve current authorization attributes; stale checkpoint claims are forbidden."""

    async def resolve_for_run(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        run_id: str,
    ) -> Principal:
        """Resolve a principal against the run's authoritative delegation binding."""


class WorkerIdentityProvider(Protocol):
    async def resolve(self) -> Principal:
        """Resolve and verify the current machine identity used to claim work."""


class AgentWorkerObserver(Protocol):
    def record_worker_outcome(self, *, outcome: str, error_code: str | None) -> None: ...


class ToolBatchDispatcher(Protocol):
    def dispatch(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        checkpoint: dict,
        result: AgentRunResult,
        max_attempts: int = 3,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FailureDisposition:
    error_code: str
    retryable: bool


class AgentWorkerFailureClassifier:
    """Maps exceptions to bounded, non-secret operational codes."""

    def classify(self, error: Exception) -> FailureDisposition:
        if isinstance(error, IdentityProviderUnavailable):
            return FailureDisposition("identity_provider_unavailable", True)
        if isinstance(error, TimeoutError):
            return FailureDisposition("dependency_timeout", True)
        if isinstance(error, ConnectionError):
            return FailureDisposition("dependency_connection_failed", True)
        if isinstance(error, ProviderInvocationError):
            return FailureDisposition(
                f"model_provider_{error.kind.value}",
                error.retryable,
            )
        if isinstance(error, ModelRoutingError):
            return FailureDisposition("model_route_unavailable", False)
        if isinstance(error, OSError):
            return FailureDisposition("dependency_io_failed", True)
        if isinstance(error, IntegrityError):
            return FailureDisposition("checkpoint_integrity_failed", False)
        if isinstance(error, ContextAssemblyError):
            return FailureDisposition("context_assembly_failed", False)
        if isinstance(error, PolicyDenied):
            return FailureDisposition("policy_denied", False)
        if isinstance(error, (TypeError, ValueError)):
            return FailureDisposition("runtime_input_invalid", False)
        if isinstance(error, HarnessError):
            return FailureDisposition("harness_operation_failed", False)
        return FailureDisposition("worker_unexpected_error", True)


class AgentWorkerOutcomeStatus(str, Enum):
    IDLE = "idle"
    COMPLETED = "completed"
    CONTINUATION_QUEUED = "continuation_queued"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_TOOL = "awaiting_tool"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class AgentWorkerOutcome:
    status: AgentWorkerOutcomeStatus
    run_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DurableAgentControlSource(RuntimeControlSource):
    """Reads encrypted, tenant-scoped commands through the worker service boundary."""

    service: AgentRunService
    worker: Principal
    run_id: str
    batch_size: int = 100

    async def poll(self, *, after_sequence: int) -> tuple[ControlDirective, ...]:
        commands = await asyncio.to_thread(
            self.service.pending_control,
            worker=self.worker,
            run_id=self.run_id,
            after_sequence=after_sequence,
            limit=self.batch_size,
        )
        return tuple(
            ControlDirective(
                sequence=command.sequence,
                command_id=command.command_id,
                command_type=command.command_type.value,
                content=command.content,
            )
            for command in commands
        )


class DurableAgentWorker:
    """Executes one leased durable Run with live identity and lease heartbeats."""

    def __init__(
        self,
        *,
        service: AgentRunService,
        loop: AgentLoop,
        principal_resolver: PrincipalResolver,
        codec: AgentRunCheckpointCodec | None = None,
        failure_classifier: AgentWorkerFailureClassifier | None = None,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float = 20.0,
        retry_base_seconds: int = 5,
        retry_cap_seconds: int = 300,
        observer: AgentWorkerObserver | None = None,
        tool_dispatcher: ToolBatchDispatcher | None = None,
    ) -> None:
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("worker lease duration is invalid")
        if not 0 < heartbeat_interval_seconds < lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        if not 1 <= retry_base_seconds <= retry_cap_seconds <= 3600:
            raise ValueError("worker retry policy is invalid")
        self.service = service
        self.loop = loop
        self.principal_resolver = principal_resolver
        self.codec = codec or AgentRunCheckpointCodec()
        self.failure_classifier = failure_classifier or AgentWorkerFailureClassifier()
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_cap_seconds = retry_cap_seconds
        self.observer = observer
        self.tool_dispatcher = tool_dispatcher

    async def process_once(self, *, worker: Principal) -> AgentWorkerOutcome:
        lease = await asyncio.to_thread(
            self.service.claim,
            worker=worker,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return self._outcome(AgentWorkerOutcomeStatus.IDLE)
        run_id = lease.run.run_id
        try:
            await asyncio.to_thread(
                self.service.start,
                worker=worker,
                run_id=run_id,
                lease_token=lease.lease_token,
            )
            resolve_for_run = getattr(self.principal_resolver, "resolve_for_run", None)
            if callable(resolve_for_run):
                principal = await resolve_for_run(
                    tenant_id=lease.run.tenant_id,
                    principal_id=lease.run.owner_principal_id,
                    run_id=run_id,
                )
                verified = True
            else:
                principal = await self.principal_resolver.resolve(
                    tenant_id=lease.run.tenant_id,
                    principal_id=lease.run.owner_principal_id,
                )
                verified = False
            control_source = DurableAgentControlSource(
                service=self.service,
                worker=worker,
                run_id=run_id,
            )
            request = self.codec.request(
                lease=lease,
                principal=principal,
                control_source=control_source,
                verified=verified,
            )
            result = await self._run_with_heartbeat(
                worker=worker,
                lease=lease,
                request=request,
            )
            checkpoint = self.codec.result(request=request, result=result)
            if result.status == "completed":
                persisted = await self._checkpoint(
                    worker=worker,
                    lease=lease,
                    result=result,
                    checkpoint=checkpoint,
                    target=DurableRunStatus.COMPLETED,
                )
                return self._outcome(
                    (
                        AgentWorkerOutcomeStatus.CONTINUATION_QUEUED
                        if persisted.status is DurableRunStatus.QUEUED
                        else AgentWorkerOutcomeStatus.COMPLETED
                    ),
                    run_id=run_id,
                )
            if result.status == "awaiting_approval":
                call_id, approval_id = self._pending_approval(result)
                await self._checkpoint(
                    worker=worker,
                    lease=lease,
                    result=result,
                    checkpoint=checkpoint,
                    target=DurableRunStatus.AWAITING_APPROVAL,
                    pending_call_id=call_id,
                    pending_approval_id=approval_id,
                )
                return self._outcome(
                    AgentWorkerOutcomeStatus.AWAITING_APPROVAL,
                    run_id=run_id,
                )
            if result.status == "awaiting_tool":
                if self.tool_dispatcher is None:
                    raise ValueError("durable tool result requires a Tool Batch dispatcher")
                await asyncio.to_thread(
                    self.tool_dispatcher.dispatch,
                    tenant_id=lease.run.tenant_id,
                    run_id=run_id,
                    worker_id=worker.principal_id,
                    lease_token=lease.lease_token,
                    checkpoint=checkpoint,
                    result=result,
                )
                return self._outcome(AgentWorkerOutcomeStatus.AWAITING_TOOL, run_id=run_id)
            error_code = self._result_error_code(result)
            await self._checkpoint(
                worker=worker,
                lease=lease,
                result=result,
                checkpoint=checkpoint,
                target=DurableRunStatus.FAILED,
                failure_code=error_code,
            )
            return self._outcome(
                AgentWorkerOutcomeStatus.FAILED,
                run_id=run_id,
                error_code=error_code,
            )
        except asyncio.CancelledError:
            raise
        except AgentRunPersistenceError:
            return self._outcome(
                AgentWorkerOutcomeStatus.LEASE_LOST,
                run_id=run_id,
                error_code="lease_lost",
            )
        except Exception as exc:
            disposition = self.failure_classifier.classify(exc)
            if disposition.retryable:
                delay = self._retry_delay(lease)
                try:
                    run = await asyncio.to_thread(
                        self.service.retry,
                        worker=worker,
                        run_id=run_id,
                        lease_token=lease.lease_token,
                        error_code=disposition.error_code,
                        delay_seconds=delay,
                    )
                except AgentRunPersistenceError:
                    return self._outcome(
                        AgentWorkerOutcomeStatus.LEASE_LOST,
                        run_id=run_id,
                        error_code="lease_lost",
                    )
                status = (
                    AgentWorkerOutcomeStatus.FAILED
                    if run.status is DurableRunStatus.FAILED
                    else AgentWorkerOutcomeStatus.RETRY_SCHEDULED
                )
                return self._outcome(status, run_id, disposition.error_code)
            try:
                await asyncio.to_thread(
                    self.service.abort,
                    worker=worker,
                    run_id=run_id,
                    lease_token=lease.lease_token,
                    error_code=disposition.error_code,
                )
            except AgentRunPersistenceError:
                return self._outcome(
                    AgentWorkerOutcomeStatus.LEASE_LOST,
                    run_id=run_id,
                    error_code="lease_lost",
                )
            return self._outcome(
                AgentWorkerOutcomeStatus.FAILED,
                run_id,
                disposition.error_code,
            )

    def _outcome(
        self,
        status: AgentWorkerOutcomeStatus,
        run_id: str | None = None,
        error_code: str | None = None,
    ) -> AgentWorkerOutcome:
        if self.observer is not None:
            self.observer.record_worker_outcome(
                outcome=status.value,
                error_code=error_code,
            )
        return AgentWorkerOutcome(status, run_id, error_code)

    async def _run_with_heartbeat(self, *, worker, lease, request) -> AgentRunResult:
        run_task = asyncio.create_task(self.loop.run(request))
        heartbeat_task = asyncio.create_task(self._heartbeat(worker=worker, lease=lease))
        done, _ = await asyncio.wait(
            {run_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            error = heartbeat_task.exception()
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            if error is None:
                raise AgentRunPersistenceError("agent run heartbeat stopped unexpectedly")
            raise AgentRunPersistenceError("agent run lease heartbeat failed") from error
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        return await run_task

    async def _heartbeat(self, *, worker: Principal, lease: AgentRunLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            # Cancelling asyncio.to_thread does not stop the underlying thread.
            # Shield and drain the current heartbeat so the terminal checkpoint
            # can never race a late lease update on the same connection.
            call = asyncio.create_task(
                asyncio.to_thread(
                    self.service.heartbeat,
                    worker=worker,
                    run_id=lease.run.run_id,
                    lease_token=lease.lease_token,
                    lease_seconds=self.lease_seconds,
                )
            )
            try:
                await asyncio.shield(call)
            except asyncio.CancelledError:
                await call
                raise

    async def _checkpoint(
        self,
        *,
        worker: Principal,
        lease: AgentRunLease,
        result: AgentRunResult,
        checkpoint: dict,
        target: DurableRunStatus,
        pending_call_id: str | None = None,
        pending_approval_id: str | None = None,
        failure_code: str | None = None,
    ) -> DurableAgentRun:
        return await asyncio.to_thread(
            self.service.checkpoint,
            worker=worker,
            run_id=lease.run.run_id,
            lease_token=lease.lease_token,
            target=target,
            checkpoint=checkpoint,
            turns=result.turns,
            tool_calls=result.tool_calls,
            total_tokens=result.total_tokens,
            model_cost_microusd=result.model_cost_microusd,
            pending_call_id=pending_call_id,
            pending_approval_id=pending_approval_id,
            failure_code=failure_code,
            applied_control_sequences=result.applied_control_sequences,
        )

    def _retry_delay(self, lease: AgentRunLease) -> int:
        exponential = min(
            self.retry_cap_seconds,
            self.retry_base_seconds * (2**lease.run.failure_count),
        )
        digest = hashlib.sha256(lease.run.run_id.encode()).digest()
        jitter_percent = (digest[0] % 41) - 20
        return max(1, exponential + (exponential * jitter_percent // 100))

    @staticmethod
    def _pending_approval(result: AgentRunResult) -> tuple[str, str]:
        for event in reversed(result.events):
            if event.event_type != "agent.awaiting_approval":
                continue
            call_id = event.data.get("call_id")
            approval_id = event.data.get("approval_id")
            if (
                isinstance(call_id, str)
                and call_id
                and isinstance(approval_id, str)
                and approval_id
            ):
                return call_id, approval_id
        raise ValueError("awaiting-approval result lacks a durable approval binding")

    @staticmethod
    def _result_error_code(result: AgentRunResult) -> str:
        for event in reversed(result.events):
            if event.event_type != "agent.failed":
                continue
            value = event.data.get("error_code")
            if isinstance(value, str) and value in {
                "turn_budget_exceeded",
                "tool_call_budget_exceeded",
                "token_budget_exceeded",
                "model_cost_budget_exceeded",
                "model_output_truncated",
            }:
                return value
        if result.status == "denied":
            return "policy_denied"
        return "runtime_invalid_result"


class DurableAgentWorkerRunner:
    """Supervises a worker with bounded idle and identity-provider backoff."""

    def __init__(
        self,
        *,
        worker: DurableAgentWorker,
        identity_provider: WorkerIdentityProvider,
        idle_poll_seconds: float = 2.0,
        identity_backoff_base_seconds: float = 1.0,
        identity_backoff_cap_seconds: float = 60.0,
        random_source: random.Random | None = None,
    ) -> None:
        if not 0.05 <= idle_poll_seconds <= 60:
            raise ValueError("worker idle poll interval is invalid")
        if not 0.1 <= identity_backoff_base_seconds <= identity_backoff_cap_seconds <= 600:
            raise ValueError("worker identity backoff is invalid")
        self.worker = worker
        self.identity_provider = identity_provider
        self.idle_poll_seconds = idle_poll_seconds
        self.identity_backoff_base_seconds = identity_backoff_base_seconds
        self.identity_backoff_cap_seconds = identity_backoff_cap_seconds
        self.random = random_source or random.SystemRandom()

    async def run(self, *, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                principal = await self.identity_provider.resolve()
                failures = 0
                outcome = await self.worker.process_once(worker=principal)
            except asyncio.CancelledError:
                raise
            except (AuthenticationError, IdentityProviderUnavailable):
                failures += 1
                await self._wait(stop, self._identity_delay(failures))
                continue
            if outcome.status is AgentWorkerOutcomeStatus.IDLE:
                await self._wait(stop, self.idle_poll_seconds)

    def _identity_delay(self, failures: int) -> float:
        value = min(
            self.identity_backoff_cap_seconds,
            self.identity_backoff_base_seconds * (2 ** min(failures - 1, 16)),
        )
        return value * self.random.uniform(0.8, 1.2)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            return
