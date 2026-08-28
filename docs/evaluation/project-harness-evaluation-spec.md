# Project Harness Evaluation 规格与确定性 Fixture（B2）

- 状态：Proposed（供主线程评审；Eval 场景语义与全局不变量以主线程冻结版本为准）
- 任务来源：`docs/plan/iteration/Multi-Agent-Harness-Parallel-Execution-Coordination.md` B2
- 语义来源：
  - `docs/plan/iteration/Team-Cooperation_Multi-Agent_Harness_Execution_Plan_v1.1_Audited.md` 第 32–34 章（测试计划 / Evaluation / Definition of Done），以及第 4–17、27–28 章的领域语义；
  - `docs/adr/0005-project-process-transition-matrix.md`（迁移矩阵与 Guard）、
    `docs/adr/0006-project-budget-and-concurrency.md`（预算与并发）、
    `docs/adr/0007-agent-execution-identity.md`（执行身份）、
    `docs/adr/0008-transactional-process-events.md`（事务事件）。
- 交付物：本规格 + `tests/fixtures/project_harness/*.json`（8 个文件，14 个场景，覆盖 Eval 1–14）。
- 边界：本任务**只定义评测输入、期望状态和安全不变量**，不实现 ProjectProcess；不修改任何源码、迁移、现有测试；不猜测尚未冻结的数据库字段、Python 类名或 API 路径。

---

## 1. 目的

为未来的 Harness Eval 执行器（协调方案 B6）提供一组**确定性、离线、可重放**的场景定义：

1. 每个场景给出完整七要素：initial state、输入事件序列、expected transitions、required outputs、forbidden outcomes、安全不变量、崩溃与恢复预期；
2. 全部期望只用**领域语义名称**表达（阶段/状态/等待原因枚举、任务生命周期、领域事件名、语义对象引用），不绑定任何具体存储字段或代码符号；
3. 把专家计划第 33 章 Eval 1–14 的简短描述展开成可执行验收规格，并把过程中发现的**未冻结契约决策**显式记录在第 10 节，供主线程裁定。

## 2. 语义名称使用规则（防猜测约束）

Fixture 与本规格遵守以下规则：

| 类别 | 允许 | 不允许 |
| --- | --- | --- |
| 过程状态 | 计划 4.2 / ADR-0005 发布的 `Phase`/`Status`/`WaitReason` 枚举值 | 自造状态值、数据库列名、约束名 |
| 任务生命周期 | 计划 5.2 的 `proposed/accepted/in_progress/submitted/verified/changes_requested/rejected` | 新增任务状态枚举值 |
| Delivery / InputRequest / Gate 状态 | 计划 15.2 / 4.6 的枚举（`ASSEMBLING/READY/ACCEPTED/REJECTED`；`OPEN/ANSWERED/CANCELLED/EXPIRED`） | 猜测其他状态值 |
| 领域事件名 | 计划 8.2 wakeup 源清单 + ADR-0005 主链迁移事件（见第 5.3 节分层约定） | 猜测消息队列 topic、API 路径 |
| 策略字段 | 计划 4.5 / ADR-0006 "at least" 字段清单的语义名（`max_total_tokens` 等） | 具体类型、精度、表名 |
| 对象标识 | fixture 内部语义引用（`team-backend`、`task-backend-api`、`resource-api-spec`） | UUID、主键、外键名 |
| 引用表达式 | `process.version_at(planner_run_started)` 这类语义表达式 | 具体版本号数值 |

场景 schema（`coifesp.orchestration-decision.v1`、`coifesp.verification-result.v1`、`coifesp.project-plan.v2`）仅按计划 7.4 / 14.3 / 6.1 的文本引用其存在与关键字段语义，不扩展未定义字段。

## 3. Fixture 文件与场景覆盖矩阵

协调方案 B2 固定了 8 个 JSON 文件名。本规格将 Eval 1–14 按主题域分组落入 8 个文件，每个文件含 1–3 个 scenario，共 14 个 scenario，一一覆盖 Eval 1–14。

| # | Fixture 文件 | Scenario | 覆盖 Eval | 主题 |
| --- | --- | --- | --- | --- |
| 1 | `simple_project.json` | `eval-01-simple-project` | Eval 1 | 双团队正常执行全链（参考轨迹） |
| 2 | `simple_project.json` | `eval-05-worker-crash-checkpoint-recovery` | Eval 5 | AgentRun 崩溃后 checkpoint 恢复 |
| 3 | `dependency.json` | `eval-02-dependency-blocking` | Eval 2 | 跨团队依赖阻塞与确定性解锁 |
| 4 | `dependency.json` | `eval-13-transactional-event-recovery` | Eval 13 | Outbox 崩溃窗口后恰好一次唤醒 |
| 5 | `rejected_contract.json` | `eval-03-rejected-contract-replan` | Eval 3 | Contract 被拒 → Planner replan |
| 6 | `verification_failure.json` | `eval-04-verification-failure-rework` | Eval 4 | 语义审查失败 → 确定性返工 |
| 7 | `budget_exhaustion.json` | `eval-07-project-budget-exhaustion-gate` | Eval 7 | 项目预算触顶 → 人工预算 Gate |
| 8 | `budget_exhaustion.json` | `eval-11-capability-capacity-exhaustion` | Eval 11 | capacity=0 → 不 dispatch → 协商 |
| 9 | `cross_team_disclosure.json` | `eval-06-malicious-context-injection` | Eval 6 | 项目资源 prompt injection 失效 |
| 10 | `cross_team_disclosure.json` | `eval-08-cross-team-disclosure-isolation` | Eval 8 | Team private 数据隔离 |
| 11 | `cross_team_disclosure.json` | `eval-10-agent-execution-identity` | Eval 10 | 执行身份 / 不冒用真实用户 |
| 12 | `stale_planner.json` | `eval-09-stale-planner-decision` | Eval 9 | 过期 Planner 决策被拒绝 |
| 13 | `delivery_rejection.json` | `eval-14-delivery-rejection-rework` | Eval 14 | Delivery 被拒 → rework |
| 14 | `delivery_rejection.json` | `eval-12-human-input-suspension-restart` | Eval 12 | InputRequest 稳定 WAITING + 重启恢复 |

与第 32 章测试计划的对应关系：Eval 1–14 是端到端行为场景；32.1–32.5 的单元级断言（guard 拒绝、乐观并发、lease/fencing、去重、payload 卫生等）作为**全局不变量**（第 6 节）注入每个场景；32.6 的 Recovery E2E 长链由 Eval 1（正常段）+ Eval 5 + Eval 4 + Eval 14 的片段组合覆盖，B6 执行器实现时可按第 9 节组合。

## 4. Fixture JSON 结构约定

每个文件顶层：

```text
fixture_schema      固定 "project-harness-fixture.v1"（仅描述本文件格式，不是领域 schema）
fixture_id / title / description
eval_coverage       [{eval_id, scenario_id}]
source_refs         指向专家计划与本规格
world               跨 scenario 共享的领域设定（teams / capabilities / principals / 默认策略）
scenarios           scenario 对象数组
```

scenario 对象七要素：

```text
scenario_id / eval_ref / summary
initial_state         process 三元组、work graph、contracts、artifacts、策略、usage、活跃 run、开放 gate/input
input_events          按序输入事件（领域事件或 harness.fault_injection 故障注入）
expected_transitions  process (phase,status,wait_reason) 迁移步骤；无迁移的窗口也显式断言
required_outputs      必须出现的可观察产物（状态/事件/artifact/audit/usage/context 断言）
forbidden_outcomes    一票否决结果
safety_invariants     场景特定安全不变量（叠加第 6 节全局不变量）
crash_and_recovery    故障注入点与恢复预期（无故障时为空数组）
contract_assumptions  该场景依赖、但尚未冻结的语义决策（对应第 10 节 OPEN-Q 编号）
```

约定：

- 所有 `*_ref` 是 fixture 内部语义引用，不是数据库 ID；
- `process_version_change` 只用 `"+1"` / `"0"`（相对断言，不用绝对值）；
- `input_events` 中 `event_type` 为 `harness.fault_injection` 的是**评测器注入指令**（不是领域事件），执行器据此在指定时机注入故障，`fault.kind` 词表见第 7 节；
- `expected_transitions` 中 `from == to` 的步骤表示"此窗口内状态必须不变"的负断言；
- 数值（token、金额、容量）均为示意值，只用于相对比较。

## 5. 领域语义约定

### 5.1 共享世界（world）

所有场景共享同一演示项目，便于执行器复用与对比：

- 项目 `project-harness-demo`，根目标 `goal-1`（交付带后端 API 与前端界面的演示应用）；
- 团队：`team-backend`（能力 `backend-api`）、`team-frontend`（能力 `frontend-ui`）；
- 任务：`task-backend-api`（实现认证 API，输出 OpenAPI artifact `resource-api-spec`）、
  `task-frontend-ui`（实现前端界面，输出 `resource-ui-build`），后者 `depends_on` 前者；
- 人类：`user-project-owner`（项目负责人，同时是 goal 确认 / plan 批准 / delivery 接受者）；
- 服务主体：`service:project-orchestrator`；团队执行主体：`team-agent:team-backend`、`team-agent:team-frontend`（计划 9.4 的 principal 书写格式）；
- 资源可见性语义枚举（待冻结，OPEN-Q10）：`team_private` / `project_shared` / `project_readonly`。

### 5.2 ProjectProcess 状态使用约定（fixture 侧，待主线程确认）

ADR-0005 冻结了主链迁移与三元组约束（`WAITING`/`BLOCKED` 必须有非 `NONE` 的 wait reason；`READY`/`RUNNING`/terminal 必须 `NONE`）。在此之上，fixture 采用以下细化约定（均为 OPEN-Q3 / OPEN-Q4）：

1. `EXECUTION/RUNNING`：本 process 至少有一个活跃 AgentRun 或 ToolJob；
2. `EXECUTION/READY`：计划已批准、存在可调度工作、但当前无活跃 run（轮次间隙）；
3. `EXECUTION/BLOCKED/TEAM_RESPONSE`：存在未完成工作但全部处于 `proposed` 待接受；
4. `EXECUTION/BLOCKED/DEPENDENCY`：存在已接受工作但依赖未满足且无其他可调度工作；
5. `EXECUTION/WAITING/HUMAN_INPUT`：存在 OPEN 的 input request；
6. `EXECUTION/WAITING/HUMAN_APPROVAL`：存在 OPEN 的 gate（预算、审批）；
7. `EXECUTION/WAITING/SCHEDULE`：工作其余条件满足但容量/调度资源未就绪；
8. 崩溃窗口内 process 三元组保持崩溃前取值不变，恢复由事件驱动重新推进。

### 5.3 事件分层约定

- **领域事件（input_events 主用）**：采用计划 8.2 wakeup 源命名风格（`team_task.accepted`、`team_task.verified`、`human.input.provided`、`approval.decided`、`risk.created` 等），是业务侧发生的事实；
- **过程迁移事件（expected_transitions.transition_event_type）**：采用 ADR-0005 主链命名（`goal.confirmed`、`analysis.started`、`plan.approved`、`work.dispatched`、`all_required_work_submitted`、`verification.failed`、`verification.passed`、`integration.passed`、`delivery.accepted`、`delivery.rejected`）；
- 两层命名在前缀上的差异（`project.goal.confirmed` vs `goal.confirmed` 等）是**未冻结契约**，见 OPEN-Q1 / OPEN-Q2；
- fixture 中少量事件名在两层清单中均未出现（如 `team_task.changes_requested`、`contract.manifest_revised`、`capacity.negotiation.*`），以语义占位并在场景 `contract_assumptions` 中标注对应 OPEN-Q。

### 5.4 计数器约定

- `process.version`：仅被**接受的 process 迁移**递增（ADR-0005 第 6 条）；
- 事件流水：每次业务事实（含未改变三元组的 wakeup 源事件）追加权威 process event，`last_event_sequence` 递增；
- Planner stale guard 同时检查 `based_on_process_version` 与 `graph_snapshot_digest`：业务事件不改 version 时由 digest 捕捉并发变化（OPEN-Q11）。

## 6. 全局安全不变量（适用于所有场景）

以下不变量来自计划 32.5 与 ADR-0005/0006/0007/0008 的 "Enforced invariants"，在每个场景中默认生效；fixture 的 `safety_invariants` 只列场景**追加**项。

- **G-INV-01 唯一迁移入口**：所有 phase/status/wait_reason 变更必须经统一迁移矩阵；任何 route/service/orchestrator/recovery/admin 路径不得直接赋值三元组；Planner command 不得指定任意 phase/status。
- **G-INV-02 版本原子性**：每个接受的迁移恰好 +1 version 并在同一次提交中追加一条权威事件；同一 expected_version 的两个竞争迁移只有一个成功。
- **G-INV-03 Terminal 不可变**：`TERMINAL` 之后不得回到非 terminal；特权恢复迁移（若存在）必须显式 reason + Audit。
- **G-INV-04 事务一致性**：推进 process 的业务变更与权威事件必须同一原子提交，或经 Transactional Outbox 由持久投递器重放；禁止业务成功后 best-effort 补发。
- **G-INV-05 幂等收敛**：重复事件 / 重复命令 / 重复投递按 event_id、(process_id, sequence)、command_id、request_digest 收敛；副作用与 usage 恰好计一次。
- **G-INV-06 双身份**：自动执行记录不可变的 `initiated_by` 与 `executed_as`；自动 run 不得以真实用户为执行主体；retry/replay/recovery 身份不变；Audit 能区分"人发起 / Harness 派发 / Team Agent 执行 / 人批准"。
- **G-INV-07 团队隔离**：team private 内容不得进入其他 team 的 context、输出或 audit payload；跨团队访问需要显式 grant；contract 不得引用不可访问 artifact。
- **G-INV-08 Context 数据非指令**：项目资源内容一律作为数据处理；Agent 工具面不包含直接写 process 状态的能力。
- **G-INV-09 预算与并发**：dispatch 前固定顺序（项目预算 → 团队并发 → run 预算派生 → dispatch）；预算触顶 → `WAITING/HUMAN_APPROVAL` + 持久 Gate；Planner command 不能提高项目预算；禁止自动重试循环。
- **G-INV-10 能力与容量**：dispatch 前必须经能力目录 match + capacity reserve 成功；不创建平行的能力注册表。
- **G-INV-11 Planner stale guard**：决策绑定 process version / event sequence / graph digest；过期决策零命令应用并触发重新规划；同一 (process_version, orchestration_reason, digest) 映射确定性 planner intent，崩溃不产生并行 Planner Run。
- **G-INV-12 完成判定**：`COMPLETED` 只能由 Harness 依据 Completion Contract 判定；`required_delivery_accepted` 未满足时不得完成；LLM 不得直接设置完成。
- **G-INV-13 Payload 卫生**：process event payload 不含模型 prompt、secret、raw tool arguments；audit 保留身份与摘要、脱敏正文。
- **G-INV-14 恢复语义**：崩溃后经 checkpoint / lease+fencing / outbox 恢复；旧 fencing token 的写入被拒绝；恢复另记 recovery 事件，不重写领域执行者身份。

## 7. 故障注入词表（crash_and_recovery 语义）

`harness.fault_injection` 的 `fault.kind` 词表（B6 执行器按此实现；词表本身为 OPEN-Q13）：

| fault.kind | 注入点语义 |
| --- | --- |
| `agent_worker_crash` | AgentRun worker 进程崩溃；`timing` 说明相对 checkpoint / terminal 写入的位置 |
| `orchestrator_worker_crash` | Orchestrator worker 在 command 应用 / 事件发布前后崩溃 |
| `publisher_crash_after_commit_before_publish` | 业务事务已提交、outbox 条目 PENDING、投递器崩溃 |
| `outbox_replay` | 恢复后对同一 outbox 条目重放 N 次（at-least-once 验证） |
| `stale_worker_resume_attempt` | 持过期 lease/fencing token 的旧 worker 尝试继续写 |
| `control_plane_restart` | 进程级重启；内存态全部丢失，仅持久态可依赖 |

恢复判定统一要求（每个含故障场景的 `crash_and_recovery` 共同语义）：

1. 故障不产生非法迁移、不产生新领域事件；
2. 恢复后状态收敛到与"未注入故障"的同一轨迹（除恢复本身的事件）；
3. 所有副作用与 usage 计数恰好一次（G-INV-05）；
4. 身份与 provenance 在恢复前后不变（G-INV-06、G-INV-14）。

## 8. 评测协议（执行器视角）

1. 执行器加载 fixture，按 `initial_state` 构造领域世界（可注入 fake 实现：Fake Planner、fake capability directory、fake 时钟）；
2. 按 `input_events` 顺序投递领域事件；遇 `harness.fault_injection` 按第 7 节注入；
3. 每个事件后对照 `expected_transitions` 断言三元组、version 相对变化与 transition 事件；
4. 场景结束时校验 `required_outputs` 全部成立、`forbidden_outcomes` 全部未发生、全局不变量（第 6 节）未违反；
5. 所有断言基于公共持久状态与审计投影，不读取会话正文、prompt 或模型私有输出。

评测必须完全离线：不依赖真实 LLM、网络、OAuth 或 `.env`；Planner 一律以 Fake Planner（脚本化 `coifesp.orchestration-decision.v1` 输出）替代。

## 9. 与 Recovery E2E（32.6）的组合

32.6 的完整故障长链可由场景片段按序拼接：

```text
Eval 1 e01–e07（create→dispatch）
→ Eval 5（Agent worker crash→recover）
→ Eval 4（submit→verification fail→返工）
→ Eval 1 e13–e17（revise→submit→verify→integration）
→ Eval 14 e08（human approve/delivery accept→complete）
```

Tool worker crash 片段未单独设 Eval，可在执行器层把 Eval 5 的 `agent_worker_crash` 换成 durable tool job 崩溃复用同一断言骨架（属于 B6 实现细节）。

## 10. 未冻结契约问题清单（需主线程裁定）

Fixture 刻意只使用专家计划 / ADR 文本中已出现的语义名称。以下是设计过程中暴露、需要主线程冻结后回填 fixture 的决策点：

- **OPEN-Q1 事件命名前缀不一致**：计划 4.4 主链用 `goal.confirmed` / `plan.approved` / `verification.passed`，计划 8.2 wakeup 源用 `project.goal.confirmed` / `project.plan.approved` / `verification.completed`。fixture 按 5.3 两层约定使用；需主线程发布统一事件表。
- **OPEN-Q2 analysis 阶段事件**：`analysis.started` / `analysis.completed` 在主链中存在，但不在 8.2 wakeup 源清单。fixture 假设它们唤醒 orchestrator。
- **OPEN-Q3 RUNNING 与 WAITING 的边界**：等待活跃 AgentRun / ToolJob 完成时 process 是 `EXECUTION/RUNNING` 还是 `EXECUTION/WAITING/AGENT_RUN`？fixture 采用第 5.2 节约定（有活跃 run 即 RUNNING）。
- **OPEN-Q4 BLOCKED 的 wait_reason 选择**：等团队接受 contract 用 `TEAM_RESPONSE`、依赖未满足用 `DEPENDENCY`、容量未就绪用 `SCHEDULE`，为 fixture 约定，需冻结确认（尤其容量等待是否用 `SCHEDULE`）。
- **OPEN-Q5 task 级验证失败事件**：fixture 用 `verification.failed`（payload 引用 `coifesp.verification-result.v1`，`passed=false`）；`team_task.changes_requested` 领域事件名未在 8.2 清单出现，为语义占位。
- **OPEN-Q6 reopen 语义**：`changes_requested` 后 task 回到哪个状态（fixture 表述为"可由同一团队再次执行/提交"，不新增枚举值）；"确定性 reopen 不计入 replan 计数"是否成立。
- **OPEN-Q7 delivery 拒绝事件与 acceptor**：`delivery.rejected` 的领域事件形式、payload（拒绝原因）与 acceptor 角色（人工 gate / `required_human_approvers`）未定义；fixture 用语义占位。
- **OPEN-Q8 Gate decision 取值**：预算 Gate 的决定值（fixture 用 `increase_budget` / `scope_reduction` / `terminate`）与 policy 版本升级规则未冻结。
- **OPEN-Q9 InputRequest 回答事件**：`human.input.provided` 在 8.2 清单中存在；request→ANSWERED 的字段语义（answered_by/answered_at）取自计划 4.6 文本，需随 Gate/InputRequest 契约一并冻结。
- **OPEN-Q10 资源可见性枚举**：fixture 用 `team_private` / `project_shared` / `project_readonly`（由计划 10.2 / 12.2 / 28.1 的语义归纳），完整枚举需冻结。
- **OPEN-Q11 process.version 与 stale guard**：业务事件是否递增 process.version？fixture 约定仅迁移递增、并发变化由 graph digest 捕捉；若主线程决定业务事件也递增 version，fixture 的 stale 断言同样成立（两条件为 OR），但需明确。
- **OPEN-Q12 planner intent 去重键**：`(process_version, orchestration_reason, graph_snapshot_digest) → planner_intent_id` 来自计划 7.4/27.6；`orchestration_reason` 的取值域未定义。
- **OPEN-Q13 故障注入词表**：第 7 节 `fault.kind` / `timing` 为 B2 提议，需 B6 执行器与主线程评审冻结。
- **OPEN-Q14 容量协商事件**：`capacity.negotiation.*` 为语义占位；现有 Capability/Negotiation 领域的真实事件名以主线程冻结为准（fixture 只断言"协商请求恰好创建一次"与"未决前不 dispatch"）。

## 11. 校验与验收

- 全部 8 个 fixture 通过 JSON 解析（UTF-8、无重复键、无注释）；
- 每个 scenario 七要素齐全（`crash_and_recovery` 允许为空数组，其余必非空）；
- 覆盖矩阵（第 3 节）与各文件 `eval_coverage` 一致，Eval 1–14 无遗漏；
- 不含：数据库表/列名、Python 类名、API 路径、UUID、绝对本机路径、真实凭据、`.env` 引用。

校验方式：`python -c "import json,glob;[json.load(open(p,encoding='utf-8')) for p in glob.glob('tests/fixtures/project_harness/*.json')]"`（或等价脚本）。
