# 任务交付物验证

结构化 TeamTask 的 `submitted` 不是 `verified`。验证针对服务器保存的提交 Run、契约版本、制品 SHA/大小和固定 verification policy；任务执行 Agent 的总结不是验证证据。

## 检查类型

- `artifact.sha256`：从制品存储读取字节并核对大小与 SHA256。
- `artifact.json`：在完整性通过后验证 JSON；解析预算不足时保持 PENDING，不猜测通过。
- `sandbox.profile:<profile_id>`：将必需检查派发给持久 Tool Worker，执行管理员配置的固定程序。
- 尚未接入的工具和 `agent_review` 保持 PENDING。不能以空实现代替独立审核。

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

这条链路是任务级工具验证，不等于完整项目验收。独立 Agent/Human Review、自动返工调度、IntegrationRun、DeliveryManifest 和项目完成判定仍须分别接通。单元测试中的模拟 OCI 结果也不代表真实 Docker、PostgreSQL 或 LLM 环境验收。
