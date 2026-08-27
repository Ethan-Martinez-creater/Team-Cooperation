from __future__ import annotations

from dataclasses import dataclass

from ..security.models import Principal
from ..security.policy import DecisionEffect, PolicyEngine
from ..skills import SkillCatalog
from ..tool_catalog import ToolManifest

KNOWN_TOOL_IDS = (
    "code.run_profile",
    "office.send_message",
    "list_skills",
    "load_skill",
    "project.list_context",
    "project.read_context",
)

STATUS_AVAILABLE = "available"
STATUS_APPROVAL_REQUIRED = "approval_required"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_NO_PERMISSION = "no_permission"


@dataclass(frozen=True, slots=True)
class CapabilityToolView:
    tool_id: str
    version: str
    status: str
    reason: str
    read_only: bool
    modifies_external_system: bool


@dataclass(frozen=True, slots=True)
class CapabilitySkillView:
    name: str
    version: str
    description: str
    publisher_team: str
    required_tools: tuple[str, ...]
    classification: str


@dataclass(frozen=True, slots=True)
class CapabilityModelView:
    provider_id: str
    model: str
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    tools: tuple[CapabilityToolView, ...]
    skills: tuple[CapabilitySkillView, ...]
    models: tuple[CapabilityModelView, ...]


class AgentCapabilityService:
    """Projects the tools, skills, and models an account can really use.

    Every entry carries an explicit status and a safe, user-presentable reason.
    Nothing here exposes connector tokens, system prompts, internal network
    addresses, or other tenants' configuration.
    """

    def __init__(
        self,
        *,
        manifests: tuple[ToolManifest, ...],
        skill_catalog: SkillCatalog | None = None,
        policy: PolicyEngine | None = None,
        llm_providers: tuple = (),
    ) -> None:
        self.manifests = {manifest.tool_id: manifest for manifest in manifests}
        self.skill_catalog = skill_catalog
        self.policy = policy or PolicyEngine()
        self.llm_providers = tuple(llm_providers)

    def report(self, *, principal: Principal, project_id: str | None = None) -> CapabilityReport:
        tools: list[CapabilityToolView] = []
        for tool_id in KNOWN_TOOL_IDS:
            tools.append(self._tool_view(tool_id, principal, project_id=project_id))
        skills: tuple[CapabilitySkillView, ...] = ()
        if self.skill_catalog is not None:
            skills = tuple(
                CapabilitySkillView(
                    name=manifest.name,
                    version=manifest.version,
                    description=manifest.description,
                    publisher_team=manifest.tenant_id,
                    required_tools=tuple(sorted(manifest.required_tools)),
                    classification=manifest.classification.name.lower(),
                )
                for manifest in self.skill_catalog.list_visible(principal)
            )
        models: list[CapabilityModelView] = []
        if self.llm_providers:
            for provider in self.llm_providers:
                models.append(
                    CapabilityModelView(
                        provider_id=provider.provider_id,
                        model=getattr(provider, "model", "") or "",
                        status=STATUS_AVAILABLE,
                        reason="模型提供方已配置，可按运行路由策略使用",
                    )
                )
        else:
            models.append(
                CapabilityModelView(
                    provider_id="",
                    model="",
                    status=STATUS_NOT_CONFIGURED,
                    reason="尚未配置任何模型提供方，Agent 无法调用模型",
                )
            )
        return CapabilityReport(
            tools=tuple(tools),
            skills=skills,
            models=tuple(models),
        )

    def _tool_view(
        self, tool_id: str, principal: Principal, *, project_id: str | None = None
    ) -> CapabilityToolView:
        manifest = self.manifests.get(tool_id)
        if tool_id in {"list_skills", "load_skill", "project.list_context", "project.read_context"}:
            if self.skill_catalog is None:
                return CapabilityToolView(
                    tool_id=tool_id,
                    version="1",
                    status=STATUS_NOT_CONFIGURED,
                    reason="Skill 目录未配置，请联系管理员预置已签名 Skill",
                    read_only=True,
                    modifies_external_system=False,
                )
        if manifest is None:
            if tool_id == "code.run_profile":
                reason = "Sandbox profile 未配置，无法执行代码工具"
            elif tool_id == "office.send_message":
                reason = "办公连接器未配置或未启用"
            else:
                reason = "工具未在当前部署中启用"
            return CapabilityToolView(
                tool_id=tool_id,
                version="1",
                status=STATUS_NOT_CONFIGURED,
                reason=reason,
                read_only=tool_id
                in {"list_skills", "load_skill", "project.list_context", "project.read_context"},
                modifies_external_system=False,
            )
        if project_id is not None and tool_id == "office.send_message":
            return CapabilityToolView(
                tool_id=tool_id,
                version=manifest.version,
                status=STATUS_NO_PERMISSION,
                reason="当前项目未绑定可写办公连接器；请通过协作草案执行外部动作",
                read_only=False,
                modifies_external_system=True,
            )
        decision = self.policy.decide_tool_execution(
            principal=principal,
            required_roles=manifest.required_roles,
            risk=manifest.risk,
        )
        if decision.effect is DecisionEffect.DENY:
            status, reason = STATUS_NO_PERMISSION, "当前账户角色不满足该工具的要求"
        elif decision.effect is DecisionEffect.REQUIRE_APPROVAL:
            status = STATUS_APPROVAL_REQUIRED
            reason = "高风险工具：每次调用都需要人工审批后才会执行"
        else:
            status = STATUS_AVAILABLE
            reason = "可用"
        return CapabilityToolView(
            tool_id=tool_id,
            version=manifest.version,
            status=status,
            reason=reason,
            read_only=tool_id
            in {"list_skills", "load_skill", "project.list_context", "project.read_context"},
            modifies_external_system=tool_id == "office.send_message",
        )

    def authorize_tools(
        self, *, principal: Principal, tool_ids: tuple[str, ...], project_id: str | None = None
    ) -> tuple[ToolManifest, ...]:
        """Validate a run-creation request against real, usable manifests.

        Raises ``ValueError`` with a user-presentable reason when a requested
        tool is unknown, not configured, or not permitted for the account.
        """
        if len(tool_ids) > 32 or len(set(tool_ids)) != len(tool_ids):
            raise ValueError("tool_ids 包含重复项或超过上限")
        authorized: list[ToolManifest] = []
        for tool_id in tool_ids:
            view = self._tool_view(tool_id, principal, project_id=project_id)
            if view.status == STATUS_NOT_CONFIGURED:
                raise ValueError(f"工具 {tool_id} 未配置：{view.reason}")
            if view.status == STATUS_NO_PERMISSION:
                raise ValueError(f"工具 {tool_id} 当前账户无权使用：{view.reason}")
            manifest = self.manifests[tool_id]
            authorized.append(manifest)
        return tuple(authorized)
