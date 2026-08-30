"""Non-blocking service loop for the durable, fenced project Runner."""

import asyncio
import logging

from .runner import ProjectOrchestratorWorkerStatus

logger = logging.getLogger("coifesp.project_orchestrator.worker")


class ProjectOrchestratorLoop:
    def __init__(self, runner, *, worker_id, idle_poll_seconds=1.0, lease_seconds=60):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("orchestrator worker identity is required")
        if isinstance(idle_poll_seconds, bool) or not 0.05 <= idle_poll_seconds <= 60:
            raise ValueError("orchestrator poll interval is invalid")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("orchestrator lease duration is invalid")
        self.runner, self.worker_id = runner, worker_id
        self.idle_poll_seconds, self.lease_seconds = idle_poll_seconds, lease_seconds

    async def run(self, *, stop: asyncio.Event):
        while not stop.is_set():
            operation = asyncio.create_task(asyncio.to_thread(
                self.runner.process_once, worker_id=self.worker_id, lease_seconds=self.lease_seconds))
            try:
                outcome = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop its database transaction.
                # Drain it before application shutdown closes the shared engine.
                try:
                    await operation
                except Exception as exc:  # noqa: BLE001 - cancellation still propagates
                    logger.warning("orchestration shutdown deferred error_type=%s", type(exc).__name__)
                raise
            except Exception as exc:  # noqa: BLE001 - claim/fence/database recovery on next poll
                logger.warning("orchestration poll deferred error_type=%s", type(exc).__name__)
                await self._wait(stop)
                continue
            if outcome.status in {ProjectOrchestratorWorkerStatus.IDLE,
                                  ProjectOrchestratorWorkerStatus.RETRY,
                                  ProjectOrchestratorWorkerStatus.FAILED}:
                await self._wait(stop)

    async def _wait(self, stop):
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.idle_poll_seconds)
        except TimeoutError:
            pass
