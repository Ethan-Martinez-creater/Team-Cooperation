import asyncio
from contextlib import asynccontextmanager

import pytest

from coifesp_harness.mcp_gateway import (
    McpEndpoint,
    McpGateway,
    McpRemoteTool,
    McpToolPolicy,
)
from coifesp_harness.security import RiskLevel
from coifesp_harness.tools import (
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ReviewDisclosure,
    ToolRegistry,
)


class FakeSession:
    protocol_version = "2025-11-25"

    async def list_tools(self) -> tuple[McpRemoteTool, ...]:
        return (
            McpRemoteTool(
                name="approved",
                description="Approved remote operation",
                parameters_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            McpRemoteTool(
                name="unapproved",
                description="Must remain unavailable",
                parameters_schema={"type": "object"},
            ),
        )

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return {"remote_name": name, "arguments": arguments}


class FakeTransport:
    @asynccontextmanager
    async def connect(self, endpoint: McpEndpoint):
        yield FakeSession()


def test_endpoint_rejects_credentials_and_plaintext_remote_transport() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        McpEndpoint("source", "team-a", "http://mcp.example.test/mcp")
    with pytest.raises(ValueError, match="credentials"):
        McpEndpoint("source", "team-a", "https://user:secret@mcp.example.test/mcp")

    local = McpEndpoint(
        "local",
        "team-a",
        "http://127.0.0.1:9000/mcp",
        allow_insecure_localhost=True,
    )
    assert "secret" not in repr(
        McpEndpoint("safe", "team-a", "https://mcp.example.test/mcp", "secret")
    )
    assert local.endpoint_id == "local"


def test_only_locally_authorized_remote_tools_are_provisioned() -> None:
    registry = ToolRegistry()
    gateway = McpGateway(FakeTransport())
    gateway.register_endpoint(McpEndpoint("source", "team-a", "https://mcp.test/mcp"))

    names = asyncio.run(
        gateway.provision_tools(
            tenant_id="team-a",
            endpoint_id="source",
            registry=registry,
            policies={
                "approved": McpToolPolicy(
                    required_roles=frozenset({"developer"}),
                    risk=RiskLevel.HIGH,
                    timeout_seconds=7,
                    max_output_chars=1000,
                    approval_review=ApprovalReviewPolicy(
                        fields=(
                            ApprovalReviewField(
                                "value",
                                "/value",
                                ReviewDisclosure.VALUE,
                            ),
                        )
                    ),
                )
            },
        )
    )

    assert names == ("mcp.source.approved",)
    assert registry.get("mcp.source.unapproved") is None
    definition = registry.get("mcp.source.approved")
    assert definition is not None
    assert definition.required_roles == frozenset({"developer"})
    assert definition.risk is RiskLevel.HIGH
    assert asyncio.run(definition.handler({"value": "ok"})) == {
        "remote_name": "approved",
        "arguments": {"value": "ok"},
    }


def test_endpoint_catalog_is_tenant_isolated() -> None:
    gateway = McpGateway(FakeTransport())
    gateway.register_endpoint(McpEndpoint("source", "team-a", "https://mcp.test/mcp"))
    with pytest.raises(LookupError, match="absent or hidden"):
        asyncio.run(
            gateway.provision_tools(
                tenant_id="team-b",
                endpoint_id="source",
                registry=ToolRegistry(),
                policies={},
            )
        )
