# PostgreSQL 与迁移运维

## 安全边界

生产持久化使用 PostgreSQL。Memory、协作治理和 durable execution 表统一采用：

- 每行显式 `tenant_id`；
- 所有仓储查询中的租户谓词；
- `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`；
- 基于事务本地 `coifesp.tenant_id` 的 fail-closed RLS 策略；
- 复合主键、检查约束、外键和面向召回路径的索引。

仓储在每个事务开始时调用参数化的 `set_config` 设置租户上下文。没有租户上下文时，
`current_setting(..., true)` 返回空值，RLS 不会放行任何租户行。应用数据库账号不得拥有
`SUPERUSER` 或 `BYPASSRLS`，否则 PostgreSQL 可以绕过该安全边界。

Memory 幂等声明和密文记录写入位于同一事务。相同键、相同请求返回原结果；相同键更换内容返回冲突；
任何记录插入错误都会连同新声明一起回滚。声明通过延迟复合外键绑定对应的 Memory 记录。

Execution 使用 `execution_tasks`、`execution_task_dependencies` 和 `execution_task_events`。任务入队
与幂等键、依赖边和首个事件原子提交；依赖必须已存在于同一租户，从结构上避免跨租户边和向前引用形成
环。执行事件仅允许追加，并由数据库触发器拒绝更新、删除和截断。每个状态变更同时追加签名审计事件。

跨团队传递使用 `governance_outbox` 和 `collaboration_inbox`。Outbox 只允许事件生产租户查询和更新，
Inbox 只允许消息接收租户访问；发送方不能查看接收方的处理结果，接收方也不能直接读取发送方待发送
队列。两个队列都使用有上限的尝试次数、租约和 fencing token，Inbox 以
`(recipient_tenant_id, message_id)` 作为重放去重边界。

高风险工具审批使用 `approval_requests`。审批记录与请求租户、执行人、工具名、规范化参数摘要、
数据分级和隔离域精确绑定；申请人不能批准自己的操作。决定使用乐观版本，执行消费使用行锁并绑定
`execution_id`，相同执行的恢复重试可重复读取消费结果，不同执行不能重放同一审批。审批理由只在
审计中保存摘要，状态变更与租户签名审计在同一事务提交。

Agent 运行使用 `agent_runs` 和 `agent_run_events`。检查点以租户派生密钥进行 AES-GCM 加密，AAD
绑定 Run 版本；领取、启动、心跳和提交均受租约 fencing token 保护。生命周期事件只追加最小元数据，
不复制检查点明文，并与签名审计原子提交。SSE 使用 Run 内连续事件序号实现 `Last-Event-ID` 续传。
`failure_count/max_failures/next_attempt_at` 在数据库内约束有限重试；只有到期的 Run 可被领取，耗尽
失败预算后进入终态。`last_error_code` 只允许无秘密的规范化代码。

工具异步执行使用 `tool_jobs` 和 `tool_job_events`。参数与结果使用从 Memory 主密钥进行域隔离派生的
租户密钥加密，AAD 绑定租户、Job、用途和密钥版本；参数内容只用带密钥摘要参与幂等比较。租户内同时
约束调用方幂等键和 `(run_id, call_id)`，防止同一模型调用形成两个外部副作用。领取使用
`FOR UPDATE SKIP LOCKED`，租约过期后按尝试预算恢复，旧 Worker 的 token 无法提交。事件表只允许
追加，状态事件和签名审计在同一事务提交。数据库租约只能提供至少一次尝试；外部连接器必须把稳定
`idempotency_key` 传给支持幂等的供应商 API，才能覆盖“供应商已执行但本地提交前宕机”的窗口。

Agent 与 Tool Job 的衔接使用 `awaiting_tool`。Agent Worker 先对整个模型工具批次完成策略、参数和审批
预检，再在一个事务中提交加密 checkpoint、全部 Tool Job 和双方签名审计；任一 Job 冲突会使整批
回滚。协调器仅在当前 checkpoint 所列 Job 全部进入终态后写回数据型 Tool Message，并在同一事务中
将 Agent Run 重新排队。Tool Worker 每次领取前后扫描等待 Run，因此“最后一个 Tool 已提交但唤醒前
宕机”的窗口可恢复。历史批次仍保留作审计证据，但不会参与后续批次聚合。

跨团队契约、版本、消费依赖和变更影响使用独立规范化表及显式租户可见性。契约命令、状态、签名审计、
追加式事件与 outbox 在同一事务提交；当前迁移头为 `20260813_28`。未被消费方接受的 action-required 或
blocking 影响通过服务层事务门禁阻止任务验收，数据库则用强制 RLS 与不可变事件触发器保护隔离和证据链。

## 迁移规则

真实连接只从 `.env` 的 `COIFESP_DATABASE_URL` 加载；连接串不会写入 `alembic.ini`。生产迁移只接受
PostgreSQL URL，SQLite 的 `create_schema()` 仅用于测试和开发引导。

部署前先生成并审查离线 SQL：

```bat
E:\miniconda3\envs\bettafish\python.exe -m alembic upgrade head --sql
```

查看当前版本并应用升级：

```bat
E:\miniconda3\envs\bettafish\python.exe -m alembic current
E:\miniconda3\envs\bettafish\python.exe -m alembic upgrade head
```

验证模型与数据库没有迁移漂移：

```bat
E:\miniconda3\envs\bettafish\python.exe -m alembic check
```

已经应用到任何共享环境的迁移文件视为不可变；后续修改使用新 revision。降级可能删除表或数据，不属于
常规发布流程，必须先备份、审查降级 SQL 并获得明确操作授权。

## 发布后检查

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_memory.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_audit.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_governance.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_execution.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_collaboration.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_approvals.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_agent_runs.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_tool_jobs.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_tool_batch.py
```

该检查验证：

- 数据库处于预期 Alembic head；
- 所有受保护的 Memory、审计、治理和执行表均启用并强制执行 RLS；
- 策略使用事务本地租户上下文；
- 当前账号不能绕过 RLS；
- Memory 明文不会直接出现在密文字段；
- 加密写入、读取和重复请求抑制正常；
- DAG 阻塞/释放、租约 fencing、崩溃恢复、取消和执行事件不可变性正常；
- Outbox/Inbox 租约恢复、签名、脱敏、重放抑制、跨租户隐藏和原子审计正常；
- 审批职责分离、版本冲突、数据标签绑定、一次性消费、恢复重放和跨租户隐藏正常；
- Agent 检查点加密、幂等创建、租约 fencing/心跳、事件续传、跨租户隐藏和事件不可变性正常；
- Tool 参数/结果加密、双重幂等、租约 fencing/恢复、跨租户隐藏、事件不可变和原子审计正常；
- 冒烟数据位于外层事务中，验证完成后整体回滚且不残留测试记录。

检查输出只包含状态和非秘密标识，不输出数据库密码、API Key 或主密钥。
