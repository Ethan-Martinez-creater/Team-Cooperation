from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..audit import AuditEvent, AuditSink
from ..approvals import ApprovalService, ApprovalWorkflowError
from ..errors import ResourceNotFound
from ..idempotency import ClaimStatus, IdempotencyStore
from ..security.policy import DecisionEffect, PolicyEngine
from .approval_review import ApprovalReviewProjector
from .models import ExecutionStatus, ToolExecutionRequest, ToolExecutionResult
from .registry import ToolRegistry


def digest_tool_request(tool_name: str, arguments: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments must be canonical JSON") from exc
    if len(canonical.encode("utf-8")) > 1_048_576:
        raise ValueError("tool arguments exceed the request size limit")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolExecutor:
    """Policy-enforced tool boundary with approvals, timeout, idempotency, and audit."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        audit: AuditSink,
        idempotency: IdempotencyStore,
        approval_service: ApprovalService | None = None,
        approval_projector: ApprovalReviewProjector | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self.idempotency = idempotency
        self.approval_service = approval_service
        self.approval_projector = approval_projector or ApprovalReviewProjector()

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        prepared = self.prepare_dispatch(request)
        if prepared.status is not ExecutionStatus.DISPATCH_REQUIRED:
            return prepared
        digest = prepared.request_digest
        assert digest is not None
        tool = self.registry.get(request.tool_name)
        assert tool is not None
        claim = self.idempotency.claim(
            namespace="tool.execute",
            tenant_id=request.principal.tenant_id,
            idempotency_key=request.idempotency_key,
            request_digest=digest,
        )
        if claim is ClaimStatus.DUPLICATE:
            return self._finish(
                request,
                ExecutionStatus.DUPLICATE_SUPPRESSED,
                error="idempotency key was already used for the same request",
                request_digest=digest,
            )
        if claim is ClaimStatus.CONFLICT:
            return self._finish(
                request,
                ExecutionStatus.IDEMPOTENCY_CONFLICT,
                error="idempotency key was reused with different arguments",
                request_digest=digest,
            )

        try:
            output = await asyncio.wait_for(
                tool.handler(request.arguments), timeout=tool.timeout_seconds
            )
            output = self._limit_output(output, tool.max_output_chars)
            return self._finish(
                request,
                ExecutionStatus.SUCCEEDED,
                output=output,
                request_digest=digest,
            )
        except TimeoutError:
            return self._finish(
                request,
                ExecutionStatus.TIMED_OUT,
                error=f"tool exceeded {tool.timeout_seconds:g}s timeout",
                request_digest=digest,
            )
        except Exception as exc:
            return self._finish(
                request,
                ExecutionStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                request_digest=digest,
            )

    def prepare_dispatch(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """Authorize and validate without performing a side effect.

        Durable runtimes use this phase before atomically committing a batch of
        Tool Jobs with the Agent checkpoint. The normal in-process path calls it
        and immediately executes the handler.
        """
        tool = self.registry.get(request.tool_name)
        if tool is None:
            return self._finish(request, ExecutionStatus.DENIED, error="unknown tool")

        decision = self.policy.decide_tool_execution(
            principal=request.principal,
            required_roles=tool.required_roles,
            risk=tool.risk,
            input_label=request.input_label,
        )
        if decision.effect is DecisionEffect.DENY:
            return self._finish(
                request,
                ExecutionStatus.DENIED,
                error=decision.reason,
                policy_version=decision.policy_version,
            )

        try:
            digest = digest_tool_request(request.tool_name, request.arguments)
        except ValueError:
            return self._finish(
                request,
                ExecutionStatus.FAILED,
                error="tool arguments are not bounded canonical JSON",
                policy_version=decision.policy_version,
            )
        try:
            Draft202012Validator(tool.parameters_schema).validate(request.arguments)
            if tool.validator is not None:
                tool.validator(request.arguments)
        except ValidationError as exc:
            field_path = ".".join(str(item) for item in exc.absolute_path) or "<root>"
            return self._finish(
                request,
                ExecutionStatus.FAILED,
                error=(f"invalid tool arguments at {field_path}: " f"violates {exc.validator}"),
                policy_version=decision.policy_version,
                request_digest=digest,
            )
        except (ValueError, TypeError):
            return self._finish(
                request,
                ExecutionStatus.FAILED,
                error="invalid tool arguments: custom validation failed",
                policy_version=decision.policy_version,
                request_digest=digest,
            )

        if decision.effect is DecisionEffect.REQUIRE_APPROVAL:
            approved = False
            effective_approval_id = request.approval_id
            if self.approval_service is not None and request.approval_id is not None:
                try:
                    self.approval_service.consume(
                        principal=request.principal,
                        approval_id=request.approval_id,
                        tool_name=request.tool_name,
                        request_digest=digest,
                        execution_id=request.execution_id,
                        input_label=request.input_label,
                        require_tool_managed=True,
                    )
                    approved = True
                except (ApprovalWorkflowError, ResourceNotFound):
                    approved = False
            elif self.approval_service is None:
                approved = request.approval is not None and request.approval.is_valid(
                    tenant_id=request.principal.tenant_id,
                    principal_id=request.principal.principal_id,
                    tool_name=request.tool_name,
                    digest=digest,
                )
            if not approved:
                if self.approval_service is not None and request.approval_id is None:
                    if tool.approval_review is None:
                        return self._finish(
                            request,
                            ExecutionStatus.DENIED,
                            error="high-risk tool lacks a managed approval review policy",
                            policy_version=decision.policy_version,
                            request_digest=digest,
                        )
                    try:
                        projection = self.approval_projector.project(
                            tool_name=request.tool_name,
                            arguments=request.arguments,
                            policy=tool.approval_review,
                        )
                        effective_approval_id = self._managed_approval_id(request, digest)
                        self.approval_service.request_for_tool(
                            principal=request.principal,
                            approval_id=effective_approval_id,
                            tool_name=request.tool_name,
                            request_digest=digest,
                            review_projection=projection,
                            label=request.input_label,
                            expires_in_seconds=tool.approval_review.expires_in_seconds,
                            required_approver_role=(tool.approval_review.required_approver_role),
                        )
                    except (ApprovalWorkflowError, ResourceNotFound, ValueError):
                        return self._finish(
                            request,
                            ExecutionStatus.DENIED,
                            error="managed approval review projection could not be created",
                            policy_version=decision.policy_version,
                            request_digest=digest,
                        )
                return self._finish(
                    request,
                    ExecutionStatus.APPROVAL_REQUIRED,
                    error=decision.reason,
                    policy_version=decision.policy_version,
                    request_digest=digest,
                    approval_id=effective_approval_id,
                )

        return ToolExecutionResult(
            execution_id=request.execution_id,
            status=ExecutionStatus.DISPATCH_REQUIRED,
            request_digest=digest,
        )

    @staticmethod
    def _managed_approval_id(
        request: ToolExecutionRequest,
        request_digest: str,
    ) -> str:
        value = (
            f"{request.principal.tenant_id}\0{request.principal.principal_id}\0"
            f"{request.execution_id}\0{request.tool_name}\0{request_digest}"
        )
        return f"approval-{hashlib.sha256(value.encode()).hexdigest()[:48]}"

    @staticmethod
    def _limit_output(output: Any, limit: int) -> Any:
        if isinstance(output, str) and len(output) > limit:
            return output[:limit] + f"\n...[truncated {len(output) - limit} chars]"
        if not isinstance(output, str):
            serialized = json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if len(serialized) > limit:
                return {
                    "truncated": True,
                    "preview": serialized[:limit],
                    "original_chars": len(serialized),
                }
        return output

    def _finish(
        self,
        request: ToolExecutionRequest,
        status: ExecutionStatus,
        *,
        output: Any = None,
        error: str | None = None,
        policy_version: str | None = None,
        request_digest: str | None = None,
        approval_id: str | None = None,
    ) -> ToolExecutionResult:
        event_id = self.audit.append(
            AuditEvent(
                tenant_id=request.principal.tenant_id,
                event_type="tool.execution",
                actor_id=request.principal.principal_id,
                outcome=status.value,
                details={
                    "execution_id": request.execution_id,
                    "tool_name": request.tool_name,
                    "request_digest": request_digest,
                    "policy_version": policy_version,
                    "error": error,
                    "approval_id": approval_id,
                },
                correlation_id=request.correlation_id,
            )
        )
        return ToolExecutionResult(
            execution_id=request.execution_id,
            status=status,
            output=output,
            error=error,
            audit_event_id=event_id,
            request_digest=request_digest,
            approval_id=approval_id,
        )
