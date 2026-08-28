# Architecture Decision Records

本目录记录 Team-Cooperation 从 Agent-first 协作产品演进为 Multi-Agent Collaborative Execution Harness 时不可由单个实现任务自行改变的架构决策。

## 基线

- 设计来源：`docs/plan/iteration/Team-Cooperation_Multi-Agent_Harness_Execution_Plan_v1.1_Audited.md`
- 代码基线：`main@7d6934db1f7e66b9d68261ae47f5de34886e526f`
- 数据库基线：Alembic `20260826_45`
- 现有稳定原语：Durable AgentRun、ToolExecutor/ToolWorker、Approval、Context、Artifact、Memory、Capability/Capacity、Exchange、Audit、Model Gateway
- 本轮新增核心：ProjectProcess、WorkGraph、Team Contract、Deterministic Orchestrator、Human Gate、Verification、Integration、Delivery、Completion

## ADR 状态

- `Proposed`：决策草案，禁止依赖它启动跨模块实现；
- `Accepted`：主线程复核通过，可作为公共接口和测试依据；
- `Superseded`：被后续 ADR 明确替代，历史保留但不得用于新实现；
- `Rejected`：方案被否决，仅保留决策背景。

只有主线程可以把 ADR 从 `Proposed` 改为 `Accepted`。实现智能体不得在代码中绕开 Accepted ADR；确需改变时必须先提出新的 ADR。

## 编号与主题

专家方案第 29 章和第 35 章对 0002–0004 的文件名存在不同排列。本目录采用第 35 章“第一批实际编码任务”的具体领域命名，并把 Team Agent logical boundary 纳入 Team Contract ADR：

| ADR | 主题 | 冻结的主要边界 |
|---|---|---|
| 0001 | Project Process vs Agent Run | 长期项目进程与有限 Agent 执行分层 |
| 0002 | Project Work Graph | 项目权威事实、节点关系与 Snapshot |
| 0003 | Team Agent Contract | Team Agent 边界、Contract 接受与调度 readiness |
| 0004 | Project Orchestrator | 确定性控制优先、Planner 仅输出命令 |
| 0005 | Project Process Transition Matrix | 唯一状态迁移入口和终态不变性 |
| 0006 | Project Budget and Concurrency | ProjectExecutionPolicy 与 RunBudget 分层 |
| 0007 | Agent Execution Identity | `initiated_by` 与 `executed_as` 分离 |
| 0008 | Transactional Process Events | 业务状态与 Process Event 原子一致 |
| 0009 | Human Gates and Input | 持久化、可恢复、可审计的人类等待点 |
| 0010 | Capability Directory Integration | 复用唯一能力/容量事实源 |
| 0011 | Integration and Delivery | DeliveryManifest 接受后才允许完成 |

## 规范性附录

- `project-process-event-catalog-v1.md`：领域事实、迁移键和 Audit 事件分层；
- `project-harness-fixture-contract-v1.md`：B2/B6 评测输入与观察协议；
- `project-harness-contract-v1.json`：供实现和契约测试使用的机器可读枚举与映射。

三份附录与 Accepted ADR 具有同等约束力。变更机器契约时必须同步更新
对应 ADR、附录和契约测试。

## 全局不可破坏约束

以下约束对全部 ADR 和实现阶段生效：

1. `ProjectProcess` 与 `AgentRun` 生命周期独立；
2. Conversation 和 Memory 不是项目权威状态；
3. WorkGraph 不复制业务对象状态，只提供统一节点身份和关系；
4. 新系统不得新增第三套 Task 状态事实源；
5. 所有 ProjectProcess 状态迁移经过 `ProjectTransitionGuard`；
6. Planner 只能产生受 schema、policy、graph 和 stale guard 验证的 command；
7. Planner、Team Agent、Specialist Agent 均不能直接写业务状态或绕开 ToolExecutor；
8. 业务状态变化与对应 Process Event 必须同事务提交，或先写 Transactional Outbox；
9. 自动 AgentRun 不得冒用真实用户身份；
10. Project Orchestrator 不默认读取 Team-private Artifact 正文；
11. 跨团队 Contract 必须被目标团队接受后才可能进入派生 readiness；
12. ProjectExecutionPolicy 不能由 Planner 自行提高；
13. 所有必需任务 verified 仍不代表项目完成；
14. Integration 必须通过，DeliveryManifest 必须被接受；
15. 最终 `COMPLETED` 只能由 deterministic CompletionEvaluator 驱动；
16. 现有 AgentRun、ToolJob、Approval、Context isolation、Artifact integrity 和 Audit 边界不得被新 Orchestrator 绕开。

## 实施 Gate

### Gate 0：ADR Accepted

11 份 ADR 全部经主线程复核为 `Accepted`，且不存在未裁决的公共 schema、迁移策略或身份语义。

### Gate 1：Work Graph

- Plan v1 兼容与 Plan v2 parser 测试通过；
- 节点、关系和 Snapshot 接口稳定；
- cycle、跨项目、缺失节点、自引用和重复关系测试通过；
- Plan 物化具备确定性 ID 和 crash-retry 幂等性。

### Gate 2：Shadow ProjectProcess

- Transition Matrix、optimistic concurrency、terminal immutability 测试通过；
- Budget、Concurrency、Gate/Input 的 sleep/restart/resume 测试通过；
- business mutation + event/outbox 原子性和崩溃恢复测试通过；
- 此阶段仍不自动 dispatch Agent。

### Gate 3：Deterministic Orchestrator

- 使用手工 WorkGraph 可推进至 Verification；
- dependency、readiness、budget、capacity、Gate 和 completion 规则无需模型即可运行；
- lease、heartbeat、fencing、wakeup 去重和 worker recovery 通过；
- 未通过 Gate 3 不得接入 Planner 自动编排。

### Gate 4：Planner 与 Team Agent

- stale decision 无法应用任何 command；
- 自动 Run 使用受约束的 service/delegated principal；
- Capability/Capacity 复用现有目录；
- Contract `ACCEPTED` 与派生 `ready` 严格分离；
- 跨团队私有上下文隔离测试通过。

### Gate 5：Delivery Completion

- Verification、Integration、Delivery 和 Completion 全链路通过；
- Delivery rejection 可进入显式 rework/replan；
- 未接受 DeliveryManifest 时任何路径都不能进入 `TERMINAL/COMPLETED`；
- Recovery E2E、PostgreSQL migration smoke 和全量回归通过。

## 修改与验收规则

- 一个写入型 Agent 只拥有一个独立 worktree 和明确文件范围；
- Alembic migration owner 同一时刻只有一个；
- `product/planning.py`、`product/service.py`、`product/repository.py`、`control_plane/bootstrap.py` 等共享文件串行修改；
- 每个 Phase 必须在对应 Gate 通过后再进入下一阶段；
- Worker 返回的测试结论由主线程复跑，不以报告代替验证；
- ADR、代码、迁移、API、UI 和真实环境验收由主线程统一收口。
