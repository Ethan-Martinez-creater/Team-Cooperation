# Project Harness Evaluation 规格与确定性 Fixture（B2，v2）

- 状态：Aligned to Gate 0 contracts（`project-harness-contract-v1.json`、`project-process-event-catalog-v1.md`、`project-harness-fixture-contract-v1.md` 及 ADR-0002…0011，均 Accepted）。本文件不再保留任何 OPEN-Q；fixture 与测试是契约的符合性实现，不做独立裁决。
- 任务来源：`docs/plan/iteration/Multi-Agent-Harness-Parallel-Execution-Coordination.md` B2。
- 交付物：本规格 + `tests/fixtures/project_harness/*.json`（8 文件、14 场景，覆盖 Eval 1–14）+ `tests/test_project_harness_evaluation_fixtures.py`（符合性与负向验证）。
- 边界：只定义评测输入、期望状态和安全不变量；不实现 ProjectProcess；不修改源码、迁移或既有测试。

## 1. 规范来源与优先级

1. `docs/adr/project-harness-contract-v1.json`（机器契约）：全部枚举、主链迁移、版本策略、身份规则、rework 策略的唯一来源。
2. `docs/adr/project-process-event-catalog-v1.md`（事件目录）：domain fact / transition key / audit event 三层命名。
3. `docs/adr/project-harness-fixture-contract-v1.md`（fixture 契约）：场景结构、stimulus/观察边界、expected step、身份与状态规则、loader 义务。
4. ADR-0002…0011：领域决策（Work Graph、Contract、Orchestrator、迁移矩阵、预算、身份、事务事件、Gate/Input、能力目录、集成交付）。

fixture 中出现的每一个 event_type、枚举值、策略字段名都可在机器契约中逐字找到；transition key 仅使用主链迁移表冻结的 key（其余迁移不锁定 key，见 §5）。

## 2. 场景覆盖矩阵

| Fixture | Scenario | Eval |
| --- | --- | --- |
| simple_project.json | eval-01-simple-project / eval-05-worker-crash-checkpoint-recovery | 1 / 5 |
| dependency.json | eval-02-dependency-blocking / eval-13-transactional-event-recovery | 2 / 13 |
| rejected_contract.json | eval-03-rejected-contract-replan | 3 |
| verification_failure.json | eval-04-verification-failure-rework | 4 |
| budget_exhaustion.json | eval-07-project-budget-exhaustion-gate / eval-11-capability-capacity-exhaustion | 7 / 11 |
| cross_team_disclosure.json | eval-06-malicious-context-injection / eval-08-cross-team-disclosure-isolation / eval-10-agent-execution-identity | 6 / 8 / 10 |
| stale_planner.json | eval-09-stale-planner-decision | 9 |
| delivery_rejection.json | eval-14-delivery-rejection-rework / eval-12-human-input-suspension-restart | 14 / 12 |

与第 32 章 Recovery E2E（32.6）的组合：Eval 1 的正常主链段 + Eval 5 的崩溃恢复段 + Eval 4 的返工段 + Eval 14 的交付接受段拼接成完整故障长链；Tool worker 崩溃片段由执行器层将 Eval 5 的 `agent_worker_crash` 替换为 durable tool job 故障复用同一断言骨架（B6 实现细节）。

## 3. Fixture 文件结构

```text
fixture_schema   "project-harness-fixture.v2"
fixture_id / title / description
contract_refs    指向三个 Gate 0 契约文件
eval_coverage    [{eval_id, scenario_id}]
scenarios        场景数组（无跨文件继承；world 语义并入各场景 initial_state）
```

scenario 结构（fixture 契约 §Executable scenario structure）：

```text
scenario_id / eval_ref / summary
initial_state       自包含规范化状态（见 §4）
stimuli             输入事实序列（见 §5）
expected_steps      与 stimuli 一一对应、按序（见 §6）
required_outputs    场景级必达断言（引用稳定 ID / 幂等键）
forbidden_outcomes  一票否决结果
safety_invariants   场景附加不变量（叠加 §8 全局不变量）
crash_and_recovery  故障注入点与恢复预期（无故障为空数组）
```

## 4. initial_state 规范化（自包含）

每个场景的 `initial_state` 显式包含以下集合与标量；缺失集合由 loader 规范化为空，缺失标量无效（fixture 契约 §initial_state）：

| 键 | 内容 |
| --- | --- |
| `process` | `process_ref`、`project_ref`、`phase/status/wait_reason`（契约枚举）、`process_version`（相对整数）、`last_event_sequence`（相对整数） |
| `graph` | `nodes`（node_type ∈ work_node_type）、`relations`（relation_type ∈ work_relation_type）、`graph_snapshot_digest`（结构化操作数） |
| `contracts` | 请求方/提供方团队、requested_capability、task_status ∈ team_task_status、input_manifest、output_contract、contract_version |
| `artifacts` | `propagation` ∈ resource_propagation（`team_private` / `project_readonly` / `portable`）、version |
| `verifications` | verification 记录（subject、checks、outcome PASS/FAIL） |
| `integration` / `delivery` | IntegrationRun、DeliveryManifest（status ∈ delivery_status）；`completion_contract` |
| `execution_policy` | 机器契约 `project_execution_policy_fields` 全部 11 字段，含 `version` |
| `usage` | runs started/completed、tokens、cost、replans、generated tasks、active runs |
| `runs` / `tool_jobs` | 自动 Run 必含 `initiated_by` 与 `executed_as`；`execution_state` ∈ fixture 内部词表 `running/queued/terminal/interrupted` |
| `reservations` | reservation 状态 ∈ fixture 内部词表 `reserved/released/failed` |
| `capabilities` | 能力与容量（供 ADR-0010 match/reserve 语义） |
| `gates` / `input_requests` | status ∈ gate_status / input_request_status |
| `pending_events` / `outbox` | 未消费 wakeup 与待投递 outbox 条目 |

禁止跨文件隐式继承：任何场景不得引用其他 fixture 文件的内容。

**结构化版本操作数**：版本/序列/摘要引用使用 `{"operand": ..., "ref"/"value": ...}` 结构，不允许 prose 字符串。本规格冻结三个操作数（fixture 内部词表）：

```text
{"operand": "initial_state"}                     场景初始值
{"operand": "process_version_at_run_creation", "ref": "<run_ref>"}
{"operand": "graph_digest_at_run_creation",    "ref": "<run_ref>"}
{"operand": "literal", "value": "sha256:..."}  显式摘要
```

## 5. Stimulus（输入事实）与观察边界

每个 stimulus：

```text
source_event_id   场景内稳定 ID
event_type        ∈ 机器契约 domain_fact（fault 除外）
origin            ∈ fixture_stimulus_origin：HUMAN / TEAM / AGENT_WORKER / TOOL_WORKER / TIMER / FIXTURE_FAULT
subject_ref       事件主体
idempotency_key   幂等键（稳定、可复放）
payload           有界载荷（无 prompt/secret/raw tool arguments）
```

**观察边界**（fixture 契约 §Stimulus versus observation）。以下 Harness 生成结果只能出现在 `expected_steps` 的 emitted facts / created objects 中，禁止作为 stimulus 注入：

- 迁移事实与 dispatch 成功：`project.work.dispatched`、`project.work.required_submitted`、`project.analysis.completed` 的迁移效应；
- 验证结果：`team_task.verified`、`team_task.changes_requested`、`project.verification.completed`；
- 交付就绪与完成：DeliveryManifest `READY`、`project.delivery.accepted` / `project.delivery.rejected` 的迁移效应、`project.completion.evaluated`；
- Gate/Input 创建：`project.gate.opened`、`project.input.requested`、`project.budget.exhausted`；
- 容量事实：`project.capability.matched`、`project.capacity.reserved`、`project.capacity.reservation_failed`（dispatch 流程的 Harness 输出）。

**允许的 stimulus 词表**（origin → event_type）：

| origin | event_type | 语义 |
| --- | --- | --- |
| HUMAN | `project.goal.confirmed` | 目标确认 |
| HUMAN | `project.plan.approved` | 计划批准 |
| HUMAN | `approval.decided` | Gate / 交付接受决定（decision ∈ budget_gate_decision / delivery_decision） |
| HUMAN | `human.input.provided` | InputRequest 回答 |
| HUMAN | `project.capacity.negotiation_resolved` | 容量协商由授权人解除 |
| TEAM | `team_task.accepted` | 授权目标团队接受契约（ADR-0003 决策 5） |
| TEAM | `team_task.rejected` | 授权目标团队拒绝契约 |
| AGENT_WORKER | `project.analysis.started` | 分析 run 认领并开始 |
| AGENT_WORKER | `agent_run.completed` | run 终止（planner 决策随 run 产出，由 Orchestrator 校验应用） |
| AGENT_WORKER | `team_task.started` / `team_task.submitted` | 团队 agent 开始 / 提交 |
| AGENT_WORKER | `risk.created` / `risk.resolved` | run 执行中的风险事实 |
| TOOL_WORKER | `project.integration.completed` | IntegrationRun 执行完成（结构化 PASS/FAIL 由 IntegrationRun 记录，迁移判定仍属 Harness） |
| FIXTURE_FAULT | `fixture.fault_injection` | 控制输入，非领域事件；`fault.kind` ∈ fixture_fault_kind |

`project.verification.completed`、`team_task.verified`、`team_task.changes_requested`、`project.work.dispatched` 等 Harness 判定结果**永远不作为 stimulus**，只出现在 emitted facts。

## 6. Expected steps

**恰好一个 expected_step 对应一个 stimulus，按序**（fixture 契约 §Expected steps）。每 step：

```text
step_id                 场景内稳定 ID
stimulus_ref            对应 stimulus 的 source_event_id
expected_process        窗口结束三元组 {phase, status, wait_reason}
process_version_change  0 或 1（每 step 至多一次迁移）
event_sequence_change   == len(emitted_domain_facts)（每条权威 fact 恰好 +1 sequence）
emitted_domain_facts    [{event_type, subject_ref, outcome?, transition_key?, transition?{from,to}}]
active_operations       活跃 run/job/verification/integration 引用；expected_process.status == RUNNING 时必须非空
created_or_updated      持久对象创建/更新（含稳定 ID 与幂等键）
commands                Orchestrator command（type ∈ planner_command_type）及其稳定 command ID
observations            引用稳定 ID 的机器断言（自然语言仅作说明）
```

约定：

- `event_sequence_change == len(emitted_domain_facts)` 由测试强制（version_policy.domain_fact_advances_event_sequence）；
- `process_version_change == len(facts with transition)` 由测试强制（only_transition_advances_process_version）；
- **三元组不变的领域事实不增加 process version**：事实在 RUNNING 状态被消费且三元组保持不变时（如运行中连续调度），该事实不携带 transition，version 增量为 0；
- **transition 必须改变三元组**：`transition.from == transition.to` 一律非法，validator 拒绝；不存在"伪再入迁移"；
- **连续调度替换活跃操作不产生状态迁移**：同一消费事务中 orchestrator 可以把 active operation 从旧 run 换成新 run（dispatch 成功事实仍记录、sequence 前进），process 停留 RUNNING、version 不变；不再使用"隐藏 READY 间隙"解释此类步骤；
- 迁移链连通性：每条 transition 的 `from` 必须等于前窗末三元组（即 fact 消费时刻的实际三元组），`to` 必须等于本窗 expected_process；
- transition 的 `transition_key` 仅在主链迁移表冻结 key 时填写（`goal.confirmed`、`analysis.started`、`analysis.completed`、`plan.approved`、`work.dispatched`、`all_required_work_submitted`、`verification.failed`、`verification.passed`、`integration.passed`、`delivery.accepted`、`delivery.rejected`）；transition 的 from/to 与主链行匹配时必须携带该行的 key；非主链迁移的 `transition_key=null` 表示 fixture 不断言尚未冻结的内部矩阵 selector 名称，**不代表真实持久化事件可以没有 selector**（Guard 实现仍须为其分配内部 key）；
- “恰好一次”断言必须落在稳定 ID / 幂等键上：run ref、command id、reservation id、manifest id、input request id、negotiation id、usage 计数器、wakeup 去重键 `(process_ref, source_event_id)`；
- 无迁移窗口使用显式不变三元组（from == to 的负断言语义）。

## 7. 身份与状态规则（fixture 契约 §Identity and state rules）

- Planner / analysis / planning / verification / replanning run：`initiated_by = service:project-orchestrator`、`executed_as = service:project-orchestrator`（execution_identity.planner_executed_as）；
- 任务执行 run：`initiated_by = service:project-orchestrator`、`executed_as = team-agent:<team-id>`；
- 所有自动 run 双身份必填；`initiated_by` 是 provenance 不是授权（ADR-0007）；
- `RUNNING` ⟹ `active_operations` 非空（测试强制）；排队/仅恢复中的 run 不支撑 RUNNING（ADR-0005 决策 3）；
- 契约接受、容量预留、readiness 是相互独立的事实（ADR-0010 决策 7）；
- rework：`team_task.changes_requested` 将任务回到 `in_progress`；确定性任务返工不计 replan；图/范围变更计 replan（rework_policy）；
- Planner stale guard 输入：process version、last_event_sequence、graph digest（version_policy.planner_stale_inputs）。

## 8. 全局安全不变量（每场景默认生效）

- **G-INV-01 唯一迁移入口**：一切三元组变化经迁移矩阵；Planner command 不得指定 phase/status。
- **G-INV-02 版本原子性**：一次迁移恰好 +1 version +1 权威事件；同 expected_version 竞争只有一个赢家。
- **G-INV-03 Terminal 不可变**：TERMINAL 后不回到非 terminal。
- **G-INV-04 事务一致性**：推进 process 的业务变更与权威事件同一原子提交或经 Transactional Outbox（ADR-0008）。
- **G-INV-05 幂等收敛**：重复事件/命令/投递按 event_id、(process, sequence)、command_id、request_digest、wakeup 键收敛；副作用与 usage 恰好一次。
- **G-INV-06 双身份**：自动 run 记录不可变 initiated_by/executed_as；不冒用真实用户；恢复不重写执行者。
- **G-INV-07 团队隔离**：team_private 内容不进入其他 team context/输出/audit；跨团队需显式 grant；契约不得引用不可访问 artifact。
- **G-INV-08 Context 数据非指令**：资源正文仅是数据；agent 工具面无 process 状态写。
- **G-INV-09 预算与并发**：dispatch 前固定顺序（项目预算 → 团队并发 → run 预算）；触顶 → WAITING/HUMAN_APPROVAL + 持久 Gate；Planner 不能提预算；无自动重试循环。
- **G-INV-10 能力与容量**：唯一能力事实源（ADR-0010）；容量耗尽不 dispatch，process 为 WAITING/SCHEDULE。
- **G-INV-11 Planner stale guard**：三输入（version/sequence/digest）任一不匹配 → 整体拒绝零命令应用 + `project.orchestrator.decision_stale`；intent 确定性派生（ADR-0004 决策 8）。
- **G-INV-12 完成判定**：仅成功 evaluator 经 `delivery.accepted` 到 TERMINAL/COMPLETED；无接受交付不得完成（ADR-0011）。
- **G-INV-13 Payload 卫生**：fact/audit 载荷无 prompt、secret、raw tool arguments。
- **G-INV-14 恢复语义**：checkpoint/lease+fencing/outbox 恢复；旧 fencing 写入被拒；恢复另记事件不改身份。

## 9. BLOCKED wait reason 选择约定

ADR-0005 冻结了语义（契约接受等待 → TEAM_RESPONSE；图前置未满足 → DEPENDENCY；等返工 → VERIFICATION；容量/调度延迟 → WAITING/SCHEDULE）。当多个条件并存时，fixture 采用固定优先级 **TEAM_RESPONSE > DEPENDENCY > VERIFICATION**（仍有未决契约时以契约为准）。这是 fixture 断言约定，不改变 ADR 语义；若 B6 实现矩阵采取不同优先级，仅需同步更新对应场景的期望三元组。

## 10. 主链尾部统一模式

所有以交付接受收尾的场景使用同一观察模式（减少 fixture 重复并保持一致）：

```text
[agent_run.completed(最后任务 run)]
  → emitted: agent_run.completed + team_task.verified + project.work.required_submitted
  → VERIFICATION/READY，创建 verification run（queued）
[agent_run.completed(verification run, executed_as=service:project-orchestrator)]
  → emitted: agent_run.completed + project.verification.completed{outcome PASS}
  → INTEGRATION/READY，创建 IntegrationRun
[TOOL_WORKER project.integration.completed{outcome PASS}]
  → DELIVERY/READY，DeliveryManifest ASSEMBLING→READY（Harness 输出）
[HUMAN approval.decided{decision ACCEPT, 授权角色}]
  → emitted: approval.decided + project.delivery.accepted + project.completion.evaluated
  → TERMINAL/COMPLETED
```

项目级验证确定性聚合（deterministic）通过 verification run 承载；IntegrationRun 执行由 tool worker 完成，其完成事实（含结构化结果）是 TOOL_WORKER 输入，迁移判定与 manifest READY 属 Harness 输出。

## 11. 验证与测试

`tests/test_project_harness_evaluation_fixtures.py`（完全离线、无 LLM/网络/Docker/OAuth/.env）提供：

1. **加载**：8 个 JSON 可解析、无重复键；
2. **覆盖**：Eval 1–14 恰好完整、scenario 与 eval_coverage 一致；
3. **结构**：七要素齐全；stimuli 与 expected_steps 一一对应且按序；
4. **契约符合**：event_type/origin/枚举/策略字段全部来自机器契约；transition key 仅来自主链表；
5. **一致性**：`event_sequence_change == len(emitted facts)`；`process_version_change == len(迁移 facts)`；RUNNING ⟹ active_operations 非空；
6. **身份**：planner/analysis/planning/verification run `executed_as=service:project-orchestrator`；任务 run `team-agent:` 前缀；自动 run 双身份；
7. **引用**：所有 `*_ref` 可解析；无跨文件继承（initial_state 必需集合齐全）；
8. **负向**：对每类违规（未知事件、未知枚举、缺 step、身份矛盾、状态矛盾、seq 等式破坏、policy 缺字段、未知 Gate decision、未知 origin、跨文件继承）以变异 fixture 断言 validator 拒绝。

运行：

```powershell
python -m pytest tests/test_project_harness_evaluation_fixtures.py -q
python -m pytest tests/test_project_harness_architecture_contract.py -q   # 相关既有契约测试
```
