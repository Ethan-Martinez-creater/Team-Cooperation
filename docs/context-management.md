# 上下文装配与压缩

## 安全边界

`ContextAssembler` 是所有补充上下文进入模型前的统一边界。调用方必须为每个 `ContextItem` 提供：

- 稳定的 `item_id`、来源类型、来源 ID 和内容 SHA-256；
- 所有租户、安全分级、隔离域和资源 ID；
- 内容可信度与指令可信度，两者互不替代；
- 跨租户内容所需的精确 `DisclosureGrant` 和使用目的。

同租户项目仍需通过 clearance 与 compartment 检查。跨租户项目必须有未过期、资源 ID、接收租户、
purpose、分级和隔离域均精确匹配的披露授权。拒绝的项目不会发送给模型，但其项目 ID 和安全拒绝原因会
进入装配结果与审计。

Memory、工具、文档、A2A、治理事件和 Skills 即使内容已经验证，也默认是 `data_only`。装配器将它们
放入独立 JSON 数据信封，不允许它们成为 `system` 消息。补充上下文宣称的系统指令一律拒绝；系统
指令只能来自部署时静态配置。工具输出同样使用 `coifesp.tool-output.v1` JSON 信封，防止内容伪造
文本分隔符后提升权限。模型仍可能错误理解不可信文字，因此所有真实副作用继续由 ToolExecutor 的
确定性策略和审批边界控制。

## 预算与压缩

预算使用 `max_input_tokens - reserved_output_tokens` 的硬上限。默认计数器按 UTF-8 字节给出保守、
provider-neutral 的估算；生产部署可注入与目标模型一致的 tokenizer。预算包含消息内容、tool call
参数以及发送给 provider 的工具描述和 JSON Schema，不只计算自然语言正文。单项大小和总项目数都有
独立上限。选择顺序由显式 priority、创建时间和 item ID 确定，结果可重复。

超过单项或总预算的内容不会被无标记截断。`StructuredCompactor` 生成
`coifesp.context.compaction.v1`：

- 每个保留片段都带原 item/source ID 和完整内容 SHA-256；
- 明确标记 excerpt 是否完整；
- 明确声明被省略内容必须重新获取后才能作为依据；
- 无法安全容纳的项目进入 `excluded`，不会静默消失。

如果对话历史本身已经超过安全预算，系统会 fail closed，要求先生成并审核 checkpoint。当前实现不让
同一次模型调用在超预算后“自我总结”，以免未经验证的摘要替换原始约束。后续语义 checkpoint 服务
必须保存原始来源集合、摘要声明到来源的逐项引用、模型/提示版本和人工或策略验收结果。

## Agent Loop 集成

`AgentRunRequest` 可携带 `context_items`、`context_budget` 和 `context_purpose`。Agent Loop 每轮调用
模型前重新装配，以便新产生的工具消息计入预算；补充上下文消息仅发送给 provider，不写回对话历史，
避免重复累积。每轮产生 `context.assembled` 运行事件和 `context.assemble` 审计事件。
