# 多 Provider LLM Gateway

LLM Gateway 是 Agent Loop 与外部/内部模型之间唯一受支持的生产调用边界。它不允许 LLM 自己选择
Provider，也不根据提示词内容动态放宽安全策略；路由由受信配置和 durable `ModelRoutePolicy` 决定。

## Provider 注册表

每个 Provider 必须声明稳定 `provider_id`、适配器类型、模型、能力、可处理的最高数据分级、地域策略
标签、是否属于外部 egress、上下文/输出上限、计费单价、优先级和并发上限。API Key 只通过
`api_key_env` 引用部署 Secret。注册表严格拒绝未知字段、重复 ID、不安全生产 URL、缺失密钥和未经
支持的能力，避免拼写错误静默降低安全边界。

当前内置适配器：

- OpenAI Chat Completions 及 OpenAI-compatible API（包括 DeepSeek）；
- Anthropic Messages API。

Provider SDK 只在构建 Gateway 时实例化，不发起探测请求。所有远端异常都会转换为规范化失败类型，
错误正文、响应体和凭据不会进入 Agent 事件、指标或审计详情。

## 路由顺序与失败关闭

路由依次检查所需能力、工具调用能力、数据最高分级、外部 egress 授权、地域、上下文窗口、硬 token
上限、成本上限和熔断状态，然后按 `priority`、最坏情况估算成本和 `provider_id` 稳定排序。没有安全
候选时抛出路由错误；只有成本不满足时返回独立的成本预算错误；所有候选熔断时返回容量错误。

认证、权限、余额、无效请求、内容策略和协议错误属于不可重试错误，不会换 Provider。限流、超时、
连接、容量和服务端错误可在配置的有限次数内故障转移。每个 Provider 有并发信号量和节点本地熔断器；
SDK 内部重试和 Gateway 故障转移均有显式上限。

## Token 与成本预算

Agent Run 持久化累计 `model_cost_microusd`，恢复后继续使用同一预算。Gateway 在请求发出前使用输入
token 的保守上界、该次允许的硬输出上限和 cache-miss 单价进行准入；Agent Loop 把剩余 Run token
预算下推为 Provider 请求的 `max_tokens`。响应返回后再次校验 Provider 报告的输入/输出 token、上下文
窗口和实际估算成本，Provider 超报或违反限制会作为协议错误失败关闭。

成本单位为微美元（USD 的百万分之一），避免浮点累积误差。当前适配器没有拆分 prompt cache hit/miss
的计费，因此配置必须填写 cache miss 或其他经财务批准的保守单价。

## 流式语义

流式适配器统一输出递增序号的 `text_delta` 和唯一 `completed` 事件。Gateway 仅允许在尚未向调用者
发出任何文本时切换到备用 Provider；一旦发出部分文本，后续故障直接终止流，禁止自动重放，以免用户
看到两个模型结果拼接或下游工具重复执行。最终事件携带完整响应、Provider、模型、usage 和成本。

原始 token delta 是短生命周期数据，不写入 durable Agent Run 生命周期事件；需要向 UI 转发时，必须
复用已鉴权的 Run WebSocket/SSE 边界并遵守租户可见性。只有最终检查点和规范化元数据进入持久层。

## 可观测性与运维

Prometheus 暴露 Provider 尝试、耗时、输入/输出 token 和成本，标签仅包含低基数 Provider ID、结果和
规范化失败类型。没有模型名、租户、用户、Run ID、提示词或远端错误正文。可观测性回调是 best-effort，
Exporter 故障不能把已成功、已计费的模型调用变成失败或触发重复请求。

多副本部署时节点本地熔断器只能保护单个 Worker；全局配额、分布式自适应并发和账户级限流属于上线
流量治理层，仍需结合队列、共享限流器和 Provider 账户隔离完成。

## DeepSeek 当前兼容说明

截至 2026-08-06，DeepSeek 官方 Chat Completions 文档支持 SSE、`stream_options.include_usage`、工具
调用、JSON Output 及规范化 finish reason；模型和价格页列出 `deepseek-v4-flash` 与
`deepseek-v4-pro`。部署前必须重新确认模型 ID、上下文、输出上限和价格：

- [Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)
- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
