# 可观测性与秘密安全

COIFESP 使用三条互补信号：OpenTelemetry Trace、Prometheus Metrics 和结构化 JSON Log。可观测性
不能成为绕过团队信息隔离的旁路，因此所有信号遵循“低基数、最小披露、无业务内容”原则。

## Trace

控制面为每个 HTTP 请求创建 server span，并接受标准 W3C Trace Context。Span 名称和属性只使用
FastAPI 路由模板，例如 `GET /v1/agent-runs/{run_id}`，不会记录实际路径、查询参数、Authorization、
租户、用户、提示词或工具参数。状态只记录 HTTP 状态码和异常类型。

Trace 通过 OTLP/HTTP 批量导出。生产环境要求 HTTPS，并采用 parent-based 比例采样；默认生产采样率
为 `0.1`，开发/测试为 `1.0`。采样率不会改变审计日志的完整性，安全审计始终走独立持久化链。

## Metrics

Prometheus 使用进程内独立 Registry，当前提供：

- HTTP 请求总数、耗时直方图和当前并发数；
- Durable Agent Worker 按规范化 outcome/error code 计数。
- LLM Provider 尝试次数与耗时，以及成功调用的输入/输出 token 和保守估算成本。

标签只包含请求方法、路由模板、状态类别和固定错误代码，不包含 tenant、principal、run ID、URL 或
文档名称。生产环境启用指标时必须配置至少 32 字节的 Bearer Token；重复 Authorization 头会失败
关闭。指标端点应另外通过 Kubernetes NetworkPolicy 或反向代理限制为监控网络可达。
Provider 指标只接受注册表约束的低基数 `provider_id`、固定 outcome 和规范化失败类型；模型名、租户、
Run ID、提示词和远端错误正文均不作为标签。指标导出失败不会触发模型重试，避免已经计费的成功请求被
重复发送。

上线资产位于 `deploy/observability/`：版本化 SLO、Prometheus recording/alert rules、Grafana 仪表盘、
TLS OTLP Collector 配置和 Kubernetes StatefulSet。Collector 使用固定版本及镜像摘要、双副本、
PVC-backed 持久发送队列、有界重试、内部 Prometheus 指标和严格 NetworkPolicy；运行手册见
`docs/observability-runbooks.md`。这些资产必须通过 `scripts/check_observability_assets.py` 的 PromQL、
低基数、TLS、镜像、Secret 引用和部署不变量校验。

## Structured Logs

JSON 日志包含时间、严重级别、服务名、logger、脱敏消息、request ID，以及存在时的 trace/span ID。
异常只记录类型，不输出异常正文或 traceback。日志 Formatter 再次通过 SecretRedactor，且拒绝把任意
LogRecord extra 自动展开，避免第三方库把请求对象或凭据写入日志。

## 配置

```dotenv
COIFESP_SERVICE_NAME=coifesp-harness
COIFESP_TELEMETRY_ENABLED=true
COIFESP_OTLP_TRACES_ENDPOINT=https://otel-collector.example/v1/traces
COIFESP_TRACE_SAMPLE_RATIO=0.1

COIFESP_METRICS_ENABLED=true
COIFESP_METRICS_PATH=/internal/metrics
COIFESP_METRICS_BEARER_TOKEN=至少32字节且独立生成的监控密钥

COIFESP_STRUCTURED_LOGGING=true
COIFESP_LOG_LEVEL=INFO
```

OTLP Collector 地址和 Metrics Token 属于部署环境配置，不应提交到仓库。当前本机没有 Collector，
因此 `.env` 不必立即启用 `COIFESP_TELEMETRY_ENABLED`；关闭时本地 span provider 保持无网络导出，
不会影响控制面运行。
