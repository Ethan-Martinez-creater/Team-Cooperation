from __future__ import annotations

from typing import Any

from ...config import Settings
from ..models import (
    LLMResponse,
    Message,
    ModelStreamEvent,
    ModelStreamEventType,
    ToolCall,
    ToolSpec,
)
from .models import ProviderFailureKind, ProviderInvocationError


class AnthropicProvider:
    """Async adapter for the Anthropic Messages API and compatible endpoints."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        provider_id: str = "anthropic",
        max_output_tokens: int = 4096,
    ) -> None:
        if not model or not provider_id:
            raise ValueError("model and provider_id are required")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.client = client
        self.model = model
        self.provider_id = provider_id
        self.max_output_tokens = max_output_tokens

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        max_output_tokens: int = 4096,
    ) -> "AnthropicProvider":
        settings.validate(require_llm=True)
        if settings.llm_provider != "anthropic":
            raise ValueError("settings do not select an Anthropic provider")
        if settings.llm_api_key is None:
            raise ValueError("LLM API key is required")
        from anthropic import AsyncAnthropic

        options: dict[str, Any] = {
            "api_key": settings.llm_api_key.reveal(),
            "timeout": timeout_seconds,
            "max_retries": max_retries,
        }
        if settings.llm_base_url:
            options["base_url"] = settings.llm_base_url
        client = AsyncAnthropic(**options)
        assert settings.llm_model is not None
        return cls(
            client=client,
            model=settings.llm_model,
            provider_id=settings.llm_provider,
            max_output_tokens=max_output_tokens,
        )

    async def complete(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        correlation_id: str,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        request = self._request(
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        )
        try:
            response = await self.client.messages.create(**request)
        except Exception as exc:
            raise self._provider_error(exc) from exc
        return self._parse_response(response)

    async def stream(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        correlation_id: str,
        max_output_tokens: int | None = None,
    ):
        request = self._request(
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        )
        sequence = 0
        try:
            async with self.client.messages.stream(**request) as stream:
                async for text in stream.text_stream:
                    if not text:
                        continue
                    sequence += 1
                    yield ModelStreamEvent(
                        event_type=ModelStreamEventType.TEXT_DELTA,
                        sequence=sequence,
                        text_delta=text,
                        provider_id=self.provider_id,
                        model=self.model,
                    )
                response = await stream.get_final_message()
                sequence += 1
                yield ModelStreamEvent(
                    event_type=ModelStreamEventType.COMPLETED,
                    sequence=sequence,
                    response=self._parse_response(response),
                    provider_id=self.provider_id,
                    model=self.model,
                )
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise self._provider_error(exc) from exc

    def _parse_response(self, response) -> LLMResponse:
        text: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(response, "content", ()):
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                arguments = getattr(block, "input", None)
                if not isinstance(arguments, dict) or not block.id or not block.name:
                    raise ProviderInvocationError(
                        kind=ProviderFailureKind.PROTOCOL,
                        provider_id=self.provider_id,
                        request_id=getattr(response, "id", None),
                    )
                tool_calls.append(ToolCall(block.id, block.name, arguments))
            elif block_type in {"thinking", "redacted_thinking"}:
                # Reasoning traces are intentionally not placed in team-visible checkpoints.
                continue
            else:
                raise ProviderInvocationError(
                    kind=ProviderFailureKind.PROTOCOL,
                    provider_id=self.provider_id,
                    request_id=getattr(response, "id", None),
                )
        stop_reason = getattr(response, "stop_reason", None)
        finish_reason = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "refusal": "content_filter",
        }.get(stop_reason, stop_reason)
        if finish_reason == "content_filter":
            raise ProviderInvocationError(
                kind=ProviderFailureKind.CONTENT_POLICY,
                provider_id=self.provider_id,
                request_id=getattr(response, "id", None),
            )
        usage = getattr(response, "usage", None)
        return LLMResponse(
            text="".join(text),
            tool_calls=tuple(tool_calls),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            provider_request_id=getattr(response, "id", None),
            provider_id=self.provider_id,
            model=self.model,
            finish_reason=finish_reason,
        )

    def _request(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        system, conversation = self._serialize_messages(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(
                self.max_output_tokens,
                max_output_tokens or self.max_output_tokens,
            ),
            "messages": conversation,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters_schema,
                }
                for tool in tools
            ]
            request["tool_choice"] = {"type": "auto"}
        return request

    def _serialize_messages(
        self, messages: tuple[Message, ...]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        conversation: list[dict[str, Any]] = []
        conversation_started = False
        for message in messages:
            if message.role == "system":
                if conversation_started:
                    raise ProviderInvocationError(
                        kind=ProviderFailureKind.PROTOCOL,
                        provider_id=self.provider_id,
                    )
                system_parts.append(message.content)
                continue
            conversation_started = True
            if message.role == "user":
                conversation.append({"role": "user", "content": message.content})
            elif message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                conversation.append({"role": "assistant", "content": blocks})
            elif message.role == "tool":
                if not message.tool_call_id:
                    raise ProviderInvocationError(
                        kind=ProviderFailureKind.PROTOCOL,
                        provider_id=self.provider_id,
                    )
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
                if (
                    conversation
                    and conversation[-1]["role"] == "user"
                    and isinstance(conversation[-1]["content"], list)
                ):
                    conversation[-1]["content"].append(block)
                else:
                    conversation.append({"role": "user", "content": [block]})
            else:
                raise ProviderInvocationError(
                    kind=ProviderFailureKind.PROTOCOL,
                    provider_id=self.provider_id,
                )
        if not conversation:
            raise ProviderInvocationError(
                kind=ProviderFailureKind.INVALID_REQUEST,
                provider_id=self.provider_id,
            )
        return "\n\n".join(system_parts), conversation

    def _provider_error(self, error: Exception) -> ProviderInvocationError:
        status = getattr(error, "status_code", None)
        request_id = getattr(error, "request_id", None)
        name = type(error).__name__
        if status == 401:
            kind = ProviderFailureKind.AUTHENTICATION
        elif status == 403:
            kind = ProviderFailureKind.PERMISSION
        elif status == 402:
            kind = ProviderFailureKind.QUOTA
        elif status in {400, 404, 422}:
            kind = ProviderFailureKind.INVALID_REQUEST
        elif status == 408 or "Timeout" in name:
            kind = ProviderFailureKind.TIMEOUT
        elif status == 429:
            kind = ProviderFailureKind.RATE_LIMIT
        elif status == 409 or (isinstance(status, int) and status >= 500):
            kind = ProviderFailureKind.SERVER
        elif "Connection" in name or isinstance(error, (ConnectionError, OSError)):
            kind = ProviderFailureKind.CONNECTION
        else:
            kind = ProviderFailureKind.UNKNOWN
        return ProviderInvocationError(
            kind=kind,
            provider_id=self.provider_id,
            request_id=request_id,
        )
