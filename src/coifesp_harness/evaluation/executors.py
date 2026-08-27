from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping

from ..runtime.loop import AgentLoop
from ..runtime.models import AgentRunRequest
from ..security.models import Classification, Principal, ResourceLabel, RiskLevel, Action, DisclosureGrant
from ..security.policy import PolicyEngine
from .models import EvaluationCase
from .runner import EvaluationObservation, RunContext


def _exact(value: object, fields: set[str], context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{context} fields are invalid")
    return value


def _principal(value: object) -> Principal:
    raw = _exact(value, {"principal_id", "tenant_id", "roles", "clearance", "compartments", "is_service"}, "principal")
    if type(raw["is_service"]) is not bool:
        raise ValueError("principal is_service must be boolean")
    return Principal(
        principal_id=str(raw["principal_id"]), tenant_id=str(raw["tenant_id"]),
        roles=frozenset(str(item) for item in raw["roles"]),
        clearance=Classification[str(raw["clearance"])],
        compartments=frozenset(str(item) for item in raw["compartments"]),
        is_service=raw["is_service"],
    )


def _resource(value: object) -> ResourceLabel:
    raw = _exact(value, {"owner_tenant_id", "classification", "compartments", "resource_id"}, "resource")
    return ResourceLabel(
        owner_tenant_id=str(raw["owner_tenant_id"]),
        classification=Classification[str(raw["classification"])],
        compartments=frozenset(str(item) for item in raw["compartments"]),
        resource_id=None if raw["resource_id"] is None else str(raw["resource_id"]),
    )


class PolicyEngineEvaluationExecutor:
    """Strict adapter that exercises the production PDP, not a policy mock."""

    def __init__(self, engine: PolicyEngine) -> None:
        self._engine = engine

    def execute(self, case: EvaluationCase, context: RunContext) -> EvaluationObservation:
        raw = case.input_payload
        operation = raw.get("operation")
        if operation == "resource_access":
            body = _exact(raw, {"operation", "principal", "action", "resource"}, "case input")
            decision = self._engine.decide_resource_access(
                principal=_principal(body["principal"]), action=Action(str(body["action"])),
                resource=_resource(body["resource"]),
            )
        elif operation == "tool_execution":
            body = _exact(raw, {"operation", "principal", "required_roles", "risk", "input_label"}, "case input")
            decision = self._engine.decide_tool_execution(
                principal=_principal(body["principal"]),
                required_roles=frozenset(str(item) for item in body["required_roles"]),
                risk=RiskLevel[str(body["risk"])],
                input_label=None if body["input_label"] is None else _resource(body["input_label"]),
            )
        elif operation == "disclosure":
            body = _exact(raw, {"operation", "sender", "recipient", "resource", "purpose", "grant"}, "case input")
            grant_raw = body["grant"]
            grant = None
            if grant_raw is not None:
                item = _exact(grant_raw, {"grant_id", "owner_tenant_id", "recipient_tenant_id", "resource_id", "purpose", "approved_by", "expires_at", "max_classification", "compartments"}, "grant")
                grant = DisclosureGrant(
                    grant_id=str(item["grant_id"]), owner_tenant_id=str(item["owner_tenant_id"]),
                    recipient_tenant_id=str(item["recipient_tenant_id"]), resource_id=str(item["resource_id"]),
                    purpose=str(item["purpose"]), approved_by=str(item["approved_by"]),
                    expires_at=datetime.fromisoformat(str(item["expires_at"])),
                    max_classification=Classification[str(item["max_classification"])],
                    compartments=frozenset(str(value) for value in item["compartments"]),
                )
            decision = self._engine.decide_disclosure(
                sender=_principal(body["sender"]), recipient=_principal(body["recipient"]),
                resource=_resource(body["resource"]), purpose=str(body["purpose"]), grant=grant,
            )
        else:
            raise ValueError("unsupported PDP evaluation operation")
        return EvaluationObservation.from_policy_effect(decision.effect, output=decision.reason)


class AgentLoopEvaluationExecutor:
    """Runs signed cases through the production AgentLoop with a controlled request factory."""

    def __init__(
        self,
        loop: AgentLoop,
        request_factory: Callable[[EvaluationCase, RunContext], AgentRunRequest],
    ) -> None:
        self._loop, self._request_factory = loop, request_factory

    async def execute_async(self, case: EvaluationCase, context: RunContext) -> EvaluationObservation:
        request = self._request_factory(case, context)
        if not isinstance(request, AgentRunRequest):
            raise TypeError("request factory returned an invalid AgentRunRequest")
        result = await self._loop.run(request)
        mapping = {"completed": "permit", "awaiting_approval": "require_approval"}
        effect = mapping.get(result.status, "deny")
        output = next((message.content for message in reversed(result.messages) if message.role == "assistant"), "")
        return EvaluationObservation.from_policy_effect(effect, output=output)
