from __future__ import annotations

import json
import inspect
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


class ProviderProtocolError(ProviderInvocationError):
    """A model response violated the provider adapter contract."""

    def __init__(self, provider_id: str, request_id: str | None = None) -> None:
        super().__init__(
            kind=ProviderFailureKind.PROTOCOL,
            provider_id=provider_id,
            request_id=request_id,
        )


class OpenAICompatibleProvider:
    """Async adapter for OpenAI Chat Completions compatible providers."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        provider_id: str = "openai-compatible",
        max_output_tokens: int = 4096,
    ) -> None:
        if not model or not provider_id:
            raise ValueError("model is required")
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
    ) -> "OpenAICompatibleProvider":
        settings.validate(require_llm=True)
        if settings.llm_provider not in {"openai", "openai_compatible"}:
            raise ValueError("settings do not select an OpenAI-compatible provider")
        if settings.llm_api_key is None:
            raise ValueError("LLM API key is required")

        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.llm_api_key.reveal(),
            base_url=settings.llm_base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
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
            response = await self.client.chat.completions.create(**request)
        except Exception as exc:
            raise self._provider_error(exc) from exc
        if not response.choices:
            raise ProviderProtocolError(self.provider_id)
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []
        for raw_call in message.tool_calls or ():
            try:
                arguments = json.loads(raw_call.function.arguments)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProviderProtocolError(self.provider_id) from exc
            if not isinstance(arguments, dict):
                raise ProviderProtocolError(self.provider_id)
            if not raw_call.id or not raw_call.function.name:
                raise ProviderProtocolError(self.provider_id)
            tool_calls.append(
                ToolCall(
                    call_id=raw_call.id,
                    name=raw_call.function.name,
                    arguments=arguments,
                )
            )

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "content_filter":
            raise ProviderInvocationError(
                kind=ProviderFailureKind.CONTENT_POLICY,
                provider_id=self.provider_id,
                request_id=getattr(response, "id", None),
            )
        if finish_reason == "insufficient_system_resource":
            raise ProviderInvocationError(
                kind=ProviderFailureKind.CAPACITY,
                provider_id=self.provider_id,
                request_id=getattr(response, "id", None),
            )
        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=message.content or "",
            tool_calls=tuple(tool_calls),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            provider_request_id=getattr(response, "id", None),
            provider_id=self.provider_id,
            model=self.model,
            finish_reason=finish_reason,
        )

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
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}
        stream = None
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        input_tokens = 0
        output_tokens = 0
        request_id = None
        finish_reason = None
        sequence = 0
        try:
            stream = await self.client.chat.completions.create(**request)
            async for chunk in stream:
                request_id = getattr(chunk, "id", None) or request_id
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_tokens", 0) or input_tokens
                    output_tokens = getattr(usage, "completion_tokens", 0) or output_tokens
                for choice in getattr(chunk, "choices", ()):
                    finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    content = getattr(delta, "content", None)
                    if content:
                        text_parts.append(content)
                        sequence += 1
                        yield ModelStreamEvent(
                            event_type=ModelStreamEventType.TEXT_DELTA,
                            sequence=sequence,
                            text_delta=content,
                            provider_id=self.provider_id,
                            model=self.model,
                        )
                    for raw_call in getattr(delta, "tool_calls", ()) or ():
                        index = getattr(raw_call, "index", None)
                        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                            raise ProviderProtocolError(self.provider_id, request_id)
                        part = tool_parts.setdefault(
                            index,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        part["id"] = getattr(raw_call, "id", None) or part["id"]
                        function = getattr(raw_call, "function", None)
                        if function is not None:
                            part["name"] = getattr(function, "name", None) or part["name"]
                            part["arguments"] += getattr(function, "arguments", None) or ""
            if finish_reason == "content_filter":
                raise ProviderInvocationError(
                    kind=ProviderFailureKind.CONTENT_POLICY,
                    provider_id=self.provider_id,
                    request_id=request_id,
                )
            if finish_reason == "insufficient_system_resource":
                raise ProviderInvocationError(
                    kind=ProviderFailureKind.CAPACITY,
                    provider_id=self.provider_id,
                    request_id=request_id,
                )
            calls: list[ToolCall] = []
            for index in sorted(tool_parts):
                part = tool_parts[index]
                if not part["id"] or not part["name"]:
                    raise ProviderProtocolError(self.provider_id, request_id)
                try:
                    arguments = json.loads(part["arguments"])
                except json.JSONDecodeError as exc:
                    raise ProviderProtocolError(self.provider_id, request_id) from exc
                if not isinstance(arguments, dict):
                    raise ProviderProtocolError(self.provider_id, request_id)
                calls.append(ToolCall(part["id"], part["name"], arguments))
            sequence += 1
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.COMPLETED,
                sequence=sequence,
                response=LLMResponse(
                    text="".join(text_parts),
                    tool_calls=tuple(calls),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    provider_request_id=request_id,
                    provider_id=self.provider_id,
                    model=self.model,
                    finish_reason=finish_reason,
                ),
                provider_id=self.provider_id,
                model=self.model,
            )
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise self._provider_error(exc) from exc
        finally:
            if stream is not None:
                close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    def _request(
        self,
        *,
        messages: tuple[Message, ...],
        tools: tuple[ToolSpec, ...],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [self._serialize_message(message) for message in messages],
            "max_tokens": min(
                self.max_output_tokens,
                max_output_tokens or self.max_output_tokens,
            ),
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema,
                    },
                }
                for tool in tools
            ]
            request["tool_choice"] = "auto"
        return request

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
        elif status in {408} or "Timeout" in name:
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

    def _serialize_message(self, message: Message) -> dict[str, Any]:
        if message.role == "assistant":
            value: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or None,
            }
            if message.tool_calls:
                value["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            return value
        if message.role == "tool":
            if not message.tool_call_id:
                raise ProviderProtocolError(self.provider_id)
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        if message.role not in {"system", "user"}:
            raise ProviderProtocolError(self.provider_id)
        if not message.images:
            return {"role": message.role, "content": message.content}
        if message.role != "user":
            raise ProviderProtocolError(self.provider_id)
        content: list[dict[str, Any]] = [
            {"type": "text", "text": message.content}
        ]
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.media_type};base64,{image.data_base64}",
                },
            }
            for image in message.images
        )
        return {"role": "user", "content": content}
