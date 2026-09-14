import asyncio

from fastapi import FastAPI

from coifesp_harness.control_plane.app import (
    _replay_terminal_projections,
    _run_terminal_projection_reconciler,
)


class Projection:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def replay_pending(self, service):
        self.calls.append(service)
        if self.fail:
            raise RuntimeError("deferred projection failure")


class SpecialistProjection:
    def __init__(self):
        self.calls = 0

    def replay_all_tenants(self):
        self.calls += 1


def test_periodic_projection_reconciler_observes_external_worker_completion():
    async def scenario():
        app = FastAPI()
        service = object()
        conversation = Projection()
        planner = Projection(fail=True)
        accounting = Projection()
        specialist = SpecialistProjection()
        app.state.agent_run_service = service
        app.state.turn_projection = conversation
        app.state.project_planner_projection = planner
        app.state.team_task_result_projection = accounting
        app.state.specialist_run_projection = specialist
        stop = asyncio.Event()
        task = asyncio.create_task(
            _run_terminal_projection_reconciler(
                app,
                stop=stop,
                interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.035)
        stop.set()
        await task
        assert len(conversation.calls) >= 2
        assert all(item is service for item in conversation.calls)
        assert len(accounting.calls) == len(conversation.calls)
        assert len(planner.calls) == len(conversation.calls)
        assert specialist.calls == len(conversation.calls)

    asyncio.run(scenario())


def test_startup_projection_replay_remains_fail_closed():
    async def scenario():
        app = FastAPI()
        app.state.agent_run_service = object()
        app.state.turn_projection = Projection(fail=True)
        try:
            await _replay_terminal_projections(app, tolerate_errors=False)
        except RuntimeError as exc:
            assert str(exc) == "deferred projection failure"
        else:
            raise AssertionError("startup must reject an unrecoverable projection failure")

    asyncio.run(scenario())
