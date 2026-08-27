import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.security import PolicyEngine, Principal, RiskLevel
from coifesp_harness.tools import (
    Approval,
    ExecutionStatus,
    ToolDefinition,
    ToolExecutionRequest,
    ToolExecutor,
    ToolRegistry,
)


def digest(tool: str, arguments: dict) -> str:
    value = json.dumps(
        {"tool": tool, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def test_high_risk_tool_requires_request_bound_approval() -> None:
    registry = ToolRegistry()

    async def handler(arguments: dict) -> str:
        return f"sent:{arguments['value']}"

    registry.register(
        ToolDefinition(
            name="send_external",
            description="external side effect",
            handler=handler,
            parameters_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            required_roles=frozenset({"operator"}),
            risk=RiskLevel.HIGH,
        )
    )
    executor = ToolExecutor(
        registry=registry,
        policy=PolicyEngine(),
        audit=InMemoryAuditSink(),
        idempotency=InMemoryIdempotencyStore(),
    )
    principal = Principal("alice", "team-a", roles=frozenset({"operator"}))
    base = dict(
        execution_id="exec-1",
        idempotency_key="idem-1",
        correlation_id="corr-1",
        principal=principal,
        tool_name="send_external",
        arguments={"value": "hello"},
    )
    gated = asyncio.run(executor.execute(ToolExecutionRequest(**base)))
    assert gated.status is ExecutionStatus.APPROVAL_REQUIRED

    approval = Approval(
        approval_id="approval-1",
        tenant_id="team-a",
        principal_id="alice",
        tool_name="send_external",
        request_digest=digest("send_external", {"value": "hello"}),
        approved_by="reviewer",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    result = asyncio.run(executor.execute(ToolExecutionRequest(**base, approval=approval)))
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output == "sent:hello"

    duplicate = asyncio.run(executor.execute(ToolExecutionRequest(**base, approval=approval)))
    assert duplicate.status is ExecutionStatus.DUPLICATE_SUPPRESSED


def test_timeout_is_enforced() -> None:
    registry = ToolRegistry()

    async def slow(_: dict) -> str:
        await asyncio.sleep(0.05)
        return "late"

    registry.register(
        ToolDefinition(
            name="slow",
            description="slow tool",
            handler=slow,
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            timeout_seconds=0.001,
        )
    )
    executor = ToolExecutor(
        registry=registry,
        policy=PolicyEngine(),
        audit=InMemoryAuditSink(),
        idempotency=InMemoryIdempotencyStore(),
    )
    result = asyncio.run(
        executor.execute(
            ToolExecutionRequest(
                execution_id="exec-2",
                idempotency_key="idem-2",
                correlation_id="corr-2",
                principal=Principal("alice", "team-a"),
                tool_name="slow",
                arguments={},
            )
        )
    )
    assert result.status is ExecutionStatus.TIMED_OUT


def test_schema_validation_does_not_echo_sensitive_argument_values() -> None:
    registry = ToolRegistry()
    calls = []

    async def handler(arguments: dict) -> str:
        calls.append(arguments)
        return "unexpected"

    registry.register(
        ToolDefinition(
            name="typed_tool",
            description="requires an integer",
            handler=handler,
            parameters_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
    )
    executor = ToolExecutor(
        registry=registry,
        policy=PolicyEngine(),
        audit=InMemoryAuditSink(),
        idempotency=InMemoryIdempotencyStore(),
    )
    secret_value = "do-not-leak-this-value"
    result = asyncio.run(
        executor.execute(
            ToolExecutionRequest(
                execution_id="exec-schema",
                idempotency_key="idem-schema",
                correlation_id="corr-schema",
                principal=Principal("alice", "team-a"),
                tool_name="typed_tool",
                arguments={"value": secret_value},
            )
        )
    )
    assert result.status is ExecutionStatus.FAILED
    assert secret_value not in (result.error or "")
    assert calls == []


def test_structured_output_is_bounded() -> None:
    registry = ToolRegistry()

    async def handler(_: dict) -> dict:
        return {"items": ["sensitive-value"] * 100}

    registry.register(
        ToolDefinition(
            name="structured",
            description="large structured output",
            handler=handler,
            parameters_schema={"type": "object", "additionalProperties": False},
            max_output_chars=80,
        )
    )
    executor = ToolExecutor(
        registry=registry,
        policy=PolicyEngine(),
        audit=InMemoryAuditSink(),
        idempotency=InMemoryIdempotencyStore(),
    )
    result = asyncio.run(
        executor.execute(
            ToolExecutionRequest(
                execution_id="exec-output-limit",
                idempotency_key="idem-output-limit",
                correlation_id="corr-output-limit",
                principal=Principal("alice", "team-a"),
                tool_name="structured",
                arguments={},
            )
        )
    )
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output["truncated"] is True
    assert len(result.output["preview"]) == 80
    assert result.output["original_chars"] > 80
