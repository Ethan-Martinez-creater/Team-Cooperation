# 持久化 Agent Run 与检查点恢复

Agent Run 把一次 Agent Loop 从单进程内对象提升为可恢复的持久化状态机。它不保存模型供应商的
隐式会话句柄，而是保存经过版本校验的完整 Harness 检查点，使 Worker 崩溃、滚动发布和审批暂停后
都能确定性恢复。

## 状态与职责

状态流为：

`queued → leased → running → awaiting_approval → queued`，或从运行态进入
`completed / failed / cancelled`。控制面创建和查看 Run，拥有 `agent_worker` 角色的服务身份才可
领取、启动、续租或提交检查点。普通用户不能伪造 Worker 身份；非所有者只能由
`agent_run_controller` 或平台管理员读取。

领取使用 PostgreSQL `FOR UPDATE SKIP LOCKED`。每次领取生成不可预测的 fencing token，后续启动、
心跳和检查点写入必须同时匹配租户、Worker、token 和未过期租约。过期租约由恢复调度器重新排队，
旧 Worker 即使晚到也不能覆盖新 Worker 的结果。

`DurableAgentWorker` 已把该状态机接到真实 `AgentLoop`。Worker 领取后必须通过注入的
`PrincipalResolver` 重新解析 Run 所有者的当前角色、clearance 和 compartment；检查点不保存也不
恢复旧身份 claim。解析结果必须精确匹配租户和所有者，且不能是服务身份。模型运行期间独立协程定期
续租；续租失败会取消本地 Loop，旧 Worker 不再提交检查点。

依赖超时、连接和 I/O 故障按带确定性 jitter 的指数退避重新排队。`failure_count/max_failures` 是
数据库硬边界，耗尽后原子进入 `failed`，避免无限重试和成本失控。完整性、策略、上下文和输入错误
直接失败，不用重试掩盖确定性缺陷。数据库与事件只保存固定 allowlist 的 `error_code`，不保存异常
消息、栈或可能带秘密的供应商响应。

## 加密检查点

检查点采用规范化 JSON，最大 2 MiB，并要求版本化 schema、消息、预算、累计用量和审批绑定。
控制面验证检查点中的用量必须与独立数据库计数器完全一致，防止通过恢复重置轮次、工具调用或 token
预算。

严格 codec 会逐字段重建消息、工具调用、运行预算、上下文预算、来源、安全标签、信任级别和披露授权，
拒绝未知字段、重复绑定、非法枚举、越界数组和内容摘要不一致。跨团队上下文仍在每次恢复后的
`ContextAssembler` 中重新检查披露授权是否匹配用途且尚未过期。预算超限会形成带实际累计 token
用量的失败检查点，而不是抛出后丢失已发生的模型成本。

存储使用 AES-256-GCM；从 Memory 主密钥通过独立 HKDF domain 为每个租户派生 Agent checkpoint
密钥。AAD 绑定租户、Run ID、版本和 key ID，每次状态版本变化都会重新加密，因此密文不能跨租户、
跨 Run 或跨版本替换。数据库只保存密文、nonce、HMAC 指纹和 key ID，不保存消息明文。

审批恢复时，客户端只能提交审批 ID 和期望版本。服务端先从持久化审批仓储确认它是同一申请人的
`tool_managed` 审批且状态为 `approved`，再读取并解密已有检查点、写入批准绑定并重新排队；审批
服务未配置时失败关闭，客户端不能借恢复接口跳过审批或替换消息、预算。

## 事件与断线续传

每个状态变化在同一数据库事务中追加：

- 租户隔离的 `agent_run_events` 生命周期事件；
- 对应的签名审计事件。

生命周期事件只包含状态、版本等最小元数据，写入前执行秘密扫描，不包含检查点、提示词、工具参数或
租约 token。数据库触发器拒绝事件更新、删除和截断。

`GET /v1/agent-runs/{run_id}/events` 提供 SSE。事件的 `id` 是 Run 内连续序号；客户端重连时发送
`Last-Event-ID`，服务端只返回更大的序号。反向代理必须关闭响应缓冲，并保留心跳注释和
`text/event-stream`。

## 运维

部署后运行：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_agent_runs.py
```

烟测覆盖强制 RLS、checkpoint 与控制指令加密、幂等创建、租约 fencing、心跳、事件连续性、跨租户
隐藏、事件/指令不可变性、控制指令与 checkpoint 原子提交、有限重试、事务审计和整体回滚。双向控制
协议、竞态语义和恢复格式见 [Agent Steering 与 Follow-up 控制流](agent-control.md)。

生产 Worker 使用两个隔离的 OAuth 客户端：`coifesp-agent-worker` 只作为领取、续租和提交检查点的机器
身份；`coifesp-directory-reader` 只具备 Keycloak `view-users`、`query-users` 和 `query-groups`，用于在
每次 Run 开始时重新读取所有者当前的直属/Group 安全属性与 Realm 角色。两个客户端 ID 和密钥必须不同。
解析器拒绝禁用用户、服务账户所有者、跨 Group 冲突的租户或 clearance、租户漂移及空角色；检查点内
旧 claim 从不作为回退来源。

启动命令：

```bat
E:\miniconda3\envs\bettafish\python.exe -m coifesp_harness.worker_main
```

当前入口只运行无副作用工具的 Agent Loop。外部工具在 PostgreSQL 工具执行账本和 sandbox worker
完成前不会注册，避免以进程内幂等存储冒充跨重启 at-most-once 保证。

真实环境复验命令：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_worker_identity_e2e.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_worker_startup.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_worker_live_run.py
```

最后一项会创建无密码临时 Keycloak 用户，执行一条公开且成本硬限制的 DeepSeek Run，随后按精确用户 UUID
清理身份；数据库保留终态 Run 与不可变审计证据，用于证明真实租约、用量与加密检查点链路。
