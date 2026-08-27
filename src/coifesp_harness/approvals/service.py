from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..errors import PolicyDenied, ResourceNotFound
from ..security.models import Action, Classification, Principal, ResourceLabel
from ..security.policy import DecisionEffect, PolicyEngine
from .models import ApprovalRecord
from .repository import SQLAlchemyApprovalRepository


class ApprovalService:
    def __init__(
        self,
        repository: SQLAlchemyApprovalRepository,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy or PolicyEngine()

    def request(
        self,
        *,
        principal: Principal,
        approval_id: str,
        tool_name: str,
        request_digest: str,
        reason: str,
        expires_in_seconds: int = 900,
        required_approver_role: str = "tool_approver",
        classification: Classification = Classification.INTERNAL,
        compartments: frozenset[str] = frozenset(),
    ) -> ApprovalRecord:
        if not 60 <= expires_in_seconds <= 86_400:
            raise ValueError("approval lifetime must be between 60 and 86400 seconds")
        label = ResourceLabel(
            owner_tenant_id=principal.tenant_id,
            classification=classification,
            compartments=compartments,
        )
        decision = self.policy.decide_resource_access(
            principal=principal,
            action=Action.READ,
            resource=label,
        )
        if decision.effect is not DecisionEffect.PERMIT:
            raise PolicyDenied("requester cannot access the labeled approval input")
        return self.repository.create(
            tenant_id=principal.tenant_id,
            approval_id=approval_id,
            requester_id=principal.principal_id,
            tool_name=tool_name,
            request_digest=request_digest,
            reason=reason,
            label=label,
            required_approver_role=required_approver_role,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    def decide(
        self,
        *,
        principal: Principal,
        approval_id: str,
        approve: bool,
        expected_version: int,
    ) -> ApprovalRecord:
        return self.repository.decide(
            tenant_id=principal.tenant_id,
            approval_id=approval_id,
            approver_id=principal.principal_id,
            approver_roles=principal.roles,
            approver_clearance=principal.clearance,
            approver_compartments=principal.compartments,
            approve=approve,
            expected_version=expected_version,
        )

    def request_for_tool(
        self,
        *,
        principal: Principal,
        approval_id: str,
        tool_name: str,
        request_digest: str,
        review_projection: dict[str, Any],
        label: ResourceLabel | None,
        expires_in_seconds: int,
        required_approver_role: str,
    ) -> ApprovalRecord:
        effective_label = label or ResourceLabel(
            owner_tenant_id=principal.tenant_id,
            classification=Classification.INTERNAL,
        )
        decision = self.policy.decide_resource_access(
            principal=principal,
            action=Action.READ,
            resource=effective_label,
        )
        if decision.effect is not DecisionEffect.PERMIT:
            raise PolicyDenied("requester cannot access the labeled approval input")
        return self.repository.create(
            tenant_id=principal.tenant_id,
            approval_id=approval_id,
            requester_id=principal.principal_id,
            tool_name=tool_name,
            request_digest=request_digest,
            reason="tool-managed approval review projection",
            label=effective_label,
            required_approver_role=required_approver_role,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
            origin="tool_managed",
            review_projection=review_projection,
        )

    def revoke(
        self,
        *,
        principal: Principal,
        approval_id: str,
        expected_version: int,
    ) -> ApprovalRecord:
        return self.repository.revoke(
            tenant_id=principal.tenant_id,
            approval_id=approval_id,
            actor_id=principal.principal_id,
            actor_roles=principal.roles,
            expected_version=expected_version,
        )

    def get(self, *, principal: Principal, approval_id: str) -> ApprovalRecord:
        record = self.repository.get(
            tenant_id=principal.tenant_id,
            approval_id=approval_id,
        )
        if (
            principal.principal_id != record.requester_id
            and record.required_approver_role not in principal.roles
        ):
            raise ResourceNotFound("approval is absent or hidden")
        if principal.clearance < record.classification or not record.compartments.issubset(
            principal.compartments
        ):
            raise ResourceNotFound("approval is absent or hidden")
        return record

    def list_pending(
        self,
        *,
        principal: Principal,
        limit: int = 100,
    ) -> tuple[ApprovalRecord, ...]:
        records = self.repository.list_pending(
            tenant_id=principal.tenant_id,
            required_roles=principal.roles,
            limit=limit,
        )
        return tuple(
            record
            for record in records
            if principal.clearance >= record.classification
            and record.compartments.issubset(principal.compartments)
        )

    def consume(
        self,
        *,
        principal: Principal,
        approval_id: str,
        tool_name: str,
        request_digest: str,
        execution_id: str,
        input_label: ResourceLabel | None = None,
        require_tool_managed: bool = False,
    ) -> ApprovalRecord:
        return self.repository.consume(
            tenant_id=principal.tenant_id,
            approval_id=approval_id,
            requester_id=principal.principal_id,
            tool_name=tool_name,
            request_digest=request_digest,
            execution_id=execution_id,
            input_label=input_label
            or ResourceLabel(
                owner_tenant_id=principal.tenant_id,
                classification=Classification.INTERNAL,
            ),
            required_origin="tool_managed" if require_tool_managed else None,
        )
