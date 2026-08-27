from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator, FrozenSet, Protocol
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from ..security import RiskLevel
from ..tools import ApprovalReviewPolicy, ToolDefinition, ToolRegistry

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    endpoint_id: str
    tenant_id: str
    url: str
    bearer_token: str | None = field(default=None, repr=False)
    allow_insecure_localhost: bool = False

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.endpoint_id):
            raise ValueError("MCP endpoint id contains unsafe characters")
        if not self.tenant_id:
            raise ValueError("MCP endpoint tenant is required")
        parsed = urlsplit(self.url)
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise ValueError("MCP URL must not contain credentials or a fragment")
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (
            self.allow_insecure_localhost and local and parsed.scheme == "http"
        ):
            raise ValueError("MCP Streamable HTTP requires HTTPS")


@dataclass(frozen=True, slots=True)
class McpRemoteTool:
    name: str
    description: str
    parameters_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpToolPolicy:
    required_roles: FrozenSet[str] = field(default_factory=frozenset)
    risk: RiskLevel = RiskLevel.MEDIUM
    timeout_seconds: float = 30.0
    max_output_chars: int = 50_000
    approval_review: ApprovalReviewPolicy | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_output_chars <= 0:
            raise ValueError("MCP tool policy limits must be positive")
        if self.risk >= RiskLevel.HIGH and self.approval_review is None:
            raise ValueError("high-risk MCP tools require a local approval review policy")


class McpSession(Protocol):
    protocol_version: str

    async def list_tools(self) -> tuple[McpRemoteTool, ...]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class McpTransport(Protocol):
    @asynccontextmanager
    async def connect(self, endpoint: McpEndpoint) -> AsyncIterator[McpSession]: ...


class _OfficialSession:
    def __init__(self, session: ClientSession, protocol_version: str) -> None:
        self._session = session
        self.protocol_version = protocol_version

    async def list_tools(self) -> tuple[McpRemoteTool, ...]:
        discovered: list[McpRemoteTool] = []
        cursor: str | None = None
        while True:
            result = await self._session.list_tools(cursor)
            for tool in result.tools:
                discovered.append(
                    McpRemoteTool(
                        name=tool.name,
                        description=tool.description or "",
                        parameters_schema=tool.inputSchema,
                    )
                )
            cursor = result.nextCursor
            if not cursor:
                return tuple(discovered)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._session.call_tool(name, arguments)
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        if result.isError:
            raise RuntimeError("remote MCP tool reported an error")
        return payload


class OfficialMcpTransport:
    """Official MCP SDK Streamable HTTP transport with conservative HTTP defaults."""

    def __init__(self, *, connect_timeout_seconds: float = 10.0) -> None:
        self.connect_timeout_seconds = connect_timeout_seconds

    @asynccontextmanager
    async def connect(self, endpoint: McpEndpoint) -> AsyncIterator[McpSession]:
        headers = {"Accept": "application/json, text/event-stream"}
        if endpoint.bearer_token:
            headers["Authorization"] = f"Bearer {endpoint.bearer_token}"
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=60.0,
            write=30.0,
            pool=self.connect_timeout_seconds,
        )
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(
                endpoint.url,
                http_client=http_client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=60),
                ) as session:
                    initialized = await session.initialize()
                    if initialized.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
                        raise RuntimeError("MCP server negotiated an unsupported protocol version")
                    yield _OfficialSession(session, initialized.protocolVersion)


class McpGateway:
    """Tenant-scoped MCP discovery that never delegates authorization to a remote server."""

    def __init__(self, transport: McpTransport) -> None:
        self.transport = transport
        self._endpoints: dict[tuple[str, str], McpEndpoint] = {}

    def register_endpoint(self, endpoint: McpEndpoint) -> None:
        key = (endpoint.tenant_id, endpoint.endpoint_id)
        if key in self._endpoints:
            raise ValueError("MCP endpoint is already registered for this tenant")
        self._endpoints[key] = endpoint

    async def provision_tools(
        self,
        *,
        tenant_id: str,
        endpoint_id: str,
        registry: ToolRegistry,
        policies: dict[str, McpToolPolicy],
    ) -> tuple[str, ...]:
        endpoint = self._endpoints.get((tenant_id, endpoint_id))
        if endpoint is None:
            raise LookupError("MCP endpoint is absent or hidden")
        registered: list[str] = []
        async with self.transport.connect(endpoint) as session:
            remote_tools = await session.list_tools()
        for remote in remote_tools:
            if not _SAFE_NAME.fullmatch(remote.name):
                continue
            policy = policies.get(remote.name)
            if policy is None:
                continue
            local_name = f"mcp.{endpoint_id}.{remote.name}"

            async def handler(
                arguments: dict[str, Any],
                *,
                remote_name: str = remote.name,
                bound_endpoint: McpEndpoint = endpoint,
            ) -> Any:
                async with self.transport.connect(bound_endpoint) as live_session:
                    return await live_session.call_tool(remote_name, arguments)

            registry.register(
                ToolDefinition(
                    name=local_name,
                    description=remote.description,
                    handler=handler,
                    parameters_schema=remote.parameters_schema,
                    required_roles=policy.required_roles,
                    risk=policy.risk,
                    timeout_seconds=policy.timeout_seconds,
                    max_output_chars=policy.max_output_chars,
                    approval_review=policy.approval_review,
                )
            )
            registered.append(local_name)
        return tuple(registered)
