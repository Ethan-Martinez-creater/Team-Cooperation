from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, replace

from ..models import (
    LLMResponse,
    Message,
    ModelCapability,
    ModelRoutePolicy,
    ModelStreamEvent,
    ModelStreamEventType,
    ToolSpec,
)
from .models import (
    ModelCostBudgetExceeded,
    ModelGatewayObserver,
    ModelRoutingError,
    ProviderDescriptor,
    ProviderFailureKind,
    ProviderInvocationError,
    ProviderRegistration,
)


@dataclass(slots=True)
class _ProviderState:
    registration: ProviderRegistration
    semaphore: asyncio.Semaphore
    consecutive_failures: int = 0
    opened_until: float = 0.0


@dataclass(frozen=True, slots=True)
class GatewayContextTokenCounter:
    counters: tuple[object | None, ...]

    def count_text(self, value: str) -> int:
        estimates = [max(1, math.ceil(len(value.encode("utf-8")) / 3))]
        estimates.extend(counter.count_text(value) for counter in self.counters
            if counter is not None)
        return max(estimates)

    def count_messages(self, messages: tuple[Message, ...]) -> int:
        from ...context import ConservativeTokenCounter
        estimates = [ConservativeTokenCounter().count_messages(messages)]
        for counter in self.counters:
            if counter is not None:
                estimates.append(counter.count(messages=messages, tools=()))
        return max(estimates)


class ModelGateway:
    """Policy-aware model router with bounded failover and node-local circuit breakers."""

    def prepare_context_counter(self, *, route_policy: ModelRoutePolicy,
                                tools: tuple[ToolSpec, ...]) -> GatewayContextTokenCounter:
        required = set(route_policy.required_capabilities)
        if tools: required.add(ModelCapability.TOOL_CALLING)
        counters = []
        for state in self._states.values():
            descriptor = state.registration.descriptor
            if route_policy.allowed_provider_ids and descriptor.provider_id not in route_policy.allowed_provider_ids: continue
            if not required.issubset(descriptor.capabilities): continue
            if route_policy.data_classification > descriptor.max_data_classification: continue
            if descriptor.external and route_policy.data_classification.value > 0 and not route_policy.allow_external_egress: continue
            if route_policy.residency_regions and descriptor.region not in route_policy.residency_regions: continue
            counters.append(state.registration.token_counter)
        if not counters:
            raise ModelRoutingError("no model route satisfies static policy")
        return GatewayContextTokenCounter(tuple(counters))

    def __init__(
        self,
        registrations: tuple[ProviderRegistration, ...],
        *,
        default_route_policy: ModelRoutePolicy | None = None,
        max_failover_attempts: int = 2,
        concurrency_wait_seconds: float = 2.0,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 30.0,
        observer: ModelGatewayObserver | None = None,
    ) -> None:
        if not registrations:
            raise ValueError("at least one model provider registration is required")
        identifiers = [item.descriptor.provider_id for item in registrations]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("model provider IDs must be unique")
        if not 0 <= max_failover_attempts <= 10:
            raise ValueError("model failover budget is invalid")
        if concurrency_wait_seconds <= 0 or circuit_cooldown_seconds <= 0:
            raise ValueError("model gateway timing limits are invalid")
        if not 1 <= circuit_failure_threshold <= 100:
            raise ValueError("model circuit threshold is invalid")
        self._states = {
            item.descriptor.provider_id: _ProviderState(
                registration=item,
                semaphore=asyncio.Semaphore(item.descriptor.max_concurrency),
            )
            for item in registrations
        }
        self.default_route_policy = default_route_policy or ModelRoutePolicy()
        self.max_failover_attempts = max_failover_attempts
        self.concurrency_wait_seconds = concurrency_wait_seconds
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self.observer = observer

    async def complete(
        self,
        *,
        messages,
        tools,
        correlation_id,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        route_policy = self.default_route_policy
        if max_output_tokens is not None:
            configured = route_policy.max_output_tokens
            route_policy = replace(
                route_policy,
                max_output_tokens=(
                    max_output_tokens
                    if configured is None
                    else min(max_output_tokens, configured)
                ),
            )
        return await self.complete_routed(
            messages=messages,
            tools=tools,
            correlation_id=correlation_id,
            route_policy=route_policy,
        )

    async def complete_routed(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        correlation_id: str,
        route_policy: ModelRoutePolicy,
    ) -> LLMResponse:
        input_estimate = self._estimate_input_tokens(messages=messages, tools=tools)
        candidates = self._route_candidates(
            route_policy=route_policy,
            input_tokens=input_estimate,
            tools_required=bool(tools),
            messages=messages,
            tools=tools,
        )
        last_error: ProviderInvocationError | None = None
        for state in candidates[: self.max_failover_attempts + 1]:
            descriptor = state.registration.descriptor
            provider_input_tokens = self._provider_input_tokens(state,
                messages=messages, tools=tools, conservative=input_estimate)
            output_limit = self._output_limit(
                descriptor=descriptor,
                route_policy=route_policy,
                input_tokens=provider_input_tokens,
            )
            if output_limit is None:
                continue
            acquired = False
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    state.semaphore.acquire(),
                    timeout=self.concurrency_wait_seconds,
                )
                acquired = True
                response = await state.registration.provider.complete(
                    messages=messages,
                    tools=tools,
                    correlation_id=correlation_id,
                    max_output_tokens=output_limit,
                )
                if (
                    response.output_tokens > output_limit
                    or response.input_tokens + response.output_tokens
                    > descriptor.context_window_tokens
                ):
                    raise ProviderInvocationError(
                        kind=ProviderFailureKind.PROTOCOL,
                        provider_id=descriptor.provider_id,
                        request_id=response.provider_request_id,
                    )
                cost = descriptor.estimate_cost_microusd(
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
                if (
                    route_policy.max_call_cost_microusd is not None
                    and cost > route_policy.max_call_cost_microusd
                ):
                    raise ProviderInvocationError(
                        kind=ProviderFailureKind.PROTOCOL,
                        provider_id=descriptor.provider_id,
                        request_id=response.provider_request_id,
                    )
                state.consecutive_failures = 0
                state.opened_until = 0.0
                self._observe(
                    descriptor.provider_id,
                    "succeeded",
                    None,
                    duration_seconds=time.monotonic() - started,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_microusd=cost,
                )
                return replace(
                    response,
                    provider_id=descriptor.provider_id,
                    model=descriptor.model,
                    cost_microusd=cost,
                )
            except TimeoutError:
                error = ProviderInvocationError(
                    kind=ProviderFailureKind.CAPACITY,
                    provider_id=descriptor.provider_id,
                )
            except ProviderInvocationError as exc:
                error = exc
            except Exception as exc:
                error = ProviderInvocationError(
                    kind=ProviderFailureKind.UNKNOWN,
                    provider_id=descriptor.provider_id,
                )
                error.__cause__ = exc
            finally:
                if acquired:
                    state.semaphore.release()
            last_error = error
            self._record_failure(state, error)
            self._observe(
                descriptor.provider_id,
                "failed",
                error.kind.value,
                duration_seconds=time.monotonic() - started,
            )
            if not error.retryable:
                raise error
        assert last_error is not None
        raise last_error

    async def stream_routed(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        correlation_id: str,
        route_policy: ModelRoutePolicy,
    ):
        route_policy = replace(
            route_policy,
            required_capabilities=(
                route_policy.required_capabilities | {ModelCapability.STREAMING}
            ),
        )
        input_estimate = self._estimate_input_tokens(messages=messages, tools=tools)
        candidates = self._route_candidates(
            route_policy=route_policy,
            input_tokens=input_estimate,
            tools_required=bool(tools),
            messages=messages,
            tools=tools,
        )
        last_error: ProviderInvocationError | None = None
        sequence = 0
        for state in candidates[: self.max_failover_attempts + 1]:
            descriptor = state.registration.descriptor
            provider_input_tokens = self._provider_input_tokens(state,
                messages=messages, tools=tools, conservative=input_estimate)
            output_limit = self._output_limit(
                descriptor=descriptor,
                route_policy=route_policy,
                input_tokens=provider_input_tokens,
            )
            if output_limit is None:
                continue
            acquired = False
            emitted_text = False
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    state.semaphore.acquire(),
                    timeout=self.concurrency_wait_seconds,
                )
                acquired = True
                stream = getattr(state.registration.provider, "stream", None)
                if not callable(stream):
                    raise ProviderInvocationError(
                        kind=ProviderFailureKind.PROTOCOL,
                        provider_id=descriptor.provider_id,
                    )
                async for event in stream(
                    messages=messages,
                    tools=tools,
                    correlation_id=correlation_id,
                    max_output_tokens=output_limit,
                ):
                    if event.event_type is ModelStreamEventType.TEXT_DELTA:
                        emitted_text = True
                        sequence += 1
                        yield replace(
                            event,
                            sequence=sequence,
                            provider_id=descriptor.provider_id,
                            model=descriptor.model,
                        )
                        continue
                    response = event.response
                    if response is None:
                        raise ProviderInvocationError(
                            kind=ProviderFailureKind.PROTOCOL,
                            provider_id=descriptor.provider_id,
                        )
                    if (
                        response.output_tokens > output_limit
                        or response.input_tokens + response.output_tokens
                        > descriptor.context_window_tokens
                    ):
                        raise ProviderInvocationError(
                            kind=ProviderFailureKind.PROTOCOL,
                            provider_id=descriptor.provider_id,
                            request_id=response.provider_request_id,
                        )
                    cost = descriptor.estimate_cost_microusd(
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                    )
                    if (
                        route_policy.max_call_cost_microusd is not None
                        and cost > route_policy.max_call_cost_microusd
                    ):
                        raise ProviderInvocationError(
                            kind=ProviderFailureKind.PROTOCOL,
                            provider_id=descriptor.provider_id,
                            request_id=response.provider_request_id,
                        )
                    final_response = replace(
                        response,
                        provider_id=descriptor.provider_id,
                        model=descriptor.model,
                        cost_microusd=cost,
                    )
                    state.consecutive_failures = 0
                    state.opened_until = 0.0
                    self._observe(
                        descriptor.provider_id,
                        "succeeded",
                        None,
                        duration_seconds=time.monotonic() - started,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        cost_microusd=cost,
                    )
                    sequence += 1
                    yield ModelStreamEvent(
                        event_type=ModelStreamEventType.COMPLETED,
                        sequence=sequence,
                        response=final_response,
                        provider_id=descriptor.provider_id,
                        model=descriptor.model,
                    )
                    return
                raise ProviderInvocationError(
                    kind=ProviderFailureKind.PROTOCOL,
                    provider_id=descriptor.provider_id,
                )
            except TimeoutError:
                error = ProviderInvocationError(
                    kind=ProviderFailureKind.CAPACITY,
                    provider_id=descriptor.provider_id,
                )
            except ProviderInvocationError as exc:
                error = exc
            except Exception as exc:
                error = ProviderInvocationError(
                    kind=ProviderFailureKind.UNKNOWN,
                    provider_id=descriptor.provider_id,
                )
                error.__cause__ = exc
            finally:
                if acquired:
                    state.semaphore.release()
            last_error = error
            self._record_failure(state, error)
            self._observe(
                descriptor.provider_id,
                "failed",
                error.kind.value,
                duration_seconds=time.monotonic() - started,
            )
            if emitted_text or not error.retryable:
                raise error
        assert last_error is not None
        raise last_error

    def _route_candidates(
        self,
        *,
        route_policy: ModelRoutePolicy,
        input_tokens: int,
        tools_required: bool,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
    ) -> list[_ProviderState]:
        candidates = self._candidates(
            route_policy=route_policy,
            input_tokens=input_tokens,
            tools_required=tools_required,
            messages=messages, tools=tools,
            ignore_circuit=False,
            ignore_cost=False,
        )
        if candidates:
            return candidates
        eligible = self._candidates(
            route_policy=route_policy,
            input_tokens=input_tokens,
            tools_required=tools_required,
            messages=messages, tools=tools,
            ignore_circuit=True,
            ignore_cost=False,
        )
        if eligible:
            raise ProviderInvocationError(
                kind=ProviderFailureKind.CAPACITY,
                provider_id="gateway",
            )
        without_cost_limit = self._candidates(
            route_policy=route_policy,
            input_tokens=input_tokens,
            tools_required=tools_required,
            messages=messages, tools=tools,
            ignore_circuit=True,
            ignore_cost=True,
        )
        if without_cost_limit:
            raise ModelCostBudgetExceeded(
                "remaining model cost budget cannot fund an eligible route"
            )
        raise ModelRoutingError("no model route satisfies policy and budget")

    def _candidates(
        self,
        *,
        route_policy: ModelRoutePolicy,
        input_tokens: int,
        tools_required: bool,
        ignore_circuit: bool,
        ignore_cost: bool,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
    ) -> list[_ProviderState]:
        required = set(route_policy.required_capabilities)
        if tools_required:
            required.add(ModelCapability.TOOL_CALLING)
        now = time.monotonic()
        candidates: list[tuple[int, int, str, _ProviderState]] = []
        for state in self._states.values():
            descriptor = state.registration.descriptor
            provider_input_tokens = self._provider_input_tokens(state,
                messages=messages, tools=tools, conservative=input_tokens)
            if not ignore_circuit and state.opened_until > now:
                continue
            if route_policy.allowed_provider_ids and (
                descriptor.provider_id not in route_policy.allowed_provider_ids
            ):
                continue
            if not required.issubset(descriptor.capabilities):
                continue
            if route_policy.data_classification > descriptor.max_data_classification:
                continue
            if (
                descriptor.external
                and route_policy.data_classification.value > 0
                and not route_policy.allow_external_egress
            ):
                continue
            if route_policy.residency_regions and (
                descriptor.region not in route_policy.residency_regions
            ):
                continue
            output_limit = self._output_limit(
                descriptor=descriptor,
                route_policy=route_policy,
                input_tokens=provider_input_tokens,
            )
            if output_limit is None:
                continue
            estimated_cost = descriptor.estimate_cost_microusd(
                input_tokens=provider_input_tokens,
                output_tokens=output_limit,
            )
            if (
                not ignore_cost
                and route_policy.max_call_cost_microusd is not None
                and estimated_cost > route_policy.max_call_cost_microusd
            ):
                continue
            candidates.append(
                (descriptor.priority, estimated_cost, descriptor.provider_id, state)
            )
        candidates.sort(key=lambda item: item[:3])
        return [item[3] for item in candidates]

    @staticmethod
    def _output_limit(
        *,
        descriptor: ProviderDescriptor,
        route_policy: ModelRoutePolicy,
        input_tokens: int,
    ) -> int | None:
        limit = descriptor.max_output_tokens
        if route_policy.max_output_tokens is not None:
            limit = min(limit, route_policy.max_output_tokens)
        if route_policy.max_call_total_tokens is not None:
            remaining = route_policy.max_call_total_tokens - input_tokens
            if remaining <= 0:
                return None
            limit = min(limit, remaining)
        if input_tokens + limit > descriptor.context_window_tokens:
            limit = descriptor.context_window_tokens - input_tokens
        return limit if limit > 0 else None

    def _record_failure(
        self,
        state: _ProviderState,
        error: ProviderInvocationError,
    ) -> None:
        if not error.retryable:
            return
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.circuit_failure_threshold:
            state.opened_until = time.monotonic() + self.circuit_cooldown_seconds

    def _observe(
        self,
        provider_id: str,
        outcome: str,
        failure_kind: str | None,
        *,
        duration_seconds: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microusd: int = 0,
    ) -> None:
        if self.observer is not None:
            try:
                self.observer.record_model_attempt(
                    provider_id=provider_id,
                    outcome=outcome,
                    failure_kind=failure_kind,
                    duration_seconds=duration_seconds,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_microusd=cost_microusd,
                )
            except Exception:
                # Telemetry is deliberately best-effort: a broken exporter must not
                # turn a paid, successful provider response into a duplicate request.
                return

    @staticmethod
    def _estimate_input_tokens(
        *, messages: tuple[Message, ...], tools: tuple[ToolSpec, ...]
    ) -> int:
        # UTF-8 byte count deliberately overestimates common tokenizers for preflight safety.
        message_bytes = sum(len(item.content.encode("utf-8")) + 32 for item in messages)
        tool_bytes = len(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.parameters_schema,
                    }
                    for item in tools
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return max(1, message_bytes + tool_bytes)

    @staticmethod
    def _provider_input_tokens(state: _ProviderState, *, messages: tuple[Message, ...],
                               tools: tuple[ToolSpec, ...], conservative: int) -> int:
        counter = state.registration.token_counter
        if counter is None:
            return conservative
        value = counter.count(messages=messages, tools=tools)
        if type(value) is not int or value <= 0:
            raise ModelRoutingError("provider tokenizer returned an invalid count")
        return value
