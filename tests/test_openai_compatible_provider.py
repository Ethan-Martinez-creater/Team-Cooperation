import asyncio
import json
from types import SimpleNamespace

from coifesp_harness.runtime import Message, ToolCall, ToolSpec
from coifesp_harness.runtime.providers import OpenAICompatibleProvider


class FakeCompletions:
    def __init__(self) -> None:
        self.request = None

    async def create(self, **request):
        self.request = request
        return SimpleNamespace(
            id="provider-request-1",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-2",
                                function=SimpleNamespace(
                                    name="lookup",
                                    arguments=json.dumps({"query": "status"}),
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=21, completion_tokens=7),
        )


def test_provider_preserves_tool_call_sequence_and_usage() -> None:
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = OpenAICompatibleProvider(
        client=client,
        model="deepseek-v4-flash",
        max_output_tokens=256,
    )
    messages = (
        Message(role="user", content="continue"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall("call-1", "lookup", {"query": "initial"}),),
        ),
        Message(
            role="tool",
            content='{"status":"succeeded"}',
            name="lookup",
            tool_call_id="call-1",
        ),
    )
    tools = (
        ToolSpec(
            name="lookup",
            description="Look up shared status",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    )
    response = asyncio.run(
        provider.complete(messages=messages, tools=tools, correlation_id="corr-1")
    )

    assert response.tool_calls == (ToolCall("call-2", "lookup", {"query": "status"}),)
    assert response.input_tokens == 21
    assert response.output_tokens == 7
    assert response.provider_request_id == "provider-request-1"
    assert completions.request["model"] == "deepseek-v4-flash"
    assert completions.request["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert completions.request["messages"][2]["tool_call_id"] == "call-1"
    assert completions.request["tools"][0]["function"]["name"] == "lookup"
