import asyncio
import json

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.approvals import ApprovalService, SQLAlchemyApprovalRepository
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.runtime import (
    AgentLoop,
    AuthorizedTool,
    AgentRunRequest,
    ApprovalBinding,
    LLMResponse,
    Message,
    RunUsage,
    ToolAuthorization,
    ToolCall,
)
from coifesp_harness.security import PolicyEngine, Principal, RiskLevel
from coifesp_harness.tools import (
    ApprovalReviewField,
    ApprovalReviewPolicy,
    ReviewDisclosure,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
)

SEND_EXTERNAL_AUTHORIZATION = ToolAuthorization(
    tools=(AuthorizedTool("send_external", "1", "test-schema"),)
)


class ApprovalProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages, tools, correlation_id):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                tool_calls=(
                    ToolCall(
                        "call-send",
                        "send_external",
                        {"destination": "customer-42", "body": "hello"},
                    ),
                ),
                input_tokens=10,
                output_tokens=5,
            )
        assert json.loads(messages[-1].content)["payload"]["status"] == "succeeded"
        return LLMResponse(text="delivery completed", input_tokens=10, output_tokens=3)


def test_agent_run_pauses_and_resumes_the_exact_approved_tool_call() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    approval_repository = SQLAlchemyApprovalRepository(engine=engine)
    approval_repository.create_schema()
    approvals = ApprovalService(approval_repository)
    registry = ToolRegistry()
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return {"delivered": True}

    registry.register(
        ToolDefinition(
            name="send_external",
            description="send an external message",
            handler=handler,
            parameters_schema={
                "type": "object",
                "properties": {
                    "destination": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["destination", "body"],
                "additionalProperties": False,
            },
            risk=RiskLevel.HIGH,
            approval_review=ApprovalReviewPolicy(
                fields=(
                    ApprovalReviewField(
                        "destination",
                        "/destination",
                        ReviewDisclosure.VALUE,
                    ),
                    ApprovalReviewField("body", "/body", ReviewDisclosure.HASH),
                )
            ),
        )
    )
    audit = InMemoryAuditSink()
    provider = ApprovalProvider()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
            approval_service=approvals,
        ),
        audit=audit,
    )
    requester = Principal("operator", "team-a")
    initial = asyncio.run(
        loop.run(
            AgentRunRequest(
                run_id="run-approval",
                correlation_id="corr-approval",
                principal=requester,
                messages=(Message("user", "send the reviewed notification"),),
                tool_authorization=SEND_EXTERNAL_AUTHORIZATION,
            )
        )
    )
    assert initial.status == "awaiting_approval"
    assert provider.calls == 1
    assert calls == []
    checkpoint = json.loads(initial.messages[-1].content)
    approval_id = checkpoint["payload"]["approval_id"]
    approval = approvals.get(principal=requester, approval_id=approval_id)
    assert approval.review_projection["fields"][0]["value"] == "customer-42"
    assert "hello" not in json.dumps(approval.review_projection)

    approvals.decide(
        principal=Principal(
            "reviewer",
            "team-a",
            roles=frozenset({"tool_approver"}),
        ),
        approval_id=approval_id,
        approve=True,
        expected_version=approval.version,
    )
    resumed = asyncio.run(
        loop.run(
            AgentRunRequest(
                run_id="run-approval",
                correlation_id="corr-approval",
                principal=requester,
                messages=initial.messages,
                tool_authorization=SEND_EXTERNAL_AUTHORIZATION,
                approval_bindings=(ApprovalBinding("call-send", approval_id),),
                resume_usage=RunUsage(
                    turns=initial.turns,
                    tool_calls=initial.tool_calls,
                    total_tokens=initial.total_tokens,
                ),
            )
        )
    )
    assert resumed.status == "completed"
    assert provider.calls == 2
    assert calls == [{"destination": "customer-42", "body": "hello"}]
    assert any(event.event_type == "approval.resuming" for event in resumed.events)


def test_durable_batch_collects_multiple_approvals_before_dispatch() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    approval_repository = SQLAlchemyApprovalRepository(engine=engine)
    approval_repository.create_schema()
    approvals = ApprovalService(approval_repository)
    registry = ToolRegistry()

    async def forbidden_inline_handler(_):
        raise AssertionError("durable preflight must not execute a handler")

    registry.register(
        ToolDefinition(
            name="send_external",
            description="send an external message",
            handler=forbidden_inline_handler,
            parameters_schema={
                "type": "object",
                "properties": {"destination": {"type": "string"}, "body": {"type": "string"}},
                "required": ["destination", "body"],
                "additionalProperties": False,
            },
            risk=RiskLevel.HIGH,
            approval_review=ApprovalReviewPolicy(
                fields=(ApprovalReviewField("destination", "/destination", ReviewDisclosure.HASH),)
            ),
        )
    )

    class TwoCallProvider:
        async def complete(self, **_):
            return LLMResponse(
                tool_calls=(
                    ToolCall("call-1", "send_external", {"destination": "a", "body": "one"}),
                    ToolCall("call-2", "send_external", {"destination": "b", "body": "two"}),
                )
            )

    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=TwoCallProvider(),
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
            approval_service=approvals,
        ),
        audit=audit,
        durable_tools=True,
    )
    principal = Principal("operator", "team-a")
    first = asyncio.run(
        loop.run(
            AgentRunRequest(
                run_id="run-multi-approval",
                correlation_id="corr-multi-approval",
                principal=principal,
                messages=(Message("user", "send both"),),
                tool_authorization=SEND_EXTERNAL_AUTHORIZATION,
            )
        )
    )
    assert first.status == "awaiting_approval"
    payloads = [
        json.loads(message.content)["payload"]
        for message in first.messages
        if message.role == "tool"
    ]
    assert len(payloads) == 2
    first_id, second_id = (payload["approval_id"] for payload in payloads)

    for approval_id in (first_id, second_id):
        approval = approvals.get(principal=principal, approval_id=approval_id)
        approvals.decide(
            principal=Principal("reviewer", "team-a", roles=frozenset({"tool_approver"})),
            approval_id=approval_id,
            approve=True,
            expected_version=approval.version,
        )

    after_first = asyncio.run(
        loop.run(
            AgentRunRequest(
                run_id="run-multi-approval",
                correlation_id="corr-multi-approval",
                principal=principal,
                messages=first.messages,
                tool_authorization=SEND_EXTERNAL_AUTHORIZATION,
                approval_bindings=(ApprovalBinding("call-1", first_id),),
                resume_usage=RunUsage(first.turns, first.tool_calls, first.total_tokens),
            )
        )
    )
    assert after_first.status == "awaiting_approval"
    assert any(
        event.data.get("call_id") == "call-2"
        for event in after_first.events
        if event.event_type == "agent.awaiting_approval"
    )

    after_second = asyncio.run(
        loop.run(
            AgentRunRequest(
                run_id="run-multi-approval",
                correlation_id="corr-multi-approval",
                principal=principal,
                messages=after_first.messages,
                tool_authorization=SEND_EXTERNAL_AUTHORIZATION,
                approval_bindings=(ApprovalBinding("call-2", second_id),),
                resume_usage=RunUsage(
                    after_first.turns, after_first.tool_calls, after_first.total_tokens
                ),
            )
        )
    )
    assert after_second.status == "awaiting_tool"
    assert after_second.tool_calls == 2
    assert all(
        json.loads(message.content)["payload"]["status"] == "dispatch_required"
        for message in after_second.messages
        if message.role == "tool"
    )
