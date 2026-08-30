import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_project_orchestrator_runner import _process, _stack

from coifesp_harness.project_process.runner import ProjectOrchestratorWorkerStatus
from coifesp_harness.project_process.worker_loop import ProjectOrchestratorLoop


def test_service_loop_consumes_real_durable_wakeups_and_stops():
    repository, _, _, runner, _ = _stack()

    async def scenario():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        actual = runner.process_once
        outcomes = []

        def observed(**kwargs):
            result = actual(**kwargs)
            outcomes.append(result)
            if len(outcomes) == 2:
                loop.call_soon_threadsafe(stop.set)
            return result

        runner.process_once = observed
        await asyncio.wait_for(ProjectOrchestratorLoop(runner, worker_id="project-loop").run(stop=stop), 10)
        assert len(outcomes) == 2
        assert all(item.status is ProjectOrchestratorWorkerStatus.APPLIED for item in outcomes)

    asyncio.run(scenario())
    assert _process(repository).phase.value == "VERIFICATION"


def test_transient_poll_error_retries_and_idle_wait_is_interruptible():
    async def scenario():
        stop = asyncio.Event()
        calls = []
        loop = asyncio.get_running_loop()

        def process_once(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("temporary database outage")
            loop.call_soon_threadsafe(stop.set)
            return SimpleNamespace(status=ProjectOrchestratorWorkerStatus.IDLE)

        await asyncio.wait_for(ProjectOrchestratorLoop(SimpleNamespace(process_once=process_once),
            worker_id="project-loop", idle_poll_seconds=.05).run(stop=stop), 2)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_cancellation_drains_current_database_operation_before_returning():
    async def scenario():
        ready, done = asyncio.Event(), threading.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def process_once(**kwargs):
            loop.call_soon_threadsafe(ready.set)
            assert release.wait(5)
            done.set()
            return SimpleNamespace(status=ProjectOrchestratorWorkerStatus.IDLE)

        worker = ProjectOrchestratorLoop(SimpleNamespace(process_once=process_once), worker_id="project-loop")
        task = asyncio.create_task(worker.run(stop=asyncio.Event()))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not done.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [{"worker_id": ""}, {"worker_id": "w", "lease_seconds": True},
                                  {"worker_id": "w", "idle_poll_seconds": 0}])
def test_invalid_worker_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ProjectOrchestratorLoop(None, **kwargs)
