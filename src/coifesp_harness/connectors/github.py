from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..security import Classification
from ..tool_catalog import github_connector_manifests
from ..tool_jobs import (
    PermanentToolError,
    RetryableToolError,
    current_tool_execution_context,
)
from ..tools import (
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ReviewDisclosure,
    ToolDefinition,
)
from .client import ConnectorError, SecureConnectorClient
from .github_receipts import build_receipt
from .models import ConnectorRequest

GITHUB_ADAPTER_PATHS = frozenset(
    {
        "/v1/github/issues",
        "/v1/github/workflow-dispatches",
        "/v1/github/commit-checks",
    }
)


class GitHubTools:
    """Bounded GitHub Issue and Actions operations through an approved adapter."""

    def __init__(
        self,
        *,
        client: SecureConnectorClient,
        tenant_id: str | None = None,
        allowed_tenant_ids: frozenset[str] | None = None,
        classification: Classification,
    ) -> None:
        self.client = client
        if allowed_tenant_ids is None:
            allowed_tenant_ids = frozenset({tenant_id}) if tenant_id else frozenset()
        elif tenant_id is not None and allowed_tenant_ids != frozenset({tenant_id}):
            raise ValueError("GitHub tool tenant scope conflicts")
        if not allowed_tenant_ids:
            raise ValueError("GitHub tool tenant scope is required")
        self.tenant_id = tenant_id
        self.allowed_tenant_ids = allowed_tenant_ids
        self.classification = classification

    def definitions(self) -> tuple[ToolDefinition, ...]:
        manifests = {manifest.tool_id: manifest for manifest in github_connector_manifests()}
        create_issue = replace(
            manifests["github.create_issue"].declaration(self.create_issue),
            approval_review=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField("connector", "/connector_id", ReviewDisclosure.VALUE),
                    ApprovalReviewField("repository", "/repository", ReviewDisclosure.VALUE),
                    ApprovalReviewField("title", "/title", ReviewDisclosure.VALUE),
                    ApprovalReviewField("body_digest", "/body", ReviewDisclosure.HASH),
                    ApprovalReviewField("body_chars", "/body", ReviewDisclosure.COUNT),
                )
            ),
        )
        dispatch = replace(
            manifests["github.dispatch_workflow"].declaration(self.dispatch_workflow),
            approval_review=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField("connector", "/connector_id", ReviewDisclosure.VALUE),
                    ApprovalReviewField("repository", "/repository", ReviewDisclosure.VALUE),
                    ApprovalReviewField("workflow", "/workflow", ReviewDisclosure.VALUE),
                    ApprovalReviewField("ref", "/ref", ReviewDisclosure.VALUE),
                    ApprovalReviewField("inputs_digest", "/inputs", ReviewDisclosure.HASH),
                )
            ),
        )
        checks = manifests["github.get_commit_checks"].declaration(self.get_commit_checks)
        return create_issue, dispatch, checks

    async def create_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._execute(
            arguments=arguments,
            path="/v1/github/issues",
            body={
                "repository": arguments["repository"],
                "title": arguments["title"],
                "body": arguments["body"],
                "labels": arguments.get("labels", []),
            },
        )

    async def dispatch_workflow(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._execute(
            arguments=arguments,
            path="/v1/github/workflow-dispatches",
            body={
                "repository": arguments["repository"],
                "workflow": arguments["workflow"],
                "ref": arguments["ref"],
                "inputs": arguments.get("inputs", {}),
            },
        )

    async def get_commit_checks(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._execute(
            arguments=arguments,
            path="/v1/github/commit-checks",
            body={
                "repository": arguments["repository"],
                "commit_sha": arguments["commit_sha"],
            },
        )

    async def _execute(
        self, *, arguments: dict[str, Any], path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        context = current_tool_execution_context()
        if context.tenant_id not in self.allowed_tenant_ids:
            raise PermanentToolError("github_tenant_mismatch")
        try:
            response = await self.client.execute(
                ConnectorRequest(
                    connector_id=arguments["connector_id"],
                    tenant_id=context.tenant_id,
                    path=path,
                    body=body,
                    idempotency_key=context.idempotency_key,
                    classification=self.classification,
                )
            )
        except ConnectorError as exc:
            if exc.retryable:
                raise RetryableToolError(f"connector_{exc.code}") from exc
            raise PermanentToolError(f"connector_{exc.code}") from exc
        try:
            receipt = build_receipt(
                context=context, path=path, arguments=arguments, response=response,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise PermanentToolError("github_invalid_receipt") from exc
        return {
            "status_code": response.status_code,
            "provider_request_id": response.provider_request_id,
            "receipt": receipt,
        }
