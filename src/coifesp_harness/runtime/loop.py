from __future__ import annotations

import copy
import json
import hashlib
from dataclasses import replace

from ..audit import AuditEvent, AuditSink
from ..context import ContextAssembler, ContextBudget
from ..security.policy import PolicyEngine
from ..skills import SkillCatalog
from ..tools.executor import ToolExecutor
from ..tools.models import ExecutionStatus, ToolExecutionRequest
from ..tools.registry import ToolRegistry
from ..tool_catalog import (
    RunScopedToolRegistry,
    build_project_context_run_tools,
    build_skill_run_tools,
)
from .models import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    Message,
    ModelProvider,
    ToolSpec,
)
from .providers.models import ModelCostBudgetExceeded


class AgentLoop:
    """A small model-independent loop with hard operational budgets.

    The loop is intentionally policy-agnostic: every action crosses ToolExecutor,
    where deterministic authorization and auditing are enforced.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        audit: AuditSink,
        context_assembler: ContextAssembler | None = None,
        durable_tools: bool = False,
        skill_catalog: SkillCatalog | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.executor = executor
        self.audit = audit
        self.context_assembler = context_assembler or ContextAssembler(
            policy=PolicyEngine(),
            audit=audit,
        )
        self.durable_tools = durable_tools
        self.skill_catalog = skill_catalog

    def _prepare_run_scope(self, request: AgentRunRequest) -> tuple[ToolRegistry, ToolExecutor]:
        """Build the per-run tool view from the immutable authorization snapshot.

        A missing authorization snapshot is treated as an empty authorization,
        so legacy runs cannot inherit tools added later. With a snapshot, only
        the tools chosen at run creation resolve. Run-scoped skill handlers are
        attached here so
        ``load_skill`` can never drift past the pinned versions.
        """
        authorization = request.tool_authorization
        if authorization is None:
            scoped = RunScopedToolRegistry(base=self.registry, allowed_tools=frozenset())
            executor = copy.copy(self.executor)
            executor.registry = scoped
            return scoped, executor
        extra: tuple = ()
        if (
            self.skill_catalog is not None
            and authorization.skills
            and "load_skill" in authorization.allowed_tools
        ) or (self.skill_catalog is not None and "list_skills" in authorization.allowed_tools):
            extra = build_skill_run_tools(
                catalog=self.skill_catalog,
                principal=request.principal,
                bindings=authorization.skill_bindings,
                available_tools=authorization.allowed_tools,
            )
        if authorization.allowed_tools.intersection(
            {"project.list_context", "project.read_context"}
        ):
            extra += build_project_context_run_tools(request.context_items)
        registry = RunScopedToolRegistry(
            base=self.registry,
            allowed_tools=authorization.allowed_tools,
            extra=extra,
        )
        executor = copy.copy(self.executor)
        executor.registry = registry
        return registry, executor

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        registry, executor = self._prepare_run_scope(request)
        messages = list(request.messages)
        events: list[AgentEvent] = []
        turns = request.resume_usage.turns if request.resume_usage else 0
        tool_calls = request.resume_usage.tool_calls if request.resume_usage else 0
        total_tokens = request.resume_usage.total_tokens if request.resume_usage else 0
        model_cost_microusd = (
            request.resume_usage.model_cost_microusd if request.resume_usage else 0
        )
        control_cursor = request.control_cursor
        applied_control_sequences: list[int] = []

        def emit(event_type: str, **data: object) -> None:
            events.append(
                AgentEvent(
                    event_type=event_type,
                    run_id=request.run_id,
                    sequence=len(events) + 1,
                    data=dict(data),
                )
            )

        def finish(status: str) -> AgentRunResult:
            self.audit.append(
                AuditEvent(
                    tenant_id=request.principal.tenant_id,
                    event_type="agent.run",
                    actor_id=request.principal.principal_id,
                    outcome=status,
                    details={
                        "run_id": request.run_id,
                        "turns": turns,
                        "tool_calls": tool_calls,
                        "total_tokens": total_tokens,
                        "model_cost_microusd": model_cost_microusd,
                    },
                    correlation_id=request.correlation_id,
                )
            )
            return AgentRunResult(
                run_id=request.run_id,
                status=status,
                messages=tuple(messages),
                turns=turns,
                tool_calls=tool_calls,
                total_tokens=total_tokens,
                model_cost_microusd=model_cost_microusd,
                events=tuple(events),
                control_cursor=control_cursor,
                applied_control_sequences=tuple(applied_control_sequences),
            )

        async def pull_control() -> bool:
            nonlocal control_cursor
            if request.control_source is None:
                return False
            directives = await request.control_source.poll(after_sequence=control_cursor)
            previous = control_cursor
            for directive in directives:
                if (
                    directive.sequence <= previous
                    or directive.command_type not in {"steer", "follow_up"}
                    or not directive.command_id
                    or not directive.content
                    or len(directive.content.encode("utf-8")) > 65_536
                ):
                    raise ValueError("runtime control directive is invalid")
                messages.append(
                    Message(
                        role="user",
                        content=json.dumps(
                            {
                                "schema": "coifesp.agent-control.v1",
                                "instruction_trust": "user_instruction",
                                "sequence": directive.sequence,
                                "command_id": directive.command_id,
                                "command_type": directive.command_type,
                                "content": directive.content,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                )
                previous = directive.sequence
                control_cursor = directive.sequence
                applied_control_sequences.append(directive.sequence)
                emit(
                    "agent.control_applied",
                    sequence=directive.sequence,
                    command_type=directive.command_type,
                )
            return bool(directives)

        emit("agent.started")
        for binding in request.approval_bindings:
            call, tool_message_index = self._pending_call(messages, binding.call_id)
            emit(
                "approval.resuming",
                call_id=call.call_id,
                approval_id=binding.approval_id,
            )
            resumed_request = ToolExecutionRequest(
                execution_id=self._execution_id(request.run_id, call.call_id),
                idempotency_key=f"{request.run_id}:{call.call_id}",
                correlation_id=request.correlation_id,
                principal=request.principal,
                tool_name=call.name,
                arguments=call.arguments,
                approval_id=binding.approval_id,
            )
            resumed = (
                executor.prepare_dispatch(resumed_request)
                if self.durable_tools
                else await executor.execute(resumed_request)
            )
            messages[tool_message_index] = self._tool_message(call, resumed)
            emit(
                "tool.completed",
                call_id=call.call_id,
                tool_name=call.name,
                status=resumed.status.value,
                approval_id=resumed.approval_id,
            )
            if resumed.status is ExecutionStatus.APPROVAL_REQUIRED:
                emit(
                    "agent.awaiting_approval",
                    call_id=call.call_id,
                    approval_id=resumed.approval_id,
                )
                return finish("awaiting_approval")
            if resumed.status is ExecutionStatus.DENIED:
                emit("agent.denied", call_id=call.call_id)
                return finish("denied")

        if self.durable_tools:
            pending = self._first_tool_message(messages, ExecutionStatus.APPROVAL_REQUIRED)
            if pending is not None:
                call_id, approval_id = pending
                emit("agent.awaiting_approval", call_id=call_id, approval_id=approval_id)
                return finish("awaiting_approval")
            dispatches = self._tool_message_ids(messages, ExecutionStatus.DISPATCH_REQUIRED)
            if dispatches:
                emit("agent.awaiting_tool", call_ids=dispatches)
                return finish("awaiting_tool")

        while True:
            if turns >= request.budget.max_turns:
                emit("agent.failed", error_code="turn_budget_exceeded")
                return finish("failed")
            if model_cost_microusd >= request.budget.max_model_cost_microusd:
                emit("agent.failed", error_code="model_cost_budget_exceeded")
                return finish("failed")
            await pull_control()
            turns += 1
            emit("turn.started", turn=turns)
            tool_specs = tuple(
                ToolSpec(
                    name=item.name,
                    description=item.description,
                    parameters_schema=item.parameters_schema,
                )
                for item in registry.definitions()
            )
            assembler = self.context_assembler
            prepare_counter = getattr(self.provider, "prepare_context_counter", None)
            if callable(prepare_counter):
                assembler = assembler.with_counter(
                    prepare_counter(route_policy=request.model_route_policy, tools=tool_specs)
                )
            tool_overhead_tokens = assembler.counter.count_text(
                json.dumps(
                    [
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "parameters": spec.parameters_schema,
                        }
                        for spec in tool_specs
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            assembled = assembler.assemble(
                principal=request.principal,
                correlation_id=request.correlation_id,
                purpose=request.context_purpose,
                conversation=tuple(messages),
                items=request.context_items,
                budget=request.context_budget or ContextBudget(),
                fixed_overhead_tokens=tool_overhead_tokens,
            )
            emit(
                "context.assembled",
                included_item_count=len(assembled.manifest),
                excluded_item_count=len(assembled.excluded),
                compacted_item_count=len(assembled.compacted_item_ids),
                estimated_input_tokens=assembled.estimated_input_tokens,
            )
            routed_complete = getattr(self.provider, "complete_routed", None)
            if callable(routed_complete):
                remaining_cost = request.budget.max_model_cost_microusd - model_cost_microusd
                remaining_tokens = request.budget.max_total_tokens - total_tokens
                configured_ceiling = request.model_route_policy.max_call_cost_microusd
                configured_token_ceiling = request.model_route_policy.max_call_total_tokens
                route_policy = replace(
                    request.model_route_policy,
                    max_call_cost_microusd=(
                        remaining_cost
                        if configured_ceiling is None
                        else min(remaining_cost, configured_ceiling)
                    ),
                    max_call_total_tokens=(
                        remaining_tokens
                        if configured_token_ceiling is None
                        else min(remaining_tokens, configured_token_ceiling)
                    ),
                )
                try:
                    response = await routed_complete(
                        messages=assembled.messages,
                        tools=tool_specs,
                        correlation_id=request.correlation_id,
                        route_policy=route_policy,
                    )
                except ModelCostBudgetExceeded:
                    emit("agent.failed", error_code="model_cost_budget_exceeded")
                    return finish("failed")
            else:
                response = await self.provider.complete(
                    messages=assembled.messages,
                    tools=tool_specs,
                    correlation_id=request.correlation_id,
                )
            total_tokens += response.input_tokens + response.output_tokens
            model_cost_microusd += response.cost_microusd
            if total_tokens > request.budget.max_total_tokens:
                emit("agent.failed", error_code="token_budget_exceeded")
                return finish("failed")
            if model_cost_microusd > request.budget.max_model_cost_microusd:
                emit("agent.failed", error_code="model_cost_budget_exceeded")
                return finish("failed")
            messages.append(
                Message(
                    role="assistant",
                    content=response.text,
                    tool_calls=response.tool_calls,
                )
            )
            emit(
                "model.completed",
                turn=turns,
                tool_call_count=len(response.tool_calls),
                provider_request_id=response.provider_request_id,
                provider_id=response.provider_id,
                model=response.model,
                finish_reason=response.finish_reason,
                cost_microusd=response.cost_microusd,
            )
            if response.finish_reason == "length":
                emit("agent.failed", error_code="model_output_truncated")
                return finish("failed")

            if not response.tool_calls:
                if turns < request.budget.max_turns and await pull_control():
                    emit("turn.completed", turn=turns)
                    continue
                emit("agent.completed", turn=turns)
                self.audit.append(
                    AuditEvent(
                        tenant_id=request.principal.tenant_id,
                        event_type="agent.run",
                        actor_id=request.principal.principal_id,
                        outcome="completed",
                        details={
                            "run_id": request.run_id,
                            "turns": turns,
                            "tool_calls": tool_calls,
                            "total_tokens": total_tokens,
                            "model_cost_microusd": model_cost_microusd,
                        },
                        correlation_id=request.correlation_id,
                    )
                )
                return AgentRunResult(
                    run_id=request.run_id,
                    status="completed",
                    messages=tuple(messages),
                    turns=turns,
                    tool_calls=tool_calls,
                    total_tokens=total_tokens,
                    model_cost_microusd=model_cost_microusd,
                    events=tuple(events),
                    control_cursor=control_cursor,
                    applied_control_sequences=tuple(applied_control_sequences),
                )

            if tool_calls + len(response.tool_calls) > request.budget.max_tool_calls:
                emit("agent.failed", error_code="tool_call_budget_exceeded")
                return finish("failed")

            pending_approvals: list[tuple[str, str]] = []
            for call in response.tool_calls:
                tool_calls += 1
                emit("tool.requested", call_id=call.call_id, tool_name=call.name)
                definition = registry.get(call.name)
                execution_request = ToolExecutionRequest(
                    execution_id=self._execution_id(request.run_id, call.call_id),
                    idempotency_key=f"{request.run_id}:{call.call_id}",
                    correlation_id=request.correlation_id,
                    principal=request.principal,
                    tool_name=call.name,
                    arguments=call.arguments,
                )
                if (
                    self.durable_tools
                    and definition is not None
                    and (getattr(definition, "executor", "tool_worker") == "agent_worker")
                ):
                    # Run-scoped, read-only tools execute in-process; the full
                    # executor boundary (policy, validation, idempotency,
                    # audit) still applies before the handler runs.
                    result = await executor.execute(execution_request)
                else:
                    result = (
                        executor.prepare_dispatch(execution_request)
                        if self.durable_tools
                        else await executor.execute(execution_request)
                    )
                messages.append(self._tool_message(call, result))
                emit(
                    "tool.completed",
                    call_id=call.call_id,
                    tool_name=call.name,
                    status=result.status.value,
                    approval_id=result.approval_id,
                )
                if result.status is ExecutionStatus.APPROVAL_REQUIRED:
                    if result.approval_id is None:
                        raise ValueError("approval-required result lacks approval ID")
                    pending_approvals.append((call.call_id, result.approval_id))
                if result.status is ExecutionStatus.DISPATCH_REQUIRED:
                    continue
                if result.status in {
                    ExecutionStatus.APPROVAL_REQUIRED,
                    ExecutionStatus.DENIED,
                }:
                    # The model may explain the gate on the next turn, but it cannot bypass it.
                    continue
            if pending_approvals:
                call_id, approval_id = pending_approvals[0]
                emit("agent.awaiting_approval", call_id=call_id, approval_id=approval_id)
                return finish("awaiting_approval")
            if self.durable_tools and any(
                self._tool_message_status(message) is ExecutionStatus.DISPATCH_REQUIRED
                for message in messages
                if message.role == "tool"
            ):
                emit(
                    "agent.awaiting_tool",
                    call_ids=[call.call_id for call in response.tool_calls],
                )
                return finish("awaiting_tool")
            emit("turn.completed", turn=turns)

    @staticmethod
    def _execution_id(run_id: str, call_id: str) -> str:
        digest = hashlib.sha256(f"{run_id}\0{call_id}".encode()).hexdigest()
        return f"execution-{digest[:48]}"

    @staticmethod
    def _tool_message(call, result) -> Message:
        payload = {
            "status": result.status.value,
            "output": result.output,
            "error": result.error,
            "approval_id": result.approval_id,
            "request_digest": result.request_digest,
        }
        return Message(
            role="tool",
            name=call.name,
            tool_call_id=call.call_id,
            content=json.dumps(
                {
                    "schema": "coifesp.tool-output.v1",
                    "instruction_trust": "data_only",
                    "payload": payload,
                },
                ensure_ascii=False,
                default=str,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _pending_call(messages: list[Message], call_id: str):
        call = None
        for message in messages:
            if message.role == "assistant":
                for candidate in message.tool_calls:
                    if candidate.call_id == call_id:
                        call = candidate
        if call is None:
            raise ValueError("approval binding references an unknown tool call")
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.role != "tool" or message.tool_call_id != call_id:
                continue
            try:
                value = json.loads(message.content)
                status = value["payload"]["status"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("pending tool checkpoint is malformed") from exc
            if status != ExecutionStatus.APPROVAL_REQUIRED.value:
                raise ValueError("approval binding does not reference a pending tool call")
            return call, index
        raise ValueError("approval binding has no pending tool checkpoint")

    @staticmethod
    def _tool_message_status(message: Message) -> ExecutionStatus | None:
        try:
            value = json.loads(message.content)
            return ExecutionStatus(value["payload"]["status"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _first_tool_message(
        messages: list[Message], status: ExecutionStatus
    ) -> tuple[str, str] | None:
        for message in messages:
            if message.role != "tool" or AgentLoop._tool_message_status(message) is not status:
                continue
            value = json.loads(message.content)
            approval_id = value["payload"].get("approval_id")
            if message.tool_call_id and isinstance(approval_id, str) and approval_id:
                return message.tool_call_id, approval_id
        return None

    @staticmethod
    def _tool_message_ids(messages: list[Message], status: ExecutionStatus) -> list[str]:
        return [
            message.tool_call_id
            for message in messages
            if message.role == "tool"
            and message.tool_call_id is not None
            and AgentLoop._tool_message_status(message) is status
        ]
