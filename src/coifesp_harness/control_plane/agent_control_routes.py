from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from ..agent_runs import (
    AgentControlCommand,
    AgentControlType,
    AgentRunPersistenceError,
    DurableAgentEvent,
    DurableAgentRun,
)
from ..errors import (
    AuthenticationError,
    HarnessError,
    IdempotencyConflict,
    PolicyDenied,
    ResourceNotFound,
)
from .agent_run_models import AgentRunResponse
from .auth import BearerAuthenticator

logger = logging.getLogger("coifesp.control_plane.agent_control")

CONTROL_SUBPROTOCOL = "coifesp.control.v1"
MAX_CONTROL_FRAME_BYTES = 70_000
MAX_COMMAND_BATCH = 100
MAX_EVENT_BATCH = 500
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def build_agent_control_router(*, authenticator: BearerAuthenticator) -> APIRouter:
    router = APIRouter(tags=["agent-runs"])

    @router.websocket("/v1/agent-runs/{run_id}/control")
    async def control(websocket: WebSocket, run_id: str) -> None:
        if websocket.scope.get("subprotocols", []).count(CONTROL_SUBPROTOCOL) != 1:
            await websocket.close(code=1002, reason="required subprotocol is absent")
            return
        try:
            authenticated = await authenticator(websocket)
            service = _service(websocket)
            run = await run_in_threadpool(
                service.get,
                principal=authenticated.principal,
                run_id=run_id,
            )
        except AuthenticationError:
            await websocket.close(code=1008, reason="authentication failed")
            return
        except (PolicyDenied, ResourceNotFound):
            await websocket.close(code=1008, reason="agent run is unavailable")
            return
        except HarnessError:
            await websocket.close(code=1011, reason="control service unavailable")
            return

        await websocket.accept(subprotocol=CONTROL_SUBPROTOCOL)
        await websocket.send_json(
            {
                "type": "control.ready",
                "protocol": CONTROL_SUBPROTOCOL,
                "run": _run_json(run),
            }
        )
        event_cursor = 0
        idle_ticks = 0
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(websocket.receive(), timeout=1.0)
                except TimeoutError:
                    event_cursor = await _push_events(
                        websocket=websocket,
                        service=service,
                        principal=authenticated.principal,
                        run_id=run_id,
                        after_sequence=event_cursor,
                    )
                    idle_ticks += 1
                    if idle_ticks % 15 == 0:
                        await websocket.send_json({"type": "control.keepalive"})
                    continue
                idle_ticks = 0
                if frame["type"] == "websocket.disconnect":
                    return
                raw = frame.get("text")
                if raw is None:
                    await websocket.close(code=1003, reason="binary frames are unsupported")
                    return
                if len(raw.encode("utf-8")) > MAX_CONTROL_FRAME_BYTES:
                    await websocket.close(code=1009, reason="control frame is too large")
                    return
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.close(code=1007, reason="control frame is not JSON")
                    return
                if not isinstance(message, dict):
                    await websocket.close(code=1008, reason="control message must be an object")
                    return
                message_type = message.get("type")
                if message_type == "command.submit":
                    await _submit(
                        websocket=websocket,
                        service=service,
                        principal=authenticated.principal,
                        run_id=run_id,
                        message=message,
                    )
                elif message_type == "session.resume":
                    event_cursor = await _resume(
                        websocket=websocket,
                        service=service,
                        principal=authenticated.principal,
                        run_id=run_id,
                        message=message,
                        current_event_cursor=event_cursor,
                    )
                elif message_type == "ping" and set(message) == {"type"}:
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.close(code=1008, reason="unsupported control message")
                    return
        except WebSocketDisconnect:
            return
        except Exception:
            logger.exception("agent control WebSocket failed", extra={"run_id": run_id})
            try:
                await websocket.close(code=1011, reason="control stream failed")
            except RuntimeError:
                pass

    return router


async def _submit(*, websocket, service, principal, run_id: str, message: dict) -> None:
    if set(message) != {
        "type",
        "command_id",
        "command_type",
        "content",
        "expected_run_version",
    }:
        await _error(websocket, "invalid_message")
        return
    command_id = message.get("command_id")
    content = message.get("content")
    expected_version = message.get("expected_run_version")
    if (
        not _identifier(command_id)
        or not isinstance(content, str)
        or not content
        or len(content.encode("utf-8")) > 65_536
        or isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version <= 0
    ):
        await _error(websocket, "invalid_message")
        return
    try:
        command_type = AgentControlType(message.get("command_type"))
    except (TypeError, ValueError):
        await _error(websocket, "invalid_message")
        return
    try:
        command = await run_in_threadpool(
            service.submit_control,
            principal=principal,
            run_id=run_id,
            command_id=command_id,
            command_type=command_type,
            content=content,
            expected_run_version=expected_version,
        )
        run = await run_in_threadpool(service.get, principal=principal, run_id=run_id)
    except IdempotencyConflict:
        await _error(websocket, "idempotency_conflict", command_id=command_id)
        return
    except AgentRunPersistenceError:
        await _error(websocket, "state_conflict", command_id=command_id)
        return
    except (PolicyDenied, ResourceNotFound):
        await _error(websocket, "forbidden", command_id=command_id)
        return
    await websocket.send_json(
        {
            "type": "command.accepted",
            "command": _command_json(command, include_content=False),
            "run": _run_json(run),
        }
    )


async def _resume(
    *, websocket, service, principal, run_id: str, message: dict, current_event_cursor: int
) -> int:
    if set(message) != {"type", "after_command_sequence", "after_event_sequence"}:
        await _error(websocket, "invalid_message")
        return current_event_cursor
    command_cursor = message.get("after_command_sequence")
    event_cursor = message.get("after_event_sequence")
    if not _cursor(command_cursor) or not _cursor(event_cursor):
        await _error(websocket, "invalid_message")
        return current_event_cursor
    try:
        commands = await run_in_threadpool(
            service.list_control,
            principal=principal,
            run_id=run_id,
            after_sequence=command_cursor,
            limit=MAX_COMMAND_BATCH,
        )
        events = await run_in_threadpool(
            service.events,
            principal=principal,
            run_id=run_id,
            after_sequence=event_cursor,
            limit=MAX_EVENT_BATCH,
        )
        run = await run_in_threadpool(service.get, principal=principal, run_id=run_id)
    except (PolicyDenied, ResourceNotFound):
        await _error(websocket, "forbidden")
        return event_cursor
    await websocket.send_json(
        {
            "type": "session.snapshot",
            "commands": [_command_json(item, include_content=True) for item in commands],
            "events": [_event_json(item) for item in events],
            "run": _run_json(run),
            "has_more_commands": len(commands) == MAX_COMMAND_BATCH,
            "has_more_events": len(events) == MAX_EVENT_BATCH,
        }
    )
    return events[-1].sequence if events else event_cursor


async def _push_events(*, websocket, service, principal, run_id: str, after_sequence: int) -> int:
    events = await run_in_threadpool(
        service.events,
        principal=principal,
        run_id=run_id,
        after_sequence=after_sequence,
        limit=MAX_EVENT_BATCH,
    )
    if not events:
        return after_sequence
    await websocket.send_json(
        {
            "type": "run.events",
            "events": [_event_json(item) for item in events],
            "has_more": len(events) == MAX_EVENT_BATCH,
        }
    )
    return events[-1].sequence


async def _error(websocket, code: str, *, command_id: str | None = None) -> None:
    value: dict[str, Any] = {"type": "control.error", "code": code}
    if command_id is not None:
        value["command_id"] = command_id
    await websocket.send_json(value)


def _command_json(command: AgentControlCommand, *, include_content: bool) -> dict[str, Any]:
    value: dict[str, Any] = {
        "run_id": command.run_id,
        "sequence": command.sequence,
        "command_id": command.command_id,
        "command_type": command.command_type.value,
        "status": command.status.value,
        "submitted_by": command.submitted_by,
        "created_at": command.created_at.isoformat(),
        "applied_at": command.applied_at.isoformat() if command.applied_at else None,
        "applied_run_version": command.applied_run_version,
        "rejected_at": command.rejected_at.isoformat() if command.rejected_at else None,
        "rejection_code": command.rejection_code,
    }
    if include_content:
        value["content"] = command.content
    return value


def _event_json(event: DurableAgentEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "data": event.data,
        "occurred_at": event.occurred_at.isoformat(),
    }


def _run_json(run: DurableAgentRun) -> dict[str, Any]:
    return AgentRunResponse.model_validate(run, from_attributes=True).model_dump(mode="json")


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER_PATTERN.fullmatch(value) is not None


def _cursor(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _service(websocket: WebSocket):
    service = getattr(websocket.app.state, "agent_run_service", None)
    if service is None:
        raise AgentRunPersistenceError("agent run service is not configured")
    return service
