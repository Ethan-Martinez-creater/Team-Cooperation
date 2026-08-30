from __future__ import annotations

from datetime import datetime
from typing import Any

from ..context import (
    ContentTrust,
    ContextBudget,
    ContextItem,
    ContextSource,
    InstructionTrust,
)
from ..errors import IntegrityError
from ..runtime import (
    AgentRunRequest,
    AgentRunResult,
    ApprovalBinding,
    AuthorizedSkill,
    AuthorizedTool,
    Message,
    ModelCapability,
    ModelRoutePolicy,
    RunBudget,
    RuntimeControlSource,
    RunUsage,
    ToolAuthorization,
    ToolCall,
)
from ..security import Classification, DisclosureGrant, Principal, ResourceLabel
from .models import AgentRunLease

CHECKPOINT_SCHEMA = "coifesp.agent-run-checkpoint.v1"
_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_ORCHESTRATOR_PRINCIPAL_ID = "service:project-orchestrator"
_TEAM_AGENT_PREFIX = "team-agent:"


class AgentRunCheckpointCodec:
    """Strict, versioned conversion between durable JSON and runtime objects."""

    def initial(self, request: AgentRunRequest) -> dict[str, Any]:
        usage = request.resume_usage or RunUsage(0, 0, 0)
        return self._encode(
            messages=request.messages,
            budget=request.budget,
            usage=usage,
            approval_bindings=request.approval_bindings,
            context_items=request.context_items,
            context_budget=request.context_budget,
            context_purpose=request.context_purpose,
            control_cursor=request.control_cursor,
            model_route_policy=request.model_route_policy,
            tool_authorization=self._authorization_json(request.tool_authorization),
        )

    def result(
        self,
        *,
        request: AgentRunRequest,
        result: AgentRunResult,
    ) -> dict[str, Any]:
        return self._encode(
            messages=result.messages,
            budget=request.budget,
            usage=RunUsage(
                result.turns,
                result.tool_calls,
                result.total_tokens,
                result.model_cost_microusd,
            ),
            approval_bindings=(),
            context_items=request.context_items,
            context_budget=request.context_budget,
            context_purpose=request.context_purpose,
            control_cursor=result.control_cursor,
            model_route_policy=request.model_route_policy,
            tool_authorization=self._authorization_json(request.tool_authorization),
        )

    def request(
        self,
        *,
        lease: AgentRunLease,
        principal: Principal,
        control_source: RuntimeControlSource | None = None,
        verified: bool = False,
    ) -> AgentRunRequest:
        if not isinstance(verified, bool):
            raise IntegrityError("service verification flag is invalid")
        principal_id = principal.principal_id
        canonical_service_id = isinstance(principal_id, str) and (
            principal_id == _ORCHESTRATOR_PRINCIPAL_ID
            or principal_id.startswith(_TEAM_AGENT_PREFIX)
        )
        service_owner_verified = (
            principal.is_service is True
            and verified
            and (
                principal_id == _ORCHESTRATOR_PRINCIPAL_ID
                or (
                    isinstance(principal_id, str)
                    and principal_id.startswith(_TEAM_AGENT_PREFIX)
                    and principal_id[
                        len(_TEAM_AGENT_PREFIX) :
                    ]
                    == principal.tenant_id
                )
            )
        )
        if (
            principal.tenant_id != lease.run.tenant_id
            or principal.principal_id != lease.run.owner_principal_id
            or (canonical_service_id and not principal.is_service)
            or (principal.is_service is True and not service_owner_verified)
        ):
            raise IntegrityError("resolved run principal does not match the durable owner")
        value = self.decode(lease.checkpoint)
        usage = value["usage"]
        if usage != RunUsage(
            lease.run.turns,
            lease.run.tool_calls,
            lease.run.total_tokens,
            lease.run.model_cost_microusd,
        ):
            raise IntegrityError("checkpoint usage differs from durable run counters")
        return AgentRunRequest(
            run_id=lease.run.run_id,
            correlation_id=lease.run.correlation_id,
            principal=principal,
            messages=value["messages"],
            budget=value["budget"],
            context_items=value["context_items"],
            context_budget=value["context_budget"],
            context_purpose=value["context_purpose"],
            approval_bindings=value["approval_bindings"],
            resume_usage=usage,
            control_cursor=value["control_cursor"],
            control_source=control_source,
            model_route_policy=value["model_route_policy"],
            tool_authorization=value["tool_authorization"],
        )

    def validate(
        self,
        checkpoint: dict[str, Any],
        *,
        usage: RunUsage,
    ) -> None:
        value = self.decode(checkpoint)
        if value["usage"] != usage:
            raise IntegrityError("checkpoint usage does not match durable counters")

    def decode(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(checkpoint) - {
                "schema",
                "messages",
                "budget",
                "usage",
                "approval_bindings",
                "context",
                "control_cursor",
                "model_route_policy",
                "tool_authorization",
            }:
                raise ValueError("checkpoint contains unknown fields")
            if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
                raise ValueError("checkpoint schema is invalid")
            messages_raw = self._list(checkpoint.get("messages"), "messages", 10_000)
            messages = tuple(self._message(item) for item in messages_raw)
            budget = self._budget(self._dict(checkpoint.get("budget"), "budget"))
            usage = self._usage(self._dict(checkpoint.get("usage"), "usage"))
            bindings_raw = self._list(
                checkpoint.get("approval_bindings", []),
                "approval_bindings",
                1_000,
            )
            bindings = tuple(self._binding(item) for item in bindings_raw)
            if len({item.call_id for item in bindings}) != len(bindings):
                raise ValueError("approval binding call IDs are not unique")
            context = self._dict(checkpoint.get("context", {}), "context")
            if set(context) - {"purpose", "budget", "items"}:
                raise ValueError("context contains unknown fields")
            purpose = context.get("purpose", "agent.run")
            if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 256:
                raise ValueError("context purpose is invalid")
            context_budget = (
                None
                if context.get("budget") is None
                else self._context_budget(self._dict(context["budget"], "context budget"))
            )
            context_items = tuple(
                self._context_item(item)
                for item in self._list(context.get("items", []), "context items", 256)
            )
            control_cursor = self._integer(
                checkpoint.get("control_cursor", 0),
                "control cursor",
            )
            if control_cursor < 0:
                raise ValueError("control cursor is invalid")
            model_route_policy = self._model_route_policy(
                self._dict(checkpoint.get("model_route_policy", {}), "model route policy")
            )
            tool_authorization = self._tool_authorization(
                checkpoint.get("tool_authorization")
            )
            return {
                "messages": messages,
                "budget": budget,
                "usage": usage,
                "approval_bindings": bindings,
                "context_items": context_items,
                "context_budget": context_budget,
                "context_purpose": purpose,
                "control_cursor": control_cursor,
                "model_route_policy": model_route_policy,
                "tool_authorization": tool_authorization,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityError("durable agent checkpoint is invalid") from exc

    def _encode(
        self,
        *,
        messages: tuple[Message, ...],
        budget: RunBudget,
        usage: RunUsage,
        approval_bindings: tuple[ApprovalBinding, ...],
        context_items: tuple[ContextItem, ...],
        context_budget: ContextBudget | None,
        context_purpose: str,
        control_cursor: int,
        model_route_policy: ModelRoutePolicy,
        tool_authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "schema": CHECKPOINT_SCHEMA,
            "messages": [self._message_json(item) for item in messages],
            "budget": {
                "max_turns": budget.max_turns,
                "max_tool_calls": budget.max_tool_calls,
                "max_total_tokens": budget.max_total_tokens,
                "max_model_cost_microusd": budget.max_model_cost_microusd,
            },
            "usage": {
                "turns": usage.turns,
                "tool_calls": usage.tool_calls,
                "total_tokens": usage.total_tokens,
                "model_cost_microusd": usage.model_cost_microusd,
            },
            "approval_bindings": [
                {"call_id": item.call_id, "approval_id": item.approval_id}
                for item in approval_bindings
            ],
            "context": {
                "purpose": context_purpose,
                "budget": (
                    None
                    if context_budget is None
                    else {
                        "max_input_tokens": context_budget.max_input_tokens,
                        "reserved_output_tokens": context_budget.reserved_output_tokens,
                        "max_item_tokens": context_budget.max_item_tokens,
                        "max_items": context_budget.max_items,
                    }
                ),
                "items": [self._context_item_json(item) for item in context_items],
            },
            "control_cursor": control_cursor,
            "model_route_policy": {
                "data_classification": int(model_route_policy.data_classification),
                "required_capabilities": sorted(
                    capability.value for capability in model_route_policy.required_capabilities
                ),
                "allowed_provider_ids": sorted(model_route_policy.allowed_provider_ids),
                "residency_regions": sorted(model_route_policy.residency_regions),
                "allow_external_egress": model_route_policy.allow_external_egress,
                "max_call_cost_microusd": model_route_policy.max_call_cost_microusd,
                "max_output_tokens": model_route_policy.max_output_tokens,
                "max_call_total_tokens": model_route_policy.max_call_total_tokens,
            },
        }
        if tool_authorization is not None:
            result["tool_authorization"] = tool_authorization
        return result

    def _tool_authorization(self, value: Any) -> ToolAuthorization | None:
        """Decode the run-scoped tool/skill authorization snapshot.

        A missing block keeps the authorization unset so pre-iteration-2 runs
        stay compatible; once present the block is strict: tools carry version
        and schema digests, skills are pinned to exact versions with content
        digests, and the authorization is immutable for the lifetime of the run.
        """
        if value is None:
            return None
        block = self._dict(value, "tool authorization")
        if set(block) - {"catalog_digest", "tools", "skills"}:
            raise ValueError("tool authorization fields are invalid")
        digest_value = block.get("catalog_digest")
        catalog_digest = (
            None
            if digest_value is None
            else self._required_string(digest_value, "tool catalog digest", max_length=128)
        )
        tools = []
        for item in self._list(block.get("tools", []), "authorized tools", 64):
            entry = self._dict(item, "authorized tool")
            if set(entry) - {"tool_id", "version", "schema_digest"}:
                raise ValueError("authorized tool fields are invalid")
            tools.append(
                AuthorizedTool(
                    tool_id=self._required_string(entry["tool_id"], "authorized tool ID"),
                    version=self._required_string(
                        entry["version"], "authorized tool version", max_length=32
                    ),
                    schema_digest=self._required_string(
                        entry["schema_digest"], "authorized tool schema digest", max_length=128
                    ),
                )
            )
        skills = []
        for item in self._list(block.get("skills", []), "authorized skills", 64):
            entry = self._dict(item, "authorized skill")
            if set(entry) - {"name", "version", "content_digest"}:
                raise ValueError("authorized skill fields are invalid")
            skills.append(
                AuthorizedSkill(
                    name=self._required_string(
                        entry["name"], "authorized skill name", max_length=64
                    ),
                    version=self._required_string(
                        entry["version"], "authorized skill version", max_length=128
                    ),
                    content_digest=self._required_string(
                        entry["content_digest"], "authorized skill digest", max_length=128
                    ),
                )
            )
        authorization = ToolAuthorization(
            tools=tuple(tools),
            skills=tuple(skills),
            catalog_digest=catalog_digest,
        )
        if len({item.tool_id for item in tools}) != len(tools):
            raise ValueError("authorized tools contain duplicates")
        if len({item.name for item in skills}) != len(skills):
            raise ValueError("authorized skills reference a skill twice")
        return authorization

    @staticmethod
    def _authorization_json(
        authorization: ToolAuthorization | None,
    ) -> dict[str, Any] | None:
        if authorization is None:
            return None
        return {
            "catalog_digest": authorization.catalog_digest,
            "tools": [
                {
                    "tool_id": item.tool_id,
                    "version": item.version,
                    "schema_digest": item.schema_digest,
                }
                for item in sorted(authorization.tools, key=lambda item: item.tool_id)
            ],
            "skills": [
                {
                    "name": item.name,
                    "version": item.version,
                    "content_digest": item.content_digest,
                }
                for item in sorted(authorization.skills, key=lambda item: item.name)
            ],
        }

    @staticmethod
    def _message_json(message: Message) -> dict[str, Any]:
        return {
            "role": message.role,
            "content": message.content,
            "name": message.name,
            "tool_call_id": message.tool_call_id,
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in message.tool_calls
            ],
        }

    def _message(self, value: Any) -> Message:
        item = self._dict(value, "message")
        if set(item) - {"role", "content", "name", "tool_call_id", "tool_calls"}:
            raise ValueError("message contains unknown fields")
        role = item.get("role")
        content = item.get("content")
        if role not in _MESSAGE_ROLES:
            raise ValueError("message role is invalid")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("message content is invalid")
        calls = tuple(
            self._tool_call(call)
            for call in self._list(item.get("tool_calls", []), "tool calls", 100)
        )
        return Message(
            role=role,
            content=content,
            name=self._optional_string(item.get("name"), "message name"),
            tool_call_id=self._optional_string(
                item.get("tool_call_id"),
                "tool call ID",
            ),
            tool_calls=calls,
        )

    def _tool_call(self, value: Any) -> ToolCall:
        item = self._dict(value, "tool call")
        if set(item) != {"call_id", "name", "arguments"}:
            raise ValueError("tool call fields are invalid")
        return ToolCall(
            self._required_string(item["call_id"], "tool call ID"),
            self._required_string(item["name"], "tool name"),
            self._dict(item["arguments"], "tool arguments"),
        )

    def _binding(self, value: Any) -> ApprovalBinding:
        item = self._dict(value, "approval binding")
        if set(item) != {"call_id", "approval_id"}:
            raise ValueError("approval binding fields are invalid")
        return ApprovalBinding(
            self._required_string(item["call_id"], "binding call ID"),
            self._required_string(item["approval_id"], "approval ID"),
        )

    @staticmethod
    def _budget(value: dict[str, Any]) -> RunBudget:
        if set(value) - {
            "max_turns",
            "max_tool_calls",
            "max_total_tokens",
            "max_model_cost_microusd",
        } or not {"max_turns", "max_tool_calls", "max_total_tokens"}.issubset(value):
            raise ValueError("run budget fields are invalid")
        return RunBudget(
            max_turns=AgentRunCheckpointCodec._integer(value["max_turns"], "max turns"),
            max_tool_calls=AgentRunCheckpointCodec._integer(
                value["max_tool_calls"],
                "max tool calls",
            ),
            max_total_tokens=AgentRunCheckpointCodec._integer(
                value["max_total_tokens"],
                "max total tokens",
            ),
            max_model_cost_microusd=AgentRunCheckpointCodec._integer(
                value.get("max_model_cost_microusd", 10_000_000),
                "max model cost",
            ),
        )

    @staticmethod
    def _usage(value: dict[str, Any]) -> RunUsage:
        if set(value) - {
            "turns",
            "tool_calls",
            "total_tokens",
            "model_cost_microusd",
        } or not {
            "turns",
            "tool_calls",
            "total_tokens",
        }.issubset(value):
            raise ValueError("run usage fields are invalid")
        return RunUsage(
            AgentRunCheckpointCodec._integer(value["turns"], "turns"),
            AgentRunCheckpointCodec._integer(value["tool_calls"], "tool calls"),
            AgentRunCheckpointCodec._integer(value["total_tokens"], "total tokens"),
            AgentRunCheckpointCodec._integer(
                value.get("model_cost_microusd", 0),
                "model cost",
            ),
        )

    def _model_route_policy(self, value: dict[str, Any]) -> ModelRoutePolicy:
        allowed = {
            "data_classification",
            "required_capabilities",
            "allowed_provider_ids",
            "residency_regions",
            "allow_external_egress",
            "max_call_cost_microusd",
            "max_output_tokens",
            "max_call_total_tokens",
        }
        if set(value) - allowed:
            raise ValueError("model route policy fields are invalid")
        external = value.get("allow_external_egress", False)
        maximum = value.get("max_call_cost_microusd")
        max_output = value.get("max_output_tokens")
        max_total = value.get("max_call_total_tokens")
        if not isinstance(external, bool):
            raise ValueError("model egress policy is invalid")
        if maximum is not None:
            maximum = self._integer(maximum, "model call cost")
        if max_output is not None:
            max_output = self._integer(max_output, "model output tokens")
        if max_total is not None:
            max_total = self._integer(max_total, "model call tokens")
        return ModelRoutePolicy(
            data_classification=Classification(
                self._integer(
                    value.get("data_classification", int(Classification.INTERNAL)),
                    "model data classification",
                )
            ),
            required_capabilities=frozenset(
                ModelCapability(item)
                for item in self._string_list(
                    value.get("required_capabilities", []),
                    "model required capabilities",
                )
            ),
            allowed_provider_ids=frozenset(
                self._string_list(
                    value.get("allowed_provider_ids", []),
                    "allowed model providers",
                )
            ),
            residency_regions=frozenset(
                self._string_list(
                    value.get("residency_regions", []),
                    "model residency regions",
                )
            ),
            allow_external_egress=external,
            max_call_cost_microusd=maximum,
            max_output_tokens=max_output,
            max_call_total_tokens=max_total,
        )

    @staticmethod
    def _context_budget(value: dict[str, Any]) -> ContextBudget:
        if set(value) != {
            "max_input_tokens",
            "reserved_output_tokens",
            "max_item_tokens",
            "max_items",
        }:
            raise ValueError("context budget fields are invalid")
        return ContextBudget(
            max_input_tokens=AgentRunCheckpointCodec._integer(
                value["max_input_tokens"],
                "max input tokens",
            ),
            reserved_output_tokens=AgentRunCheckpointCodec._integer(
                value["reserved_output_tokens"],
                "reserved output tokens",
            ),
            max_item_tokens=AgentRunCheckpointCodec._integer(
                value["max_item_tokens"],
                "max item tokens",
            ),
            max_items=AgentRunCheckpointCodec._integer(value["max_items"], "max items"),
        )

    def _context_item(self, value: Any) -> ContextItem:
        item = self._dict(value, "context item")
        required = {
            "item_id",
            "content",
            "source",
            "source_id",
            "label",
            "content_trust",
            "instruction_trust",
            "priority",
            "created_at",
            "content_digest",
            "disclosure_grant",
        }
        if set(item) != required:
            raise ValueError("context item fields are invalid")
        created = datetime.fromisoformat(
            self._required_string(item["created_at"], "context created_at")
        )
        result = ContextItem(
            item_id=self._required_string(item["item_id"], "context item ID"),
            content=self._required_string(item["content"], "context content", max_length=1_000_000),
            source=ContextSource(item["source"]),
            source_id=self._required_string(item["source_id"], "context source ID"),
            label=self._label(self._dict(item["label"], "context label")),
            content_trust=ContentTrust(self._integer(item["content_trust"], "content trust")),
            instruction_trust=InstructionTrust(item["instruction_trust"]),
            priority=self._integer(item["priority"], "context priority"),
            created_at=created,
            disclosure_grant=(
                None
                if item["disclosure_grant"] is None
                else self._grant(self._dict(item["disclosure_grant"], "disclosure grant"))
            ),
        )
        if result.content_digest != item["content_digest"]:
            raise ValueError("context content digest is invalid")
        return result

    @staticmethod
    def _context_item_json(item: ContextItem) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "content": item.content,
            "source": item.source.value,
            "source_id": item.source_id,
            "label": {
                "owner_tenant_id": item.label.owner_tenant_id,
                "classification": int(item.label.classification),
                "compartments": sorted(item.label.compartments),
                "resource_id": item.label.resource_id,
            },
            "content_trust": int(item.content_trust),
            "instruction_trust": item.instruction_trust.value,
            "priority": item.priority,
            "created_at": item.created_at.isoformat(),
            "content_digest": item.content_digest,
            "disclosure_grant": AgentRunCheckpointCodec._grant_json(item.disclosure_grant),
        }

    def _label(self, item: dict[str, Any]) -> ResourceLabel:
        if set(item) != {
            "owner_tenant_id",
            "classification",
            "compartments",
            "resource_id",
        }:
            raise ValueError("resource label fields are invalid")
        return ResourceLabel(
            owner_tenant_id=self._required_string(
                item["owner_tenant_id"],
                "label tenant",
            ),
            classification=Classification(self._integer(item["classification"], "classification")),
            compartments=frozenset(self._string_list(item["compartments"], "label compartments")),
            resource_id=self._optional_string(item["resource_id"], "resource ID"),
        )

    def _grant(self, item: dict[str, Any]) -> DisclosureGrant:
        required = {
            "grant_id",
            "owner_tenant_id",
            "recipient_tenant_id",
            "resource_id",
            "purpose",
            "approved_by",
            "expires_at",
            "max_classification",
            "compartments",
        }
        if set(item) != required:
            raise ValueError("disclosure grant fields are invalid")
        return DisclosureGrant(
            grant_id=self._required_string(item["grant_id"], "grant ID"),
            owner_tenant_id=self._required_string(
                item["owner_tenant_id"],
                "grant owner tenant",
            ),
            recipient_tenant_id=self._required_string(
                item["recipient_tenant_id"],
                "grant recipient tenant",
            ),
            resource_id=self._required_string(item["resource_id"], "grant resource ID"),
            purpose=self._required_string(item["purpose"], "grant purpose", max_length=256),
            approved_by=self._required_string(item["approved_by"], "grant approver"),
            expires_at=datetime.fromisoformat(
                self._required_string(item["expires_at"], "grant expiry")
            ),
            max_classification=Classification(
                self._integer(item["max_classification"], "grant classification")
            ),
            compartments=frozenset(self._string_list(item["compartments"], "grant compartments")),
        )

    @staticmethod
    def _grant_json(grant: DisclosureGrant | None) -> dict[str, Any] | None:
        if grant is None:
            return None
        return {
            "grant_id": grant.grant_id,
            "owner_tenant_id": grant.owner_tenant_id,
            "recipient_tenant_id": grant.recipient_tenant_id,
            "resource_id": grant.resource_id,
            "purpose": grant.purpose,
            "approved_by": grant.approved_by,
            "expires_at": grant.expires_at.isoformat(),
            "max_classification": int(grant.max_classification),
            "compartments": sorted(grant.compartments),
        }

    @staticmethod
    def _dict(value: Any, name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
        return value

    @staticmethod
    def _list(value: Any, name: str, maximum: int) -> list[Any]:
        if not isinstance(value, list) or len(value) > maximum:
            raise ValueError(f"{name} must be a bounded array")
        return value

    @staticmethod
    def _integer(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    @staticmethod
    def _required_string(value: Any, name: str, *, max_length: int = 128) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_length:
            raise ValueError(f"{name} is invalid")
        return value

    @staticmethod
    def _optional_string(value: Any, name: str) -> str | None:
        if value is None:
            return None
        return AgentRunCheckpointCodec._required_string(value, name)

    def _string_list(self, value: Any, name: str) -> tuple[str, ...]:
        items = self._list(value, name, 256)
        strings = tuple(self._required_string(item, name) for item in items)
        if len(set(strings)) != len(strings):
            raise ValueError(f"{name} contains duplicates")
        return strings
