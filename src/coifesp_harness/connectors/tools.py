from __future__ import annotations

import re
from typing import Any

from ..security import Classification, RiskLevel
from ..tool_jobs import PermanentToolError, RetryableToolError, current_tool_execution_context
from ..tools import (
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ReviewDisclosure,
    ToolDefinition,
)
from .client import ConnectorError, SecureConnectorClient
from .models import ConnectorRequest

_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")


class OfficeMessageTools:
    def __init__(
        self,
        *,
        client: SecureConnectorClient,
        tenant_id: str,
        classification: Classification,
    ) -> None:
        self.client = client
        self.tenant_id = tenant_id
        self.classification = classification

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="office.send_message",
            description=(
                "Send a reviewed message through a tenant-owned, administrator-registered "
                "office connector. Cross-team disclosure must be approved separately."
            ),
            handler=self.send_message,
            parameters_schema={
                "type": "object",
                "properties": {
                    "connector_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,63}$"},
                    "target": {"type": "string", "minLength": 1, "maxLength": 256},
                    "text": {"type": "string", "minLength": 1, "maxLength": 100000},
                },
                "required": ["connector_id", "target", "text"],
                "additionalProperties": False,
            },
            required_roles=frozenset({"contributor"}),
            risk=RiskLevel.HIGH,
            timeout_seconds=120,
            max_output_chars=50_000,
            validator=self._validate,
            approval_review=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField("connector", "/connector_id", ReviewDisclosure.VALUE),
                    ApprovalReviewField("target", "/target", ReviewDisclosure.VALUE),
                    ApprovalReviewField("text_digest", "/text", ReviewDisclosure.HASH),
                    ApprovalReviewField("text_chars", "/text", ReviewDisclosure.COUNT),
                )
            ),
        )

    async def send_message(self, arguments: dict[str, Any]) -> dict[str, Any]:
        context = current_tool_execution_context()
        try:
            response = await self.client.execute(
                ConnectorRequest(
                    connector_id=arguments["connector_id"],
                    tenant_id=self.tenant_id,
                    path="/v1/messages",
                    body={"target": arguments["target"], "text": arguments["text"]},
                    idempotency_key=context.idempotency_key,
                    classification=self.classification,
                )
            )
        except ConnectorError as exc:
            if exc.retryable:
                raise RetryableToolError(f"connector_{exc.code}") from exc
            raise PermanentToolError(f"connector_{exc.code}") from exc
        return {
            "status_code": response.status_code,
            "provider_request_id": response.provider_request_id,
            "provider_result": response.body,
        }

    @staticmethod
    def _validate(arguments: dict[str, Any]) -> None:
        if not _TARGET.fullmatch(arguments["target"]):
            raise ValueError("office message target is invalid")
