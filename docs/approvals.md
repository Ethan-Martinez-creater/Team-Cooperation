# 持久化审批与安全审阅投影

## 两类审批

`approval_requests.origin` 区分：

- `tool_managed`：只能由 ToolExecutor 根据已通过 JSON Schema 和自定义 validator 的参数生成，可用于
  高风险工具消费；
- `manual`：用于一般协作治理记录，不能作为 ToolExecutor 的执行授权。

控制面允许创建 manual 记录，但 ToolExecutor 在持久化模式下要求 `tool_managed`，因此客户端不能通过
伪造工具名、摘要或说明绕过工具拥有的审阅规则。进程内 `Approval` 只保留给未配置持久化服务的测试与
本地兼容路径，生产组装不得使用。

## 工具拥有的审阅投影

每个高风险工具必须在本地 `ToolDefinition.approval_review` 中声明 allowlist。MCP 远端工具的描述不能
声明这项权限；只有本地 `McpToolPolicy` 可以配置。字段使用有界 JSON Pointer，并选择一种披露方式：

- `value`：展示规范化值，并再次经过秘密模式脱敏；
- `hash`：只展示值摘要；
- `count`：只展示字符串、数组或对象的元素数；
- `redacted`：固定显示 `[REDACTED]`，同时保存值摘要。

投影采用 `coifesp.approval-review.v1`，包含本地工具名、字段指针、披露方式和值摘要。未列入 allowlist
的参数不会进入投影。找不到字段、字段重复、投影过大、schema/tool 名不匹配都会 fail closed。数据库
同时约束投影必须是对象、schema 和工具名必须匹配、字段必须是数组，并保存规范化投影 SHA-256。
完整工具请求摘要统一由 `digest_tool_request` 生成：UTF-8、Unicode 不转义、键排序、紧凑分隔符、
拒绝 NaN/Infinity，且规范化请求最大 1 MiB。调用方不得自行采用不同的 JSON 规范化算法。

## 生命周期与恢复

审批与租户、申请人、工具、完整请求摘要、安全分级和隔离域绑定。审批人必须：

- 不是申请人；
- 具有工具指定的审批角色；
- clearance 不低于输入分级；
- 拥有输入所需的全部 compartment。

决定使用乐观版本；批准可在消费前撤销。消费使用数据库行锁并绑定确定性 `execution_id`。同一执行的
崩溃恢复可以重复读取已消费结果，不同执行不能重放审批。

Agent Loop 遇到高风险工具时返回 `awaiting_approval`，保存 assistant tool call 和带 approval ID 的
tool checkpoint，不会让模型生成一个新调用绕开绑定。恢复请求必须提供原 call ID、approval ID 和
累计 `RunUsage`，运行时由 run ID + call ID 重建同一 execution ID，替换原 checkpoint 后继续模型
循环。轮次、工具次数和 token 使用量跨恢复累计，不能通过暂停重置预算。

## 信息可见性

申请人只能读取自己的审批；审批人只能列出角色、clearance 和 compartment 均匹配的未过期 pending
记录。其他同租户用户和其他租户均得到隐藏结果。审阅投影继承审批安全标签，不是公开信息。审计仅记录
请求、投影摘要、状态、参与者和 execution ID，不写入原始工具参数。
