from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Callable

from .models import (
    Action,
    DisclosureGrant,
    Principal,
    ResourceLabel,
    RiskLevel,
)


class DecisionEffect(str, Enum):
    PERMIT = "permit"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class Decision:
    effect: DecisionEffect
    reason: str
    policy_version: str


class PolicyEngine:
    """Deterministic, default-deny policy decision point.

    Model output and tool metadata are inputs to this class, never authorities.
    """

    def __init__(
        self,
        policy_version: str = "2026-07-29.v1",
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.policy_version = policy_version
        self._clock = clock

    def decide_resource_access(
        self,
        *,
        principal: Principal,
        action: Action,
        resource: ResourceLabel,
    ) -> Decision:
        if action not in {
            Action.READ,
            Action.WRITE,
            Action.MEMORY_READ,
            Action.MEMORY_WRITE,
        }:
            return self._deny("action is not a local resource action")
        if principal.tenant_id != resource.owner_tenant_id:
            return self._deny("cross-tenant resource access requires disclosure workflow")
        if principal.clearance < resource.classification:
            return self._deny("principal clearance is insufficient")
        if not resource.compartments.issubset(principal.compartments):
            return self._deny("principal lacks one or more required compartments")
        return self._permit("tenant, clearance, and compartment checks passed")

    def decide_tool_execution(
        self,
        *,
        principal: Principal,
        required_roles: frozenset[str],
        risk: RiskLevel,
        input_label: ResourceLabel | None = None,
    ) -> Decision:
        if not required_roles.issubset(principal.roles):
            return self._deny("principal lacks required tool role")
        if input_label is not None:
            resource_decision = self.decide_resource_access(
                principal=principal,
                action=Action.READ,
                resource=input_label,
            )
            if resource_decision.effect is not DecisionEffect.PERMIT:
                return resource_decision
        if risk >= RiskLevel.HIGH:
            return self._approval("high-impact tool requires bound external approval")
        return self._permit("tool capability and risk checks passed")

    def decide_disclosure(
        self,
        *,
        sender: Principal,
        recipient: Principal,
        resource: ResourceLabel,
        purpose: str,
        grant: DisclosureGrant | None,
    ) -> Decision:
        if sender.tenant_id != resource.owner_tenant_id:
            return self._deny("only the owning tenant may disclose a resource")
        if sender.clearance < resource.classification:
            return self._deny("sender clearance is insufficient")
        if not resource.compartments.issubset(sender.compartments):
            return self._deny("sender lacks one or more resource compartments")
        if recipient.tenant_id == sender.tenant_id:
            if recipient.clearance < resource.classification:
                return self._deny("recipient clearance is insufficient")
            if not resource.compartments.issubset(recipient.compartments):
                return self._deny("recipient lacks one or more resource compartments")
            return self._permit("same-tenant disclosure checks passed")
        if resource.resource_id is None:
            return self._deny("cross-tenant resources require a stable resource_id")
        if grant is None:
            return self._approval("cross-tenant disclosure requires an explicit grant")
        if not grant.is_valid_for(
            resource=resource,
            recipient_tenant_id=recipient.tenant_id,
            purpose=purpose,
            now=self._clock(),
        ):
            return self._deny("disclosure grant does not match this transfer")
        if recipient.clearance < resource.classification:
            return self._deny("recipient clearance is insufficient")
        if not resource.compartments.issubset(recipient.compartments):
            return self._deny("recipient lacks one or more resource compartments")
        return self._permit("explicit disclosure grant and recipient checks passed")

    def _permit(self, reason: str) -> Decision:
        return Decision(DecisionEffect.PERMIT, reason, self.policy_version)

    def _deny(self, reason: str) -> Decision:
        return Decision(DecisionEffect.DENY, reason, self.policy_version)

    def _approval(self, reason: str) -> Decision:
        return Decision(DecisionEffect.REQUIRE_APPROVAL, reason, self.policy_version)
