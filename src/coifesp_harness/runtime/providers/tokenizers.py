from __future__ import annotations

import json
from typing import Protocol

from ..models import Message, ToolSpec


class ProviderTokenCounter(Protocol):
    exact: bool
    tokenizer_id: str
    def count_text(self, text: str) -> int: ...
    def count(self, *, messages: tuple[Message, ...], tools: tuple[ToolSpec, ...]) -> int: ...


class TiktokenCounter:
    """Local deterministic counter bound to an explicit encoding and wire schema."""
    exact = True

    def __init__(self, encoding_name: str) -> None:
        import tiktoken
        self.encoding = tiktoken.get_encoding(encoding_name)
        self.tokenizer_id = f"tiktoken:{encoding_name}:coifesp-chat-v1"

    def count(self, *, messages: tuple[Message, ...], tools: tuple[ToolSpec, ...]) -> int:
        payload = {"messages": [_message(item) for item in messages],
            "tools": [{"type": "function", "function": {"name": item.name,
                "description": item.description, "parameters": item.parameters_schema}}
                for item in tools]}
        wire = json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        return max(1, len(self.encoding.encode(wire, disallowed_special=())))

    def count_text(self, text: str) -> int:
        return max(1, len(self.encoding.encode(text, disallowed_special=())))


def _message(item: Message) -> dict:
    value = {"role": item.role, "content": item.content}
    if item.name is not None: value["name"] = item.name
    if item.tool_call_id is not None: value["tool_call_id"] = item.tool_call_id
    if item.tool_calls:
        value["tool_calls"] = [{"id": call.call_id, "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments,
                ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)}}
            for call in item.tool_calls]
    return value
