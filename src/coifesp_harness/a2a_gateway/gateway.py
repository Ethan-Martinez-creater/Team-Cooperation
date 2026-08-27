from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Message,
    Part,
    Role,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from ..collaboration.governance import GovernanceBoard
from ..security import Principal

_A2A_VERSION = "1.0"
_ALLOWED_BINDINGS = frozenset({"JSONRPC", "HTTP+JSON"})


@dataclass(frozen=True, slots=True)
class A2AEndpoint:
    agent_id: str
    tenant_id: str
    card: AgentCard


class A2AEndpointCatalog:
    """Default-deny, tenant-bound catalog for verified A2A 1.0 Agent Cards."""

    def __init__(self) -> None:
        self._endpoints: dict[tuple[str, str], A2AEndpoint] = {}

    def register(self, endpoint: A2AEndpoint) -> None:
        if not endpoint.agent_id or not endpoint.tenant_id:
            raise ValueError("A2A agent id and tenant are required")
        if not endpoint.card.security_requirements:
            raise ValueError("A2A Agent Card must require authentication")
        if not endpoint.card.supported_interfaces:
            raise ValueError("A2A Agent Card must expose an interface")
        for interface in endpoint.card.supported_interfaces:
            parsed = urlsplit(interface.url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("A2A production interfaces require HTTPS")
            if interface.protocol_version != _A2A_VERSION:
                raise ValueError("only A2A protocol version 1.0 is accepted")
            if interface.protocol_binding not in _ALLOWED_BINDINGS:
                raise ValueError("A2A interface binding is not enabled")
            if interface.tenant and interface.tenant != endpoint.tenant_id:
                raise ValueError("A2A interface tenant does not match catalog tenant")
        key = (endpoint.tenant_id, endpoint.agent_id)
        if key in self._endpoints:
            raise ValueError("A2A endpoint is already registered for this tenant")
        self._endpoints[key] = endpoint

    def get(self, *, tenant_id: str, agent_id: str) -> A2AEndpoint:
        endpoint = self._endpoints.get((tenant_id, agent_id))
        if endpoint is None:
            raise LookupError("A2A endpoint is absent or hidden")
        return endpoint


def build_public_agent_card(*, public_base_url: str, version: str) -> AgentCard:
    """Build the public capability card; mounting remains blocked until real OIDC exists."""
    parsed = urlsplit(public_base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("public A2A base URL must use HTTPS")
    return AgentCard(
        name="COIFESP Collaboration Harness",
        description="Secure cross-team task coordination and artifact handoff",
        supported_interfaces=[
            AgentInterface(
                url=f"{public_base_url.rstrip('/')}/a2a",
                protocol_binding="HTTP+JSON",
                protocol_version=_A2A_VERSION,
            )
        ],
        version=version,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        security_schemes={
            "oidc_bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="JWT",
                    description="OIDC access token validated by the control plane",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"oidc_bearer": StringList(list=[])})],
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="governance-assignment",
                name="Governance assignment exchange",
                description="Exchange explicitly disclosed task contracts and artifact references",
                tags=["collaboration", "governance", "handoff"],
            )
        ],
    )


def build_assignment_message(
    *,
    board: GovernanceBoard,
    principal: Principal,
    assignment_id: str,
    recipient_tenant_id: str,
    message_id: str,
) -> Message:
    """Create a minimal A2A handoff without private plan text, memory, or prompt state."""
    member = board.members.get(principal.principal_id)
    if member is None or member.tenant_id != principal.tenant_id:
        raise LookupError("governance program is absent or hidden")
    assignment = board.assignments.get(assignment_id)
    if assignment is None or principal.tenant_id not in assignment.visible_to_tenants:
        raise LookupError("assignment is absent or hidden")
    assignee = board.members[assignment.assignee_id]
    if assignee.tenant_id != recipient_tenant_id:
        raise ValueError("recipient tenant is not the assignment owner")
    if recipient_tenant_id not in assignment.visible_to_tenants:
        raise ValueError("assignment is not disclosed to the recipient")
    payload = {
        "schema": "coifesp.assignment.v1",
        "program_id": board.program_id,
        "assignment_id": assignment.assignment_id,
        "title": assignment.title,
        "deliverable_contract": assignment.deliverable_contract,
        "dependency_ids": list(assignment.dependencies),
        "plan_digest": assignment.plan_digest,
        "artifact_refs": list(assignment.artifact_refs),
    }
    return Message(
        message_id=message_id,
        role=Role.ROLE_USER,
        parts=[Part(text=json.dumps(payload, ensure_ascii=False, sort_keys=True))],
        metadata={
            "sender_tenant_id": principal.tenant_id,
            "recipient_tenant_id": recipient_tenant_id,
            "classification": board.classification.name.lower(),
        },
    )
