# 任务交付物验证

结构化 TeamTask 的 `submitted` 不是 `verified`。验证针对服务器保存的提交 Run、契约版本、制品 SHA/大小和固定 verification policy；任务执行 Agent 的总结不是验证证据。

## 检查类型

- `artifact.sha256`：从制品存储读取字节并核对大小与 SHA256。
- `artifact.json`：在完整性通过后验证 JSON；解析预算不足时保持 PENDING，不猜测通过。
- `sandbox.profile:<profile_id>`：将必需检查派发给持久 Tool Worker，执行管理员配置的固定程序。
- `agent_review`：必需工具检查通过后启动独立、受预算约束的审核 Run，读取固定共享制品，产生结构化审核证据。
- `human_review`：必需自动检查通过后，等待任务发起团队的真实用户确认固定证据；不能由执行 Agent 自行通过。
- 尚未接入的工具保持 PENDING，不能以空实现代替实际检查。

例如任务的 `verification_policy`：

```json
{
  "criteria": [
    {"criterion_id": "content", "type": "tool_check", "tool": "artifact.sha256", "required": true},
    {"criterion_id": "tests", "type": "tool_check", "tool": "sandbox.profile:project-pytest", "required": true}
  ]
}
```

始终额外执行必需的制品完整性基线。任何必需 FAIL 使任务进入 changes_requested；全部必需检查 PASS 才进入 verified。可选检查不能阻塞提交完成；当前可选外部 profile 不派发，明确显示 `optional_tool_not_scheduled`，不会被标为已运行或 PASS。

## 固定程序与输入

控制面和 Tool Worker 使用同一套 `COIFESP_SANDBOX_PROFILES_JSON`。验证 profile 必须：

1. 镜像锁定到 SHA256 digest，程序路径和 fixed_arguments 由管理员配置。
2. `workspace_access` 为 `read_only`，`arguments_schema` 能接受空数组。验证不接受模型动态参数。
3. 配置真实 OCI runtime、工作目录以及同一制品存储；工具所需依赖预先装入镜像。执行时不联网安装包。
4. 固定程序读取 `verification-inputs.json` 所描述的制品，不假定工作区就是宿主仓库。源码压缩包需要镜像内的受控包装程序读取、检查并展开到容器临时目录；宿主不会自动解压任意上传文件，也不执行宿主 pytest/build。

`project-pytest` 只是配置 ID，不自带 pytest 环境。实际镜像应包含约定的检查程序，例如 `/opt/verification/check_project.py`，其固定命令负责对提交的项目运行 pytest。仅添加一个名称而没有真实镜像/检查程序不能使验证通过。lint、build 或 OpenAPI 检查可按同一方式定义各自 profile。

验证队列的内部工具名是 `verification.run_profile`，不发布到 Agent 模型工具目录。参数仅包含 verification、criterion、subject digest 和 profile digest；制品内容及路径不由调用者传入。执行器从持久验证记录取得固定快照，并校验当前 Job/Run/团队绑定。

制品准备有数量及总大小限制（128 个 / 64 MiB）。只写入隔离的 Job 输入目录；不得追随符号链接或复用不匹配内容。程序只能读取已经固定的输入。沿用现有沙箱的网络禁用、资源限额和只读挂载，不新增身份协议。

## 结果与恢复

- 验证记录、Job 派发与提交绑定在同一个数据库事务中保存；重复验证复用 Job。
- Profile digest 固定镜像、固定参数和资源限制。修改配置不会重新解释已派发的证据；执行未完成的旧 Job 时应保持或恢复其原 profile。
- 只有正确绑定的结构化工具结果才作为证据；正常退出 0 为 PASS，非零为 FAIL。
- 超时、截断、无效结果和运行环境故障不能通过验证。基础设施故障保留 PENDING，区别于测试断言失败。
- Tool Worker 每轮执行前后恢复本团队验证，控制面启动也会重放；扫描有界轮转，等待其他能力的旧记录不会永远阻塞后续记录。
- 任务或契约变更后，旧验证变为 STALE。即使旧工具随后成功，也不能批准新提交。
- 项目事件仅携带验证状态及关联 ID，不传播原始 stdout/stderr、对话或团队内部文件。

任务双方可通过现有授权接口操作：

```text
POST /v1/projects/{project_id}/tasks/{task_id}:verify
GET  /v1/projects/{project_id}/tasks/{task_id}/verifications
POST /v1/projects/{project_id}/tasks/{task_id}:retry-verification-tools
```

第三个接口用于环境恢复后的显式重试，不接受 caller-supplied PASS/FAIL，也不能覆盖已完成的业务 FAIL。只有耗尽自动重试、结果无效或执行不完整的 Job 会创建下一次尝试，保留旧 Job ID；正在执行的检查不会重复派发。每份提交最多 10 次显式工具尝试，每次沿用 Tool Job 的有限自动重试。恢复轮询不会无限创建新 Job。

## 范围

这条链路已包含任务级工具、Agent、Human 与必需检查组合验证，不等于完整项目验收。自动返工调度、IntegrationRun、DeliveryManifest 和项目完成判定仍须分别接通。人工审核已有服务/API，原生前端审核面板属于后续 UI 阶段；没有声称浏览器已经可完成本轮新增操作。单元测试中的模拟 OCI/模型结果也不代表真实 Docker、PostgreSQL 或 LLM 环境验收。

## 独立 Agent Review

在上述 policy 的 criteria 中加入：

```json
{"criterion_id": "requirements-and-design", "type": "agent_review", "required": true}
```

审核使用单独的 Durable Agent Run，`initiated_by` 和 `executed_as` 均为 `service:project-orchestrator`，归属于任务发起团队，而非执行任务的 Team Agent。模型路由限制沿用源 Run 的已批准配置；不会复制其对话、内部总结、记忆、工具和技能权限。审核输入只有已接受任务的要求、criterion 与提交时固定的共享制品全文及引用 ID。输入 JSON 是待审数据，不是授权指令。

当前审核支持 UTF-8 文本、JSON/XML 制品；整个输入 JSON 上限 16,000 字节，不静默截断。二进制、过大输入或读取能力不可用时保持 PENDING/`review_input_unavailable`。可选 Agent Review 当前不派发，显示 `optional_review_not_scheduled`。每个必需 criterion 对应独立 Run；任务双方应在接受契约时明确 criterion 与验收要求。

单次文本审核最多接收 32 个制品引用，超过上限在派发前保持待验证，不消耗模型预算。

独立 Run 使用现有全项目预算预留与实际用量结算，每次上限 16,000 token / 1,000,000 micro-USD / 1 个模型回合；工具与技能白名单为空。预算不足保持 PENDING，不绕过策略派发。Run 绑定、预算预留与验证记录同事务保存，终态、用量结算与项目 Run 事实同事务投影。重复通知不重复收费或通过任务。

模型输出必须是唯一 JSON 对象，恰好包含：

```json
{
  "schema": "coifesp.verification-result.v1",
  "passed": true,
  "findings": [],
  "required_changes": [],
  "evidence_refs": ["resource-id-from-submission"]
}
```

引用只能来自固定提交；有制品时 PASS 必须引用证据且不得包含 required_changes，FAIL 必须包含 findings 与 required_changes。拒绝重复键、额外字段、伪布尔、超限或非 JSON 输出。非法回复或运行故障记为 UNAVAILABLE，任务仍 PENDING；业务 FAIL 则进入 changes_requested。模型不能直接决定项目完成。

控制面与独立 Agent Worker 均已接入回写；Worker 每轮前后按当前团队分批恢复，包括审核落库后、任务状态更新前的崩溃窗口。Worker 与控制面需配置同一数据库、制品存储和工具 profile，并为任务发起团队运行对应 Worker。任务/契约变化使旧验证失效，不允许旧审核批准新提交。Control-plane 启动仍提供补偿重放。

环境或结果格式问题解决后，任务双方可以调用：

```text
POST /v1/projects/{project_id}/tasks/{task_id}:retry-agent-review
```

该接口不接收人工 PASS/FAIL。仅 UNAVAILABLE 结果可新增尝试，QUEUED 不重复派发，业务 FAIL 不被覆盖；每个提交的每个 criterion 最多 3 次独立审核尝试，旧记录保留。PENDING 预算/输入问题修复后可重新调用 verify。当前预算不足尚未自动打开 Human Gate，审核失败后的自动 Replan 也属于后续编排接线。

提交失效或制品撤回时，未执行的审核 Run 会在同一事务取消、置 STALE 并归还预算；已持有执行租约的 Run 不会被提前清零，待真实终态或租约恢复后处理，避免丢失实际用量。即使先收到审核终态、后检测父验证失效，也会核对当前契约和资源，不把旧审核记为当前 PASS。

## Human Review 与 Composite

例如把三个必需检查合并进已接受的任务契约：

```json
{"criteria": [
  {"criterion_id": "sha", "type": "tool_check", "tool": "artifact.sha256", "required": true},
  {"criterion_id": "semantic", "type": "agent_review", "required": true},
  {"criterion_id": "business", "type": "human_review", "required": true}
]}
```

Composite 采用现有 criteria 数组的所有必需项 AND 聚合，不另造嵌套策略或多数票。工具和 Agent 必需检查全部 PASS 后，才创建持久人工审核项。人工 ACCEPT 仅通过对应人审项；其它必需项 FAIL/PENDING 仍不能使任务 verified。多个必需人审 criterion 必须逐项接受；并不隐含必须由不同用户签署。可选人审当前不创建请求，明确显示 `optional_human_review_not_requested`。

审核项固定绑定提交 Run、契约版本、制品摘要及 criterion。只有任务发起团队内仍启用且注册状态 active 的用户可 ACCEPT/REJECT；执行团队可查看反馈，但不能自验收，服务身份禁止调用决定接口。审核理由会对任务双方可见，请勿填写仅团队内部可知的信息。

```text
GET  /v1/projects/{project_id}/tasks/{task_id}/human-reviews
POST /v1/projects/{project_id}/tasks/{task_id}/human-reviews/{review_id}:decide
```

GET 返回审核项、固定制品引用/摘要、自动检查证据及当前版本。POST 恰好包含：

```json
{"decision": "ACCEPT", "reason": "交付物符合已约定的验收要求", "idempotency_key": "accept-business-1", "expected_version": 1}
```

decision 仅允许 ACCEPT/REJECT；reason 必填且最多 2,000 字符；idempotency_key 以字母或数字开头，允许字母数字、点、下划线、冒号、连字符，最多 128 字符；expected_version 为正整数。拒绝传入 actor、team、passed、status 等额外字段。精确重试返回原结果；同键不同内容或第二次决定冲突。

决定、验证聚合、任务状态和相应事件/outbox 同事务落库。人审拒绝使任务 changes_requested，并将其它尚未决定的人审项置 STALE；契约变更、任务撤回或共享资源失效后，旧请求不能批准新提交，恢复验证时会关闭旧请求。重启不会丢失或重建重复请求，等待期间不占用 Agent Run。

本轮任务级人审提供 Verification 证据，不改变全局 ProjectProcess 三元组；预算和最终交付仍使用原来的 ProjectGate。新增 opened/decided/closed 项目事实通过既有事务监听器唤醒编排器，事件只携带绑定 ID、状态、版本和摘要，不传播人工理由或原始正文。详见 ADR-0012。

## 项目级验证证据与受控迁移

`load_project_verification_evidence` 在调用方事务内读取当前 WorkGraph 的任务、最新执行尝试、固定提交和验证记录。现有图任务没有 optional 标记，全部按必需工作检查；空图不会自动通过。仅修改任务为 verified 不能代替证据。历史 PASS/FAIL 不被改写，但契约、提交、制品共享状态变化后的旧 PASS 不再授权当前项目进入集成。

`VerificationOrchestrationEffect` 与持久 Runner 配合，在执行迁移的事务内再次核对 graph digest、process version/event sequence、decision 和工作租约。当前 PASS 才通过 Guard 进入 INTEGRATION；当前 FAIL 才返回 EXECUTION/READY。它不重写任务、已接受契约或历史证据，不增加 Planner replan/generated-task 计数；实际重跑仍须正常预留执行预算与团队容量。

生产后台消费者现已接线：持久快照读取当前契约、团队能力/共享容量、预算、活跃任务与审核 Run、Gate/Input 和验证证据；多个任务共享同一 provider/capability/version 容量池。当前验证 FAIL 可按原已接受契约重派新 execution attempt，仍受预算、容量与租约约束。返工上下文包含已验证的 Agent Review 修改意见或对任务双方公开的 Human Review 拒绝理由，不读取原 Run 的私有总结；已撤回共享的证据引用不会透传。项目级 verification.completed 事实明确携带 PASS/FAIL outcome；通过验证不等于项目完成。IntegrationRun 的实际制品组装见下文，DeliveryManifest 的接受/拒绝及最终 CompletionEvaluator 尚待接入。

独立 Agent Worker 的终态链路现已组合任务预算/容量结算、结构化任务结果投影和验证；各步骤可幂等恢复，前一个投影异常不会跳过其它回调。Worker 每轮按认证团队有界扫描终态但未完成这些步骤的任务 Run，并在同一领域事务中写入编排 wakeup。审核 Run 仍由审核拥有团队的恢复队列处理；不读取其它任务的私有对话。未配置制品存储时仍会结算真实用量，但不会把不可读取的文件误判为合格提交。

## 任务 Agent 生成真实文件

配置现有 `COIFESP_ARTIFACT_STORE_ROOT` 后，控制面、Agent Worker 和 Tool Worker 使用同一份 `project.publish_artifact` 工具声明。项目编排器给默认任务 Agent 配置版本固定的工具授权；未配置存储时不声明或授予该工具。显式注入的 Team Agent runtime policy 仍由调用方决定，不会被默认设置覆盖。普通聊天和人工上传不会因此获得机器任务发布权限。

模型通过持久 Tool Job 提交以下结构，而不是生成虚假的文件链接：

```json
{
  "title": "实现说明",
  "media_type": "text/plain",
  "content": "这里是真实文件内容。",
  "encoding": "utf8",
  "propagation": "project_readonly"
}
```

`encoding` 支持 utf8/base64；每次 content 最多 150,000 字符（预留 JSON 转义开销），解码后最多 512 KiB。该入口适合小型文档、源码片段和二进制结果；大文件上传仍使用现有资源上传入口，不宣称已经支持任意大小的 Agent 文件导出。media_type 和数量必须符合双方已接受的任务输出契约。

工具校验当前 task_execution Run、执行尝试、任务契约、团队 Agent 委派、真实待执行调用、固定授权及工具租约。文件按摘要不可变保存；资源记录、制品清单、发布幂等记录和共享发布事实在同一数据库事务中提交。数据库事务失败后可能保留未被引用的不可变字节，但不会出现已发布资源指向未提交清单；重试复用相同对象。发布已提交而 Tool Job 回执丢失时，新 Worker 复用原资源，不重新发布。暂时存储或数据库可用性错误进入有界重试。

迁移 `20260831_59` 为项目资源增加机器来源：`created_by=null`、`produced_by_principal_id=team-agent:<team>`、`source_run_id`、`process_id`。人工上传仍保留真实 `created_by` 外键；机器不得伪造人工账号。内部 Integration 发布使用互斥的 `source_integration_id`，来源为 `service:project-integrator`。含机器来源的数据库不能无损退回旧格式，因此迁移 downgrade 会在修改表前拒绝。

`team_private` 产出只在本团队可见，不广播项目发布事实。任务使用私有输入、输入共享已撤回或存在非任务上下文时，不允许自动改为 `project_readonly`。需要由所属团队通过现有资源共享操作审阅后披露；此工具不自动创建披露审批，也不会把未共享草稿判为跨团队合格交付。

只有文件发布/办公工具时，Tool Worker 不要求 Docker 沙箱；一旦配置任意沙箱参数，仍须提供完整合法配置。代码执行和沙箱验证的隔离行为不变，也没有新增认证系统或配置凭据。

`tests/test_task_artifact_publication.py` 使用真实持久 Agent Worker、Tool Worker、数据库、对象存储、任务投影和验证器，模型响应由测试 Provider 提供；覆盖新文件产生后验证、回执丢失恢复、事务回滚、私有输入、过期调用、存储重试与二进制/空文件。这不等价于真实 LLM、浏览器或 PostgreSQL 部署验收，也不代表 Integration/Delivery/Completion 已完成。

`ProjectOrchestratorLoop` 将持久 Runner 放入后台线程消费，不阻塞异步请求循环；空闲/故障有轮询间隔，关闭时等待当前数据库操作结束再释放底层资源。它本身不调用模型，也不绕过 Runner 的租约校验。生产 bootstrap 已用应用现有数据库、队列、AgentRunService 和能力目录构造并挂载 `app.state.project_orchestrator_worker`，在数据库就绪和启动恢复之后启动，并在释放数据库之前停止；不会自行创建数据库表或迁移真实库。团队 Agent Run 仍由对应团队的独立 Agent Worker 执行。

## 已验证制品的实际集成

配置同一制品存储后，生产编排器在 `INTEGRATION/READY` 选择持久的
`assemble_integration` 决策；未配置存储则保持等待，不制造 PASS。当前唯一可执行
集成模式是 `artifact_composition`，最多 256 个输入制品、总输入字节不超过 64 MiB；
策略可收紧但不能放宽这些上限。代码构建、仓库合并、语义验收并未因此自动实现。

`IntegrationService` 在同一带租约的事务中固定 WorkGraph、过程版本/事件序号、
最新任务 Run、PASS 验证记录及输入摘要，再重读实际字节。成功后发布确定性 ZIP，
附带版本/摘要清单，并保存 IntegrationRun PASS、结果资源及
`project.integration.completed`。内部发布器校验真实 ZIP 与清单完全一致，拒绝额外文件、
压缩归档、错误摘要、失效共享和跨项目输入。结果由项目拥有团队持有，使用机器来源，
不冒用人工账号；它不是可供普通对话直接调用的发布入口。

集成结果、资源/制品登记、领域事件和状态迁移同事务提交；租约失效、存储暂时不可用或
事件写入失败都会回滚，不误记为业务 FAIL。对象存储可能留下尚未引用的不可变字节，
幂等重试复用对象和资源身份。提交后队列确认丢失，不会重复发布或增加过程版本。

实际完整性失败、空制品集合、组装格式失败或超出组装上限，会记录具体失败代码和
受影响任务，Guard 经 `integration.failed` 返回 `EXECUTION/READY`。相关任务变为
`changes_requested`，历史任务 PASS 与已接受契约保持不变。再次派发前独立校验该 FAIL
仍绑定当前任务执行尝试、契约和提交证据；新 Run 创建后旧失败不再授权另一轮重复返工。
返工上下文只携带固定失败代码及具体建议，不复制其它团队的原始检查正文或私有总结。

测试覆盖真实持久编排、ZIP 字节、幂等恢复、事件回滚、以及集成失败后第二次任务执行、
重新验证并得到新 IntegrationRun PASS 的往返。测试 Provider 代替真实模型；尚未进行
本轮 PostgreSQL、浏览器或真实 LLM 部署验收。集成 PASS 仅进入 `DELIVERY/READY`，
不等于 DeliveryManifest 已由用户接受，更不等于整个项目已完成。
