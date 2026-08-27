import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.approvals import (
    ApprovalService,
    ApprovalStatus,
    ApprovalWorkflowError,
    SQLAlchemyApprovalRepository,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.errors import ResourceNotFound
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.security import (
    Classification,
    PolicyEngine,
    Principal,
    ResourceLabel,
    RiskLevel,
)
from coifesp_harness.tools import (
    Approval,
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ExecutionStatus,
    ReviewDisclosure,
    ToolDefinition,
    ToolExecutionRequest,
    ToolExecutor,
    ToolRegistry,
)


def stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyApprovalRepository(engine=engine)
    repository.create_schema()
    return ApprovalService(repository)


def request_digest(tool_name: str, arguments: dict) -> str:
    value = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def test_approval_enforces_separation_version_and_single_operation_consumption() -> None:
    service = stack()
    requester = Principal("alice", "team-a")
    approver = Principal("reviewer", "team-a", roles=frozenset({"tool_approver"}))
    digest = request_digest("send_external", {"value": "hello"})
    pending = service.request(
        principal=requester,
        approval_id="approval-1",
        tool_name="send_external",
        request_digest=digest,
        reason="Send the reviewed customer notification.",
    )
    assert pending.status is ApprovalStatus.PENDING
    assert len(pending.reason_digest) == 64
    duplicate = service.request(
        principal=requester,
        approval_id="approval-1",
        tool_name="send_external",
        request_digest=digest,
        reason="Send the reviewed customer notification.",
    )
    assert duplicate.version == pending.version
    with pytest.raises(ApprovalWorkflowError, match="different request content"):
        service.request(
            principal=requester,
            approval_id="approval-1",
            tool_name="send_external",
            request_digest=digest,
            reason="A different reason must conflict.",
        )
    assert service.list_pending(principal=approver) == (pending,)
    assert service.list_pending(principal=requester) == ()

    with pytest.raises(ApprovalWorkflowError, match="own operation"):
        service.decide(
            principal=Principal(
                "alice",
                "team-a",
                roles=frozenset({"tool_approver"}),
            ),
            approval_id="approval-1",
            approve=True,
            expected_version=1,
        )
    approved = service.decide(
        principal=approver,
        approval_id="approval-1",
        approve=True,
        expected_version=1,
    )
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.version == 2

    consumed = service.consume(
        principal=requester,
        approval_id="approval-1",
        tool_name="send_external",
        request_digest=digest,
        execution_id="execution-1",
    )
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumed_by_execution_id == "execution-1"
    replay = service.consume(
        principal=requester,
        approval_id="approval-1",
        tool_name="send_external",
        request_digest=digest,
        execution_id="execution-1",
    )
    assert replay.status is ApprovalStatus.CONSUMED
    with pytest.raises(ApprovalWorkflowError, match="already been consumed"):
        service.consume(
            principal=requester,
            approval_id="approval-1",
            tool_name="send_external",
            request_digest=digest,
            execution_id="execution-2",
        )
    with pytest.raises(ResourceNotFound, match="absent or hidden"):
        service.get(
            principal=Principal("mallory", "team-b"),
            approval_id="approval-1",
        )


def test_durable_approval_is_required_when_executor_has_an_approval_service() -> None:
    approval_service = stack()
    registry = ToolRegistry()

    async def handler(arguments):
        return f"sent:{arguments['value']}"

    registry.register(
        ToolDefinition(
            name="send_external",
            description="external side effect",
            handler=handler,
            parameters_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            risk=RiskLevel.HIGH,
            approval_review=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField(
                        "destination_value",
                        "/value",
                        ReviewDisclosure.VALUE,
                    ),
                )
            ),
        )
    )
    executor = ToolExecutor(
        registry=registry,
        policy=PolicyEngine(),
        audit=InMemoryAuditSink(),
        idempotency=InMemoryIdempotencyStore(),
        approval_service=approval_service,
    )
    requester = Principal("alice", "team-a")
    arguments = {"value": "hello"}
    digest = request_digest("send_external", arguments)
    inline = Approval(
        "inline",
        "team-a",
        "alice",
        "send_external",
        digest,
        "reviewer",
        datetime.now(UTC) + timedelta(minutes=5),
    )
    base = dict(
        execution_id="execution-1",
        idempotency_key="idem-1",
        correlation_id="corr-1",
        principal=requester,
        tool_name="send_external",
        arguments=arguments,
    )
    pending = asyncio.run(executor.execute(ToolExecutionRequest(**base, approval=inline)))
    assert pending.status is ExecutionStatus.APPROVAL_REQUIRED
    assert pending.request_digest == digest
    assert pending.approval_id is not None
    review = approval_service.get(
        principal=requester,
        approval_id=pending.approval_id,
    )
    assert review.origin == "tool_managed"
    assert review.review_projection["fields"][0]["value"] == "hello"
    approval_service.decide(
        principal=Principal(
            "reviewer",
            "team-a",
            roles=frozenset({"tool_approver"}),
        ),
        approval_id=pending.approval_id,
        approve=True,
        expected_version=1,
    )
    result = asyncio.run(
        executor.execute(ToolExecutionRequest(**base, approval_id=pending.approval_id))
    )
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output == "sent:hello"


def test_durable_executor_fails_closed_without_a_tool_owned_review_policy() -> None:
    approval_service = stack()
    registry = ToolRegistry()

    async def handler(_):
        return "unexpected"

    registry.register(
        ToolDefinition(
            name="unreviewable",
            description="high risk without a review projection",
            handler=handler,
            parameters_schema={"type": "object", "additionalProperties": False},
            risk=RiskLevel.HIGH,
        )
    )
    result = asyncio.run(
        ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=InMemoryAuditSink(),
            idempotency=InMemoryIdempotencyStore(),
            approval_service=approval_service,
        ).execute(
            ToolExecutionRequest(
                execution_id="unreviewable-1",
                idempotency_key="unreviewable-1",
                correlation_id="unreviewable-1",
                principal=Principal("alice", "team-a"),
                tool_name="unreviewable",
                arguments={},
            )
        )
    )
    assert result.status is ExecutionStatus.DENIED
    assert result.approval_id is None


def test_sensitive_approval_requires_matching_clearance_compartment_and_input_label() -> None:
    service = stack()
    requester = Principal(
        "alice",
        "team-a",
        clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    service.request(
        principal=requester,
        approval_id="approval-sensitive",
        tool_name="deploy",
        request_digest="b" * 64,
        reason="Deploy the compartmented release.",
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}),
    )
    with pytest.raises(ApprovalWorkflowError, match="clearance"):
        service.decide(
            principal=Principal(
                "reviewer",
                "team-a",
                roles=frozenset({"tool_approver"}),
                clearance=Classification.INTERNAL,
                compartments=frozenset({"project-x"}),
            ),
            approval_id="approval-sensitive",
            approve=True,
            expected_version=1,
        )
    service.decide(
        principal=Principal(
            "reviewer",
            "team-a",
            roles=frozenset({"tool_approver"}),
            clearance=Classification.CONFIDENTIAL,
            compartments=frozenset({"project-x"}),
        ),
        approval_id="approval-sensitive",
        approve=True,
        expected_version=1,
    )
    with pytest.raises(ApprovalWorkflowError, match="exact operation"):
        service.consume(
            principal=requester,
            approval_id="approval-sensitive",
            tool_name="deploy",
            request_digest="b" * 64,
            execution_id="execution-sensitive",
            input_label=ResourceLabel(
                "team-a",
                Classification.INTERNAL,
                frozenset({"project-x"}),
            ),
        )
