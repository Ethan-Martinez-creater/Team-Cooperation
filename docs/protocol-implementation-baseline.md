# MCP / A2A 协议实施基线

记录日期：2026-08-13
状态：冻结当前实现进度；后续开发必须以本文件为恢复基线，不得把尚未完成的能力声明为可用。

## 0. 冻结约束

- 冻结生效日期：2026-08-13；
- 冻结范围：MCP/A2A 的直接实现、包导出和专项测试；
- 冻结期间允许继续演进被全系统共用的治理、身份、审计、Memory、Tool、队列和可观测性模块，
  但不得把这些共用能力自动视为已经接入 MCP/A2A；
- 未经明确恢复决定，不新增协议路由、持久化模型、配置项、能力声明或依赖，也不修改下列冻结文件；
- 恢复开发前应重新核对本文件中的“尚未实现”和“当前能力声明”，并为本节更新日期和文件指纹；
- 若安全修复必须触及冻结文件，应单独记录原因、影响、测试和新指纹，不能借机扩大协议能力范围。

冻结时的文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `src/coifesp_harness/mcp_gateway/__init__.py` | `b93f9daaf51298a3bc81c59f9baa3d37fa5020bea4d94120a2ee4e1be7eb259b` |
| `src/coifesp_harness/mcp_gateway/gateway.py` | `cb9d879b5878a0251bd00de53ea3d6bfa21e67f2cd8994f45b9aa50762a8f4e8` |
| `tests/test_mcp_gateway.py` | `49f7d0fa19717293966a797f89dea4ff4221f2d2be722d487fa1e5919877e9e2` |
| `src/coifesp_harness/a2a_gateway/__init__.py` | `07f3894852c49ed816974b438c967145111476b88618a4d0ad7f4639fa10f7ec` |
| `src/coifesp_harness/a2a_gateway/gateway.py` | `d6129dde4dab23b59bb37d64f3fdd95681946d54cce7e5ed3d603850fd078cff` |
| `tests/test_a2a_gateway.py` | `ce86754b70339fd6d3c8c841848c1c6f09d1e91ffa400f695c107d12f11ab784` |

这些指纹只用于检测冻结范围内的意外改动；Python 缓存、测试缓存和构建产物不属于冻结基线。

## 1. MCP

当前定位：**安全的 Tool-only MCP Client 核心已完成，但尚不是可运营的完整 MCP Gateway，也不是
MCP Server。**

### 已实现

- 使用官方 Python SDK `mcp >=1.28.1,<2` 和 Streamable HTTP Client；
- 初始化和协议版本协商，只接受 SDK 明确支持的版本；
- `tools/list` 分页发现和 `tools/call` 调用；
- 远端 HTTPS 强制要求；仅显式配置时允许 localhost HTTP；
- URL 禁止内嵌凭据和 fragment，HTTP 客户端禁重定向、禁用环境代理继承；
- Bearer Token 不进入对象表示；
- endpoint 按租户隔离且默认拒绝；
- 远端 Tool 只有存在本地 `McpToolPolicy` 时才会注册；
- 本地重新绑定角色、风险、超时、输出上限和高风险审批投影，远端描述不能扩权；
- MCP Tool 映射为系统原生 `ToolDefinition`，可进入既有工具执行与审批管线；
- 单元测试覆盖端点安全、租户隔离、默认拒绝和本地授权映射。

实现入口：`src/coifesp_harness/mcp_gateway/gateway.py`。
测试入口：`tests/test_mcp_gateway.py`。

### 尚未实现

- COIFESP MCP Server；
- endpoint 的控制面 API、PostgreSQL 持久化、RLS 和运维生命周期；
- MCP OAuth discovery、PKCE、scope、token refresh 和凭据轮换编排；
- Resources、Prompts、Roots、Sampling、Elicitation、Notifications、Logging 和 Completion；
- 会话恢复、连接池、健康状态、熔断状态持久化和真实 MCP Server 端到端验收；
- 随应用启动自动装配 endpoint 与 Tool。

### 当前能力声明

只能声明“受本地策略约束的 MCP Tool Client 核心”。不得声明完整 MCP client 功能覆盖、MCP Server、
MCP OAuth 或生产 MCP Gateway 已完成。

## 2. A2A

当前定位：**A2A 1.0 的安全发现模型和最小治理任务交付消息已完成，但尚未形成可访问的 A2A
Server/Client 业务链路。**

### 已实现

- 使用官方 `a2a-sdk >=1.1.1,<2` 的 A2A 1.0 类型；
- 构造声明 OIDC Bearer 的 A2A 1.0 Agent Card；
- 校验 HTTPS、协议版本 `1.0`、`HTTP+JSON`/`JSONRPC` binding、认证要求和 tenant 一致性；
- Agent Card endpoint 目录按租户隔离且默认拒绝；
- 将治理 assignment 转换为标准 A2A `Message`；
- 消息只包含显式交付契约字段，不暴露项目/计划私有目标、实现描述、Prompt、Memory 或 Agent 内部状态；
- 校验接收团队确为任务负责人且在任务可见范围内；
- 团队能力目录可以发布 `a2a-1.0` 能力及输入/输出契约、数据分级、compartment、驻留和可见团队；
- 已有 durable collaboration outbox/inbox、签名 Envelope、治理状态机和 Agent Run，可作为后续 A2A
  TaskStore/执行映射的底层组件；这些组件当前尚未接入 A2A 网络协议。

实现入口：`src/coifesp_harness/a2a_gateway/gateway.py`。
测试入口：`tests/test_a2a_gateway.py`。

### 尚未实现

- Agent Card 和 A2A REST/JSON-RPC 的实际 HTTP 路由；
- 官方 SDK `RequestHandler` 适配和 PostgreSQL-backed A2A `TaskStore`；
- `SendMessage`、`SendStreamingMessage`、`GetTask`、`ListTasks`、`CancelTask`、
  `SubscribeToTask` 和 Extended Agent Card；
- A2A Task 与 governance assignment、execution task、agent run 的持久映射；
- Task lifecycle、SSE 状态/artifact 更新、游标恢复；
- 入站 OIDC identity 到 tenant/principal 的绑定、协议级幂等、防重放和审计适配；
- 远端 Agent Card 信任验证及消息/服务签名；
- Push Notification Config、回调认证、SSRF/DNS rebinding 防护、重试和 dead letter；
- 两个真实 A2A Agent 之间的端到端互操作验收。

### 当前能力声明

Agent Card 中必须继续声明：

```text
streaming = false
push_notifications = false
```

不得声明已提供 A2A Task API、流式订阅或 Push。虽然本地 Keycloak OIDC 已完成验收，A2A 暂不挂载的
原因是业务路由、授权适配和持久化 TaskStore 尚未实现，而不是身份环境缺失。

## 3. 恢复开发顺序

1. A2A PostgreSQL TaskStore 与现有治理/执行/Agent Run 的状态映射；
2. A2A OIDC 上下文适配和 Agent Card、REST/JSON-RPC 路由；
3. 非流式 Task 操作，再实现 SSE 流式与恢复；
4. Agent Card 信任、签名和真实双 Agent 互操作；
5. 满足公网 HTTPS、回调认证和 SSRF 门禁后才启用 Push；
6. MCP endpoint 持久化、控制面、OAuth 和真实 Server 联调；
7. COIFESP MCP Server 以及 Resources/Prompts 等扩展能力。

任何恢复开发都应同时补齐：协议兼容测试、OIDC/RLS 安全测试、真实 PostgreSQL 验收和失败恢复测试。
