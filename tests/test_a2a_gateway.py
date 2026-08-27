import json

import pytest

from coifesp_harness.a2a_gateway import (
    A2AEndpoint,
    A2AEndpointCatalog,
    build_assignment_message,
    build_public_agent_card,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.collaboration import CollaborationRole, GovernanceBoard
from coifesp_harness.collaboration.governance_models import BoardMember
from coifesp_harness.security import Classification, Principal


def approved_board() -> GovernanceBoard:
    board = GovernanceBoard(
        program_id="program-1",
        owner_tenant_id="team-a",
        title="Private program title",
        objective="Private program objective",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}),
        audit=InMemoryAuditSink(),
    )
    board.add_member(BoardMember("lead-a", "team-a", CollaborationRole.LEAD))
    board.add_member(
        BoardMember("worker-b", "team-b", CollaborationRole.CONTRIBUTOR),
        actor_id="lead-a",
    )
    plan = board.create_plan(
        actor_id="lead-a",
        plan_id="plan-1",
        version=1,
        title="Private plan title",
        objective="Private plan objective",
        deliverables=("contract",),
        required_approvers=frozenset({"lead-a"}),
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    board.open_discussion(actor_id="lead-a", plan_id=plan.plan_id)
    board.approve_plan(actor_id="lead-a", plan_id=plan.plan_id)
    board.propose_assignment(
        actor_id="lead-a",
        assignment_id="task-1",
        plan_id=plan.plan_id,
        assignee_id="worker-b",
        title="Implement adapter",
        description="Private implementation notes",
        deliverable_contract="Signed commit and contract test report",
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    return board


def test_public_agent_card_is_a2a_1_and_requires_bearer_authentication() -> None:
    card = build_public_agent_card(public_base_url="https://agents.example.test", version="0.1.0")
    assert card.supported_interfaces[0].protocol_version == "1.0"
    assert card.supported_interfaces[0].protocol_binding == "HTTP+JSON"
    assert card.security_requirements

    catalog = A2AEndpointCatalog()
    catalog.register(A2AEndpoint("coordinator", "team-a", card))
    assert catalog.get(tenant_id="team-a", agent_id="coordinator").card.name
    with pytest.raises(LookupError, match="absent or hidden"):
        catalog.get(tenant_id="team-b", agent_id="coordinator")


def test_a2a_catalog_rejects_plaintext_or_unauthenticated_cards() -> None:
    card = build_public_agent_card(public_base_url="https://agents.example.test", version="1")
    card.supported_interfaces[0].url = "http://agents.example.test/a2a"
    with pytest.raises(ValueError, match="HTTPS"):
        A2AEndpointCatalog().register(A2AEndpoint("agent", "team-a", card))

    unauthenticated = build_public_agent_card(
        public_base_url="https://agents.example.test", version="1"
    )
    del unauthenticated.security_requirements[:]
    with pytest.raises(ValueError, match="authentication"):
        A2AEndpointCatalog().register(A2AEndpoint("agent", "team-a", unauthenticated))


def test_assignment_handoff_contains_only_explicit_contract_fields() -> None:
    message = build_assignment_message(
        board=approved_board(),
        principal=Principal("lead-a", "team-a"),
        assignment_id="task-1",
        recipient_tenant_id="team-b",
        message_id="message-1",
    )
    payload = json.loads(message.parts[0].text)
    assert payload["assignment_id"] == "task-1"
    assert payload["deliverable_contract"] == "Signed commit and contract test report"
    serialized = message.SerializeToString()
    assert b"Private implementation notes" not in serialized
    assert b"Private program objective" not in serialized
    assert b"Private plan objective" not in serialized


def test_assignment_handoff_rejects_an_unrelated_recipient() -> None:
    with pytest.raises(ValueError, match="not the assignment owner"):
        build_assignment_message(
            board=approved_board(),
            principal=Principal("lead-a", "team-a"),
            assignment_id="task-1",
            recipient_tenant_id="team-c",
            message_id="message-1",
        )
