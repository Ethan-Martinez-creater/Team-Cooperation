import asyncio
from types import SimpleNamespace

import pytest
from test_control_plane import StubVerifier, settings

from coifesp_harness.control_plane import create_app


def test_consumer_starts_after_recovery_and_stops_before_engine_cleanup():
    async def scenario():
        events, started = [], asyncio.Event()

        class Worker:
            async def run(self, *, stop):
                events.append("worker-start")
                started.set()
                await stop.wait()
                events.append("worker-stop")

        app = create_app(settings=settings(), verifier=StubVerifier({}),
            readiness_probe=lambda: events.append("ready") or True,
            shutdown_callbacks=(lambda: events.append("engine-cleanup"),))
        app.state.agent_run_service = object()
        app.state.task_verification_service = SimpleNamespace(
            replay_pending=lambda _: events.append("recovery"))
        app.state.project_orchestrator_worker = Worker()
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), 2)
            assert events == ["ready", "recovery", "worker-start"]
        assert events == ["ready", "recovery", "worker-start", "worker-stop", "engine-cleanup"]

    asyncio.run(scenario())


def test_failed_readiness_never_starts_consumer():
    async def scenario():
        events = []

        class Worker:
            async def run(self, **kwargs):
                events.append("started")

        app = create_app(settings=settings(), verifier=StubVerifier({}), readiness_probe=lambda: False,
                         shutdown_callbacks=(lambda: events.append("cleaned"),))
        app.state.project_orchestrator_worker = Worker()
        with pytest.raises(RuntimeError, match="readiness"):
            async with app.router.lifespan_context(app):
                raise AssertionError("startup must fail")
        assert events == ["cleaned"]

    asyncio.run(scenario())


def test_consumer_failure_still_closes_application_resources():
    async def scenario():
        events = []

        class Worker:
            async def run(self, **kwargs):
                raise RuntimeError("consumer failed")

        app = create_app(settings=settings(), verifier=StubVerifier({}),
                         shutdown_callbacks=(lambda: events.append("cleaned"),))
        app.state.project_orchestrator_worker = Worker()
        with pytest.raises(RuntimeError, match="consumer failed"):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
        assert events == ["cleaned"]

    asyncio.run(scenario())
