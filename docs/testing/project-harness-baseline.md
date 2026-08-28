# Project Harness 改造前基线回归冻结（B1）

- 基线分支：`main`
- 基线提交：`7d6934db1f7e66b9d68261ae47f5de34886e526f`
- Alembic head：`20260826_45`
- 全量回归基线：`521 passed / 1 skipped`
- 冻结文件：
  - `tests/test_project_harness_legacy_baseline.py`（主链 + 幂等 + 隔离 + Capability + 双模式）
  - `tests/test_project_harness_baseline_failures.py`（崩溃重放 + 失败/恢复路径）
  - `docs/testing/project-harness-baseline.md`（本文）

对应 `Team-Cooperation_Multi-Agent_Harness_Execution_Plan_v1.1_Audited.md` 的 Phase 0
“在大改之前固定现有行为”，以及并行协调方案 Wave 0 的外部任务 B1。本任务不实现
ProjectProcess、WorkGraph 或 Orchestrator，只冻结改造前的可观察行为。

## 运行方式

```bash
python -m pytest tests/test_project_harness_legacy_baseline.py tests/test_project_harness_baseline_failures.py
```

完全离线：内存 SQLite（`StaticPool`）、fake run reader、fake artifact repository、
fake replay run service；不依赖真实 LLM、网络、OAuth、Docker、`.env` 或本地密钥。

Windows 环境注意：若 `C:\Users\<user>\AppData\Local\Temp\pytest-of-PC` 存在权限问题，
`tmp_path` fixture 会对部分现有测试报 `PermissionError (WinError 5)`。用
`--basetemp=<可写目录>` 即可复现完整 `521 passed / 1 skipped` 基线。这与基线代码无关。

## 被冻结的关键路径主链

```text
Project create
→ project conversation
→ planning run projection
→ plan approve
→ TeamTask materialization
→ task accept
→ task in_progress
→ artifact/project resource
→ task submit
→ task verify
```

由 `test_baseline_main_chain_project_to_verified_task` 端到端覆盖：planning turn 的
终态投影导入 `coifesp.project-plan.v1` 计划草稿；owner 团队 approve 后按
`team_category` 物化跨团队 TeamTask（proposed）；目标团队 accept → 内部 assign →
in_progress → 以本人团队拥有的、非 team-private 的项目资源 submit → 来源团队
review verify（`completed_at`、`review_note`、`artifact_resource_ids` 落定）。

## 永久不变量（Harness 改造后必须继续成立）

1. **TeamTask 状态机与授权**：`proposed → accepted → in_progress → submitted →
   verified`，旁路 `changes_requested / rejected`。只有目标团队可
   accept/start/submit，只有来源团队可 review；`start` 要求已分配内部负责人；
   不允许跳级（proposed 不能直接 in_progress/submit/verify）。
2. **AgentRun 终态投影幂等**：同一 run 的重复终态回调（worker 重试、进程重启）不得
   重复追加会话消息、不得重复导入计划草稿或 Exchange 草稿。一个 `source_run_id`
   恰好对应一个计划草稿。
3. **Plan import 崩溃重放安全**：approve 的业务投影先于草稿定稿；所有投影对象使用
   由 draft id 派生的确定性 id；中途崩溃后草稿保持 DRAFTING，重试收敛且不重复
   任何 topic/task；草稿以条件更新收尾，并发 approve 不能二次确认。
4. **数据隔离（team-private / project-readonly）**：
   - `team_private` 资源仅 owner 团队可见；owner 自己也不能 SAVE/RESHARE 传播；
   - `project_readonly` 资源仅项目团队在项目上下文内 VIEW/AGENT_USE，禁止
     DOWNLOAD/SAVE/RESHARE（“只读不出口”）；
   - 提交到 TeamTask 的资源必须项目可见（非 team-private）且属于目标团队；
   - Exchange 共享包在**发布边界**拒绝携带 team-private 资源；
   - recipient 的 context 快照只含共享资源与自己团队的私有资源，绝不泄露其他
     团队的私有资源。
5. **Exchange Draft/Reply 人工确认边界**：Agent 只能起草（源侧 draft、recipient 侧
   reply draft），发送/提交必须由人执行（approve_draft / submit_response）；乐观锁
   `expected_version` 保护编辑与发布；每个团队只能回复一次；全部 recipient 回复后
   exchange 才变为 RESPONDED；exchange 内容只对来源团队与被指派团队可见。
6. **失败恢复语义**：run 失败/取消时 turn 终结、用户消息保留、可立即重试；recipient
   侧 drafting 绑定在失败/取消时释放（团队回到 PENDING，不卡死在“草拟中”）；
   `replay_pending` 启动扫描补齐“run 已终态但投影未落”的 ACTIVE turn，重放幂等。
7. **Capability Directory match/reserve/release**：publish 幂等（idempotency_key）、
   元数据防泄漏扫描；match 只在容量新鲜、可见、clearance/compartments/residency
   全部满足时命中且带可解释打分；reserve 幂等重放不重复扣槽、槽满即 match 失败、
   超订冲突；release 释放槽位后可重新预约。新 Orchestrator 必须复用该目录，
   不得另建平行能力注册表。

## 迁移期兼容行为（不应长期保留）

以下行为按专家计划第 19 章（Phase A–E）会在改造过程中被替换或删除；在此之前
它们被冻结，防止无意识变更：

1. **Product/Governance 双任务模型并存**：`Project/TeamTask`（Product 域）与
   `Program/PlanRecord/TaskAssignment`（Governance 域）同时可用且互不联动。
   创建 TeamTask 不会产生 TaskAssignment；二者状态机语义也不同（TeamTask 拒绝
   回 `changes_requested`，Governance 拒绝回 `in_progress`）。
   → A5 领域收敛后 Product TeamTask/WorkGraph 将成为唯一事实源。
2. **TaskExecutionService 的 Governance 门**：durable execution 的 enqueue 目前
   读取 `AssignmentState.IN_PROGRESS`（而非 TeamTask.status）作为放行条件。
   → Phase B/C 将新增 `enqueue_project_work` 并最终切换校验目标。
3. **Exchange 承担全部跨团队沟通**：Informational / Work Request / Blocker 三类
   消息当前都走 Exchange 自由文本。
   → 类型 B/C 将演进为 TeamTask Contract 与 WorkGraph relation。
4. **Turn projection 的 legacy correlation 兼容**：投影兜底匹配同时兼容
   `conv:`、当前 `exchange-draft:conv:` 与旧版 `exchange-draft:` 前缀。
   → 旧格式数据退场后收窄。
5. **AgentRun checkpoint 不携带项目状态**：Project 状态与 Agent 执行原语仅通过
   run binding / correlation 关联（这是永久原则，但其投影层 v2 将增加
   task_execution / verification run 类型，当前仅 user_message / planning /
   exchange / exchange_draft 四种 trigger）。

## 覆盖矩阵

| 冻结点 | 用例 |
|---|---|
| 主链 create→…→verify | `test_baseline_main_chain_project_to_verified_task` |
| 计划投影活动事件 | `test_baseline_task_schedule_and_activity_events_are_recorded` |
| 终态投影幂等（重复回调） | `test_agent_run_terminal_projection_is_idempotent_across_retries` |
| 计划导入一 run 一草稿 | `test_planning_projection_imports_exactly_one_draft_per_run` |
| Exchange Draft/Reply | `test_exchange_draft_reply_baseline` |
| Exchange 参与方可见性 | `test_exchange_visibility_follows_involvement` |
| team-private / project-readonly 隔离 | `test_resource_isolation_team_private_and_project_readonly` |
| 提交与发布泄漏边界 | `test_task_submission_and_exchange_reject_team_private_resources` |
| Capability match/reserve/release | `test_capability_publish_match_reserve_release_baseline` |
| Product/Governance 并存 | `test_product_and_governance_task_modes_run_side_by_side` |
| Governance IN_PROGRESS 门 | `test_governance_gate_still_requires_in_progress_assignment_state` |
| 计划导入重放收敛 | `test_plan_import_replay_converges_on_single_draft` |
| 计划载荷校验 | `test_plan_import_rejects_invalid_payloads` |
| approve 授权与重复确认 | `test_non_owner_cannot_approve_and_approved_draft_cannot_reapprove` |
| approve 崩溃重放不重复 | `test_plan_approve_crash_replay_does_not_duplicate_projection` |
| 启动 replay 补投影 | `test_startup_replay_completes_terminal_run_projection_once` |
| 失败 run 释放 drafting 绑定 | `test_failed_exchange_reply_run_releases_drafting_turn_and_allows_retry` |
| Exchange 乐观锁与越权读 | `test_exchange_draft_version_conflicts_and_uninvolved_reads` |
| TeamTask 迁移守卫 | `test_team_task_transition_guards_reject_unauthorized_moves` |
| changes_requested 循环 | `test_changes_requested_loop_reenters_in_progress_and_verifies` |
| proposed 冻结 | `test_task_lifecycle_before_accept_is_frozen` |
| Governance 拒绝回 in_progress | `test_governance_submit_requires_artifact_and_reject_returns_to_in_progress` |

## 维护约定

- 改动任一被冻结行为时，必须先更新本文件与对应测试，并在 ADR/迭代计划中说明
  该变更是“永久语义变化”还是“迁移期替换落地”。
- 迁移期兼容项落地替换后，应同步删除对应断言并在此记录移除提交。
- 本基线不覆盖 Alembic migration smoke、真实 PostgreSQL、Worker/Recovery E2E、
  真实模型调用——这些仍在主线程的关键路径职责内（协调方案 §2.1）。
