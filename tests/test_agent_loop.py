import asyncio

from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.context import (
    ContentTrust,
    ContextBudget,
    ContextItem,
    ContextSource,
)
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.runtime import (
    AgentLoop,
    AgentRunRequest,
    LLMResponse,
    Message,
    ToolCall,
)
from coifesp_harness.security import (
    Classification,
    PolicyEngine,
    Principal,
    ResourceLabel,
)
from coifesp_harness.tools import ToolDefinition, ToolExecutor, ToolRegistry


class ScriptedProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    async def complete(self, *, messages, tools, correlation_id):
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            return LLMResponse(
                tool_calls=(ToolCall("call-1", "echo", {"value": "hello"}),),
                input_tokens=10,
                output_tokens=5,
            )
        return LLMResponse(text="done", input_tokens=10, output_tokens=2)


def test_loop_routes_all_actions_through_executor() -> None:
    registry = ToolRegistry()

    async def echo(arguments: dict) -> str:
        return arguments["value"]

    registry.register(
        ToolDefinition(
            name="echo",
            description="echo value",
            handler=echo,
            parameters_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
    )
    audit = InMemoryAuditSink()
    executor = ToolExecutor(
        registry=registry,
        policy=PolicyEngine(),
        audit=audit,
        idempotency=InMemoryIdempotencyStore(),
    )
    provider = ScriptedProvider()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        executor=executor,
        audit=audit,
    )
    result = asyncio.run(
        loop.run(
            AgentRunRequest(
                run_id="run-1",
                correlation_id="corr-1",
                principal=Principal("alice", "team-a"),
                messages=(Message("user", "echo hello"),),
                context_items=(
                    ContextItem(
                        item_id="memory-1",
                        content="Reference value only",
                        source=ContextSource.MEMORY,
                        source_id="memory:1",
                        label=ResourceLabel(
                            "team-a",
                            Classification.INTERNAL,
                            resource_id="memory:memory-1",
                        ),
                        content_trust=ContentTrust.VERIFIED,
                    ),
                ),
                context_budget=ContextBudget(
                    max_input_tokens=2_000,
                    reserved_output_tokens=200,
                ),
            )
        )
    )
    assert result.status == "completed"
    assert result.turns == 2
    assert result.tool_calls == 1
    assert any(event.event_type == "tool.completed" for event in result.events)
    assert any(event.event_type == "context.assembled" for event in result.events)
    assert "Reference value only" in provider.messages[0][-1].content
    assert provider.messages[0][-1].role == "user"
    assert '"schema":"coifesp.tool-output.v1"' in provider.messages[1][-2].content
