import asyncio
import base64
from types import SimpleNamespace

import pytest

from coifesp_harness.agent_runs import AgentRunCheckpointCodec
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.context import ContextItem, ContextSource
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.runtime import (
    AgentLoop,
    AgentRunRequest,
    LLMResponse,
    Message,
    MessageImage,
    ModelCapability,
    ModelRoutePolicy,
    ModelStreamEvent,
    ModelStreamEventType,
    RunBudget,
    ToolCall,
    ToolSpec,
)
from coifesp_harness.runtime.providers import (
    AnthropicProvider,
    ModelGateway,
    ModelRoutingError,
    OpenAICompatibleProvider,
    ProviderDescriptor,
    ProviderFailureKind,
    ProviderInvocationError,
    ProviderRegistration,
    TiktokenCounter,
)
from coifesp_harness.security import (
    Classification,
    PolicyEngine,
    Principal,
    ResourceLabel,
)
from coifesp_harness.tools import ToolDefinition, ToolExecutor, ToolRegistry


class Provider:
    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.error = error
        self.calls = 0
        self.last_request = None

    async def complete(self, **request):
        self.calls += 1
        self.last_request = request
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class StreamingProvider(Provider):
    def __init__(self, events=(), error=None, *, error_after_delta=False):
        super().__init__()
        self.events = list(events)
        self.stream_error = error
        self.error_after_delta = error_after_delta

    async def stream(self, **request):
        self.calls += 1
        self.last_request = request
        if self.stream_error is not None and not self.error_after_delta:
            raise self.stream_error
        for event in self.events:
            yield event
        if self.stream_error is not None:
            raise self.stream_error


def descriptor(
    provider_id,
    *,
    external=False,
    priority=100,
    capabilities=frozenset({ModelCapability.TOOL_CALLING}),
    maximum=Classification.INTERNAL,
    region="local",
    output_rate=1_000_000,
    max_output=16,
):
    return ProviderDescriptor(
        provider_id=provider_id,
        model=f"{provider_id}-model",
        capabilities=capabilities,
        max_data_classification=maximum,
        region=region,
        external=external,
        context_window_tokens=10_000,
        max_output_tokens=max_output,
        input_microusd_per_million_tokens=0,
        output_microusd_per_million_tokens=output_rate,
        priority=priority,
        max_concurrency=2,
    )


def registration(provider_id, provider, **kwargs):
    return ProviderRegistration(descriptor(provider_id, **kwargs), provider)


def run(coro):
    return asyncio.run(coro)


async def collect(stream):
    return [event async for event in stream]


def test_provider_bound_tokenizer_controls_route_budget_without_remote_calls():
    provider = Provider([LLMResponse(text="ok", input_tokens=5, output_tokens=1)])
    counter = TiktokenCounter("cl100k_base")
    gateway = ModelGateway((ProviderRegistration(descriptor("exact", max_output=8),
        provider, token_counter=counter),))
    messages = (Message("user", "hello world"),)
    exact = counter.count(messages=messages, tools=())
    response = run(gateway.complete_routed(messages=messages, tools=(),
        correlation_id="exact-ok", route_policy=ModelRoutePolicy(
            max_call_total_tokens=exact + 1, max_output_tokens=1)))
    assert response.text == "ok" and provider.calls == 1

    denied_provider = Provider([LLMResponse(text="must-not-run")])
    denied = ModelGateway((ProviderRegistration(descriptor("exact-denied", max_output=8),
        denied_provider, token_counter=counter),))
    with pytest.raises(ModelRoutingError):
        run(denied.complete_routed(messages=messages, tools=(),
            correlation_id="exact-denied", route_policy=ModelRoutePolicy(
                max_call_total_tokens=exact)))
    assert denied_provider.calls == 0


def test_context_counter_uses_safe_upper_bound_across_failover_candidates():
    class Counter:
        exact = True
        tokenizer_id = "test:large"
        def count_text(self, text): return len(text) * 2
        def count(self, *, messages, tools): return sum(len(item.content) * 2 for item in messages) + 1
    gateway = ModelGateway((ProviderRegistration(descriptor("small"), Provider(),
        token_counter=TiktokenCounter("cl100k_base")),
        ProviderRegistration(descriptor("large"), Provider(), token_counter=Counter())))
    counter = gateway.prepare_context_counter(route_policy=ModelRoutePolicy(), tools=())
    assert counter.count_text("abcdefghij") == 20
    assert counter.count_messages((Message("user", "abcdefghij"),)) >= 21


def test_gateway_enforces_egress_classification_residency_capability_and_cost():
    external = Provider([LLMResponse(text="external", input_tokens=2, output_tokens=2)])
    internal = Provider([LLMResponse(text="internal", input_tokens=2, output_tokens=3)])
    gateway = ModelGateway(
        (
            registration(
                "external",
                external,
                external=True,
                priority=1,
                maximum=Classification.CONFIDENTIAL,
                region="cn-east",
            ),
            registration("internal", internal, priority=10, region="on-prem"),
        )
    )
    response = run(
        gateway.complete_routed(
            messages=(Message("user", "private plan"),),
            tools=(),
            correlation_id="corr-1",
            route_policy=ModelRoutePolicy(data_classification=Classification.INTERNAL),
        )
    )
    assert response.text == "internal"
    assert response.provider_id == "internal"
    assert response.model == "internal-model"
    assert response.cost_microusd == 3
    assert external.calls == 0

    bounded = Provider([LLMResponse(text="ok", input_tokens=2, output_tokens=2)])
    bounded_gateway = ModelGateway((registration("bounded", bounded),))
    run(
        bounded_gateway.complete_routed(
            messages=(Message("user", "short"),),
            tools=(),
            correlation_id="corr-bounded",
            route_policy=ModelRoutePolicy(
                data_classification=Classification.INTERNAL,
                max_output_tokens=2,
            ),
        )
    )
    assert bounded.last_request["max_output_tokens"] == 2

    public_external = Provider([LLMResponse(text="public", input_tokens=1, output_tokens=1)])
    public_gateway = ModelGateway(
        (registration("public-api", public_external, external=True, region="cn-east"),)
    )
    public = run(
        public_gateway.complete_routed(
            messages=(Message("user", "public text"),),
            tools=(),
            correlation_id="corr-2",
            route_policy=ModelRoutePolicy(
                data_classification=Classification.PUBLIC,
                residency_regions=frozenset({"cn-east"}),
                max_call_cost_microusd=100,
            ),
        )
    )
    assert public.provider_id == "public-api"
    with pytest.raises(ModelRoutingError):
        run(
            public_gateway.complete_routed(
                messages=(Message("user", "restricted"),),
                tools=(ToolSpec("lookup", "lookup", {"type": "object"}),),
                correlation_id="corr-3",
                route_policy=ModelRoutePolicy(
                    data_classification=Classification.RESTRICTED,
                    allow_external_egress=True,
                ),
            )
        )


def test_gateway_failover_is_bounded_and_nonretryable_errors_fail_closed():
    transient = ProviderInvocationError(
        kind=ProviderFailureKind.SERVER,
        provider_id="primary",
    )
    primary = Provider(error=transient)
    backup = Provider(
        [
            LLMResponse(text="backup-1", input_tokens=1, output_tokens=1),
            LLMResponse(text="backup-2", input_tokens=1, output_tokens=1),
        ]
    )
    gateway = ModelGateway(
        (
            registration("primary", primary, priority=1),
            registration("backup", backup, priority=2),
        ),
        circuit_failure_threshold=1,
        max_failover_attempts=1,
    )
    policy = ModelRoutePolicy(data_classification=Classification.INTERNAL)
    first = run(
        gateway.complete_routed(
            messages=(Message("user", "work"),),
            tools=(),
            correlation_id="corr-1",
            route_policy=policy,
        )
    )
    second = run(
        gateway.complete_routed(
            messages=(Message("user", "work"),),
            tools=(),
            correlation_id="corr-2",
            route_policy=policy,
        )
    )
    assert (first.text, second.text) == ("backup-1", "backup-2")
    assert primary.calls == 1

    denied = Provider(
        error=ProviderInvocationError(
            kind=ProviderFailureKind.AUTHENTICATION,
            provider_id="bad-credentials",
        )
    )
    never_called = Provider([LLMResponse(text="unsafe fallback")])
    fail_closed = ModelGateway(
        (
            registration("bad-credentials", denied, priority=1),
            registration("other", never_called, priority=2),
        )
    )
    with pytest.raises(ProviderInvocationError) as captured:
        run(
            fail_closed.complete_routed(
                messages=(Message("user", "work"),),
                tools=(),
                correlation_id="corr-3",
                route_policy=policy,
            )
        )
    assert captured.value.kind is ProviderFailureKind.AUTHENTICATION
    assert never_called.calls == 0


def test_observer_failure_never_duplicates_a_successful_model_call():
    class BrokenObserver:
        def record_model_attempt(self, **_):
            raise RuntimeError("telemetry unavailable")

    provider = Provider([LLMResponse(text="done", input_tokens=1, output_tokens=1)])
    gateway = ModelGateway(
        (registration("local", provider),),
        observer=BrokenObserver(),
    )
    response = run(
        gateway.complete_routed(
            messages=(Message("user", "work"),),
            tools=(),
            correlation_id="observer-corr",
            route_policy=ModelRoutePolicy(data_classification=Classification.INTERNAL),
        )
    )
    assert response.text == "done"
    assert provider.calls == 1


def test_stream_failover_only_occurs_before_any_text_is_emitted():
    capabilities = frozenset({ModelCapability.STREAMING})
    transient = ProviderInvocationError(
        kind=ProviderFailureKind.SERVER,
        provider_id="primary",
    )
    primary = StreamingProvider(error=transient)
    backup = StreamingProvider(
        events=(
            ModelStreamEvent(
                ModelStreamEventType.TEXT_DELTA,
                1,
                text_delta="safe ",
            ),
            ModelStreamEvent(
                ModelStreamEventType.COMPLETED,
                2,
                response=LLMResponse(
                    text="safe answer",
                    input_tokens=2,
                    output_tokens=2,
                    finish_reason="stop",
                ),
            ),
        )
    )
    gateway = ModelGateway(
        (
            registration("primary", primary, priority=1, capabilities=capabilities),
            registration("backup", backup, priority=2, capabilities=capabilities),
        ),
        max_failover_attempts=1,
    )
    events = run(
        collect(
            gateway.stream_routed(
                messages=(Message("user", "stream"),),
                tools=(),
                correlation_id="stream-corr",
                route_policy=ModelRoutePolicy(data_classification=Classification.INTERNAL),
            )
        )
    )
    assert [event.event_type for event in events] == [
        ModelStreamEventType.TEXT_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    assert events[-1].response.provider_id == "backup"
    assert primary.calls == backup.calls == 1

    partial = StreamingProvider(
        events=(
            ModelStreamEvent(ModelStreamEventType.TEXT_DELTA, 1, text_delta="partial"),
        ),
        error=transient,
        error_after_delta=True,
    )
    unused = StreamingProvider(events=backup.events)
    no_duplicate_gateway = ModelGateway(
        (
            registration("partial", partial, priority=1, capabilities=capabilities),
            registration("unused", unused, priority=2, capabilities=capabilities),
        ),
        max_failover_attempts=1,
    )

    async def consume_partial():
        values = []
        with pytest.raises(ProviderInvocationError):
            async for event in no_duplicate_gateway.stream_routed(
                messages=(Message("user", "stream"),),
                tools=(),
                correlation_id="partial-corr",
                route_policy=ModelRoutePolicy(data_classification=Classification.INTERNAL),
            ):
                values.append(event)
        return values

    partial_events = run(consume_partial())
    assert partial_events[0].text_delta == "partial"
    assert unused.calls == 0


def test_agent_loop_enforces_durable_cumulative_model_cost_budget():
    provider = Provider(
        [
            LLMResponse(
                tool_calls=(ToolCall("call-1", "echo", {"value": "one"}),),
                input_tokens=1,
                output_tokens=5,
                finish_reason="tool_calls",
            ),
            LLMResponse(text="done", input_tokens=1, output_tokens=5, finish_reason="stop"),
        ]
    )
    gateway = ModelGateway(
        (registration("local", provider, max_output=5),),
        max_failover_attempts=0,
    )
    registry = ToolRegistry()

    async def echo(arguments):
        return arguments["value"]

    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            parameters_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=echo,
        )
    )
    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=gateway,
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
        audit=audit,
    )
    result = run(
        loop.run(
            AgentRunRequest(
                run_id="cost-run",
                correlation_id="cost-corr",
                principal=Principal("lead", "team-a"),
                messages=(Message("user", "bounded work"),),
                budget=RunBudget(max_model_cost_microusd=9),
                model_route_policy=ModelRoutePolicy(
                    data_classification=Classification.INTERNAL,
                ),
            )
        )
    )
    assert result.status == "failed"
    assert result.model_cost_microusd == 5
    assert provider.calls == 1
    assert result.events[-1].data["error_code"] == "model_cost_budget_exceeded"


def test_checkpoint_preserves_route_policy_and_cost_across_recovery():
    request = AgentRunRequest(
        run_id="route-run",
        correlation_id="route-corr",
        principal=Principal("lead", "team-a"),
        messages=(Message("user", "confidential task"),),
        budget=RunBudget(max_model_cost_microusd=1234),
        model_route_policy=ModelRoutePolicy(
            data_classification=Classification.CONFIDENTIAL,
            required_capabilities=frozenset({ModelCapability.TOOL_CALLING}),
            allowed_provider_ids=frozenset({"private-provider"}),
            residency_regions=frozenset({"on-prem"}),
            max_call_cost_microusd=500,
            max_output_tokens=256,
            max_call_total_tokens=512,
        ),
    )
    codec = AgentRunCheckpointCodec()
    value = codec.decode(codec.initial(request))
    assert value["budget"].max_model_cost_microusd == 1234
    assert value["usage"].model_cost_microusd == 0
    assert value["model_route_policy"] == request.model_route_policy


def test_checkpoint_round_trips_multimodal_context_and_messages():
    encoded = base64.b64encode(b"image-bytes").decode("ascii")
    image = MessageImage("image/jpeg", encoded)
    context = ContextItem(
        item_id="ctx-image",
        content="project image reference",
        source=ContextSource.DOCUMENT,
        source_id="artifact-image",
        label=ResourceLabel("team-a", Classification.INTERNAL),
        image_media_type="image/jpeg",
        image_data_base64=encoded,
    )
    request = AgentRunRequest(
        run_id="image-run",
        correlation_id="image-corr",
        principal=Principal("lead", "team-a"),
        messages=(Message("user", "分析图片", images=(image,)),),
        context_items=(context,),
        model_route_policy=ModelRoutePolicy(
            required_capabilities=frozenset({ModelCapability.VISION})
        ),
    )

    value = AgentRunCheckpointCodec().decode(AgentRunCheckpointCodec().initial(request))

    assert value["messages"][0].images == (image,)
    assert value["context_items"][0].image_data_base64 == encoded
    assert ModelCapability.VISION in value["model_route_policy"].required_capabilities


def test_run_request_rejects_route_classification_below_context_data():
    confidential = ContextItem(
        item_id="ctx-confidential",
        content="team-only implementation detail",
        source=ContextSource.DOCUMENT,
        source_id="design-doc",
        label=ResourceLabel("team-a", Classification.CONFIDENTIAL),
    )
    with pytest.raises(ValueError, match="cannot be lower"):
        AgentRunRequest(
            run_id="classification-run",
            correlation_id="classification-corr",
            principal=Principal("lead", "team-a"),
            messages=(Message("user", "summarize"),),
            context_items=(confidential,),
            model_route_policy=ModelRoutePolicy(
                data_classification=Classification.INTERNAL,
            ),
        )


class FakeAnthropicMessages:
    def __init__(self):
        self.request = None

    async def create(self, **request):
        self.request = request
        return SimpleNamespace(
            id="anthropic-request-1",
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="text", text="checking"),
                SimpleNamespace(
                    type="tool_use",
                    id="call-2",
                    name="lookup",
                    input={"query": "status"},
                ),
            ],
            usage=SimpleNamespace(input_tokens=12, output_tokens=4),
        )


def test_anthropic_adapter_preserves_system_tool_use_and_tool_results():
    messages_api = FakeAnthropicMessages()
    provider = AnthropicProvider(
        client=SimpleNamespace(messages=messages_api),
        model="claude-model",
    )
    response = run(
        provider.complete(
            messages=(
                Message("system", "follow policy"),
                Message("user", "check"),
                Message(
                    "assistant",
                    "",
                    tool_calls=(ToolCall("call-1", "lookup", {"query": "initial"}),),
                ),
                Message("tool", '{"status":"ok"}', tool_call_id="call-1"),
            ),
            tools=(ToolSpec("lookup", "lookup", {"type": "object"}),),
            correlation_id="corr-anthropic",
        )
    )
    assert messages_api.request["system"] == "follow policy"
    assert messages_api.request["messages"][1]["content"][0]["type"] == "tool_use"
    assert messages_api.request["messages"][2]["content"][0]["type"] == "tool_result"
    assert messages_api.request["tools"][0]["input_schema"] == {"type": "object"}
    assert response.tool_calls == (ToolCall("call-2", "lookup", {"query": "status"}),)
    assert response.finish_reason == "tool_calls"
    assert response.input_tokens == 12


class RateLimitError(Exception):
    status_code = 429
    request_id = "request-rate-limit"


class FailingOpenAICompletions:
    async def create(self, **_):
        raise RateLimitError()


def test_openai_compatible_adapter_normalizes_remote_errors_without_body_leakage():
    provider = OpenAICompatibleProvider(
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=FailingOpenAICompletions())
        ),
        model="deepseek-model",
        provider_id="deepseek",
    )
    with pytest.raises(ProviderInvocationError) as captured:
        run(
            provider.complete(
                messages=(Message("user", "work"),),
                tools=(),
                correlation_id="corr-rate-limit",
            )
        )
    assert captured.value.kind is ProviderFailureKind.RATE_LIMIT
    assert captured.value.retryable
    assert captured.value.request_id == "request-rate-limit"


class FakeOpenAIStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        async def iterator():
            for chunk in self.chunks:
                yield chunk

        return iterator()

    async def close(self):
        self.closed = True


class StreamingOpenAICompletions:
    def __init__(self, stream):
        self.stream = stream
        self.request = None

    async def create(self, **request):
        self.request = request
        return self.stream


def test_openai_compatible_stream_accumulates_tool_arguments_usage_and_closes():
    stream = FakeOpenAIStream(
        [
            SimpleNamespace(
                id="stream-request",
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            content="working",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-stream",
                                    function=SimpleNamespace(
                                        name="lookup",
                                        arguments='{"query":',
                                    ),
                                )
                            ],
                        ),
                    )
                ],
            ),
            SimpleNamespace(
                id="stream-request",
                usage=SimpleNamespace(prompt_tokens=8, completion_tokens=3),
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(
                                        name=None,
                                        arguments='"status"}',
                                    ),
                                )
                            ],
                        ),
                    )
                ],
            ),
        ]
    )
    completions = StreamingOpenAICompletions(stream)
    provider = OpenAICompatibleProvider(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="deepseek-model",
        provider_id="deepseek",
    )
    events = run(
        collect(
            provider.stream(
                messages=(Message("user", "work"),),
                tools=(ToolSpec("lookup", "lookup", {"type": "object"}),),
                correlation_id="stream-openai",
            )
        )
    )
    assert completions.request["stream_options"] == {"include_usage": True}
    assert events[0].text_delta == "working"
    assert events[-1].response.tool_calls == (
        ToolCall("call-stream", "lookup", {"query": "status"}),
    )
    assert events[-1].response.input_tokens == 8
    assert stream.closed
