from datetime import UTC, datetime, timedelta

from coifesp_harness.security import (
    Action,
    Classification,
    DecisionEffect,
    DisclosureGrant,
    PolicyEngine,
    Principal,
    ResourceLabel,
)


def test_local_access_requires_tenant_clearance_and_compartment() -> None:
    policy = PolicyEngine()
    principal = Principal(
        "alice",
        "team-a",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    resource = ResourceLabel(
        "team-a",
        Classification.CONFIDENTIAL,
        frozenset({"project-x"}),
        "design-1",
    )
    assert (
        policy.decide_resource_access(
            principal=principal, action=Action.READ, resource=resource
        ).effect
        is DecisionEffect.PERMIT
    )

    outsider = Principal(
        "mallory",
        "team-b",
        clearance=Classification.RESTRICTED,
        compartments=frozenset({"project-x"}),
    )
    assert (
        policy.decide_resource_access(
            principal=outsider, action=Action.READ, resource=resource
        ).effect
        is DecisionEffect.DENY
    )


def test_cross_tenant_disclosure_requires_exact_expiring_grant() -> None:
    policy = PolicyEngine()
    sender = Principal(
        "alice",
        "team-a",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    recipient = Principal(
        "bob",
        "team-b",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    resource = ResourceLabel(
        "team-a",
        Classification.CONFIDENTIAL,
        frozenset({"project-x"}),
        "artifact-7",
    )
    missing = policy.decide_disclosure(
        sender=sender,
        recipient=recipient,
        resource=resource,
        purpose="integration",
        grant=None,
    )
    assert missing.effect is DecisionEffect.REQUIRE_APPROVAL

    grant = DisclosureGrant(
        grant_id="grant-1",
        owner_tenant_id="team-a",
        recipient_tenant_id="team-b",
        resource_id="artifact-7",
        purpose="integration",
        approved_by="security-owner",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        max_classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    allowed = policy.decide_disclosure(
        sender=sender,
        recipient=recipient,
        resource=resource,
        purpose="integration",
        grant=grant,
    )
    assert allowed.effect is DecisionEffect.PERMIT
