from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..security import Principal
from ..tools import ToolRegistry
from .repository import SQLAlchemyToolJobRepository, ToolJobError

logger = logging.getLogger("coifesp.tool_worker")


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    tenant_id: str
    job_id: str
    run_id: str
    call_id: str
    idempotency_key: str
    worker_id: str | None = None
    lease_token: str | None = None


_CONTEXT: contextvars.ContextVar[ToolExecutionContext | None] = contextvars.ContextVar(
    "coifesp_tool_execution_context", default=None
)


def current_tool_execution_context() -> ToolExecutionContext:
    value = _CONTEXT.get()
    if value is None:
        raise RuntimeError("tool execution context is unavailable")
    return value


class RetryableToolError(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class PermanentToolError(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class ToolBatchReconciler(Protocol):
    def reconcile(self, *, tenant_id: str, actor_id: str, limit: int = 100) -> int: ...


class ToolWorkerIdentityProvider(Protocol):
    async def resolve(self) -> Principal: ...


class ToolWorkspaceManager(Protocol):
    def prepare(self, *, tenant_id: str, job_id: str) -> Any: ...


class DurableToolWorker:
    """Consumes authorized jobs; connectors use the context idempotency key."""

    def __init__(
        self,
        *,
        repository: SQLAlchemyToolJobRepository,
        registry: ToolRegistry,
        tenant_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        heartbeat_seconds: float = 20,
        retry_delay_seconds: int = 30,
        reconciler: ToolBatchReconciler | None = None,
        workspace_manager: ToolWorkspaceManager | None = None,
    ) -> None:
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("tool worker lease duration is invalid")
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("tool worker heartbeat must be shorter than its lease")
        if not 1 <= retry_delay_seconds <= 3600:
            raise ValueError("tool worker retry delay is invalid")
        self.repository = repository
        self.registry = registry
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.reconciler = reconciler
        self.workspace_manager = workspace_manager

    async def run_once(self) -> bool:
        if self.reconciler is not None:
            self.reconciler.reconcile(tenant_id=self.tenant_id, actor_id=self.worker_id, limit=100)
        self.repository.recover_expired(
            tenant_id=self.tenant_id,
            actor_id=self.worker_id,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        lease = self.repository.claim_next(
            tenant_id=self.tenant_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return False
        try:
            return await self._run_leased_job(lease)
        except ToolJobError:
            # Preparation/validation may outlive the claim. A stale worker
            # must not execute or terminate the loop; recovery owns this job.
            logger.info("tool job lease changed before execution job_id=%s", lease.job.job_id)
            return True

    async def _run_leased_job(self, lease) -> bool:
        job = lease.job
        if self.workspace_manager is not None:
            try:
                self.workspace_manager.prepare(tenant_id=job.tenant_id, job_id=job.job_id)
            except (OSError, ValueError):
                self._start(lease)
                self._fail(
                    job.job_id,
                    lease.lease_token,
                    "workspace_preparation_failed",
                    retryable=False,
                )
                return True
        definition = self.registry.get(job.tool_name)
        if definition is None:
            self._start(lease)
            self._fail(job.job_id, lease.lease_token, "unknown_tool", retryable=False)
            return True

        try:
            Draft202012Validator(definition.parameters_schema).validate(job.arguments)
            if definition.validator is not None:
                assert job.arguments is not None
                definition.validator(job.arguments)
        except (ValidationError, ValueError, TypeError):
            self._start(lease)
            self._fail(job.job_id, lease.lease_token, "invalid_arguments", retryable=False)
            return True

        self._start(lease)
        heartbeat_failed = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(job.job_id, lease.lease_token, heartbeat_failed)
        )
        token = _CONTEXT.set(
            ToolExecutionContext(
                job.tenant_id,
                job.job_id,
                job.run_id,
                job.call_id,
                job.idempotency_key,
                self.worker_id,
                lease.lease_token,
            )
        )
        try:
            assert job.arguments is not None
            execution = asyncio.create_task(definition.handler(job.arguments))
            lease_loss = asyncio.create_task(heartbeat_failed.wait())
            done, _ = await asyncio.wait(
                {execution, lease_loss},
                timeout=definition.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution in done:
                lease_loss.cancel()
                result = self._limit_output(execution.result(), definition.max_output_chars)
                self.repository.succeed(
                    tenant_id=self.tenant_id,
                    job_id=job.job_id,
                    worker_id=self.worker_id,
                    lease_token=lease.lease_token,
                    result=result,
                )
            elif lease_loss in done:
                execution.cancel()
                await self._discard_cancel(execution)
                logger.error("tool execution stopped after lease loss job_id=%s", job.job_id)
            else:
                execution.cancel()
                lease_loss.cancel()
                await self._discard_cancel(execution)
                self._fail(job.job_id, lease.lease_token, "tool_timeout", retryable=True)
        except RetryableToolError as exc:
            self._fail(job.job_id, lease.lease_token, exc.error_code, retryable=True)
        except PermanentToolError as exc:
            self._fail(job.job_id, lease.lease_token, exc.error_code, retryable=False)
        except ToolJobError:
            logger.exception("tool job state update failed job_id=%s", job.job_id)
        except Exception:
            # Provider exception text may contain confidential response data.
            logger.exception("tool handler failed job_id=%s", job.job_id)
            self._fail(job.job_id, lease.lease_token, "handler_error", retryable=False)
        finally:
            _CONTEXT.reset(token)
            heartbeat.cancel()
            await self._discard_cancel(heartbeat)
        if self.reconciler is not None:
            self.reconciler.reconcile(tenant_id=self.tenant_id, actor_id=self.worker_id, limit=100)
        return True

    async def run(self, *, stop: asyncio.Event, idle_poll_seconds: float = 2.0) -> None:
        if not 0.05 <= idle_poll_seconds <= 60:
            raise ValueError("tool worker idle poll is invalid")
        while not stop.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_poll_seconds)
                except TimeoutError:
                    pass

    def _start(self, lease) -> None:
        self.repository.start(
            tenant_id=self.tenant_id,
            job_id=lease.job.job_id,
            worker_id=self.worker_id,
            lease_token=lease.lease_token,
        )

    async def _heartbeat(self, job_id: str, lease_token: str, failed: asyncio.Event) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                self.repository.heartbeat(
                    tenant_id=self.tenant_id,
                    job_id=job_id,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                    lease_seconds=self.lease_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any heartbeat failure revokes execution
            failed.set()

    def _fail(self, job_id: str, lease_token: str, error_code: str, *, retryable: bool) -> None:
        try:
            self.repository.fail(
                tenant_id=self.tenant_id,
                job_id=job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                error_code=error_code,
                retryable=retryable,
                retry_delay_seconds=self.retry_delay_seconds,
            )
        except ToolJobError:
            logger.exception("tool failure could not be persisted job_id=%s", job_id)

    @staticmethod
    async def _discard_cancel(task: asyncio.Task[Any]) -> None:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - drain already handled task
            pass

    @staticmethod
    def _limit_output(output: Any, limit: int) -> Any:
        from ..tools.executor import ToolExecutor

        return ToolExecutor._limit_output(output, limit)


class DurableToolWorkerRunner:
    """Revalidates the dedicated service token before every claim cycle."""

    def __init__(
        self,
        *,
        repository: SQLAlchemyToolJobRepository,
        registry: ToolRegistry,
        reconciler: ToolBatchReconciler,
        identity_provider: ToolWorkerIdentityProvider,
        tenant_id: str,
        idle_poll_seconds: float = 2.0,
        lease_seconds: int = 60,
        heartbeat_seconds: float = 20,
        workspace_manager: ToolWorkspaceManager | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.reconciler = reconciler
        self.identity_provider = identity_provider
        self.tenant_id = tenant_id
        self.idle_poll_seconds = idle_poll_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.workspace_manager = workspace_manager

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            principal = await self.identity_provider.resolve()
            if (
                not principal.is_service
                or principal.tenant_id != self.tenant_id
                or "tool_worker" not in principal.roles
            ):
                raise ToolJobError("Tool Worker identity is invalid")
            worker = DurableToolWorker(
                repository=self.repository,
                registry=self.registry,
                tenant_id=self.tenant_id,
                worker_id=principal.principal_id,
                lease_seconds=self.lease_seconds,
                heartbeat_seconds=self.heartbeat_seconds,
                reconciler=self.reconciler,
                workspace_manager=self.workspace_manager,
            )
            if not await worker.run_once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.idle_poll_seconds)
                except TimeoutError:
                    pass
