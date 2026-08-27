# 可观测性告警运行手册

本手册对应 `deploy/observability/prometheus-rules.yaml` 中的稳定 `runbook` 标识。告警和仪表盘仅使用
低基数运维指标，不含租户、用户、Run、Prompt 或制品标识。处置人员不得把原始请求、凭据或团队私有内容
复制到工单和即时通信中。

所有处置都遵循同一顺序：确认告警数据完整性、冻结高风险变更、定位受影响组件、执行有界缓解、验证恢复、
记录不含业务内容的时间线。不得为了恢复指标而绕过审批、RLS、审计或信息隔离。

<a id="http-availability"></a>
## HTTP 可用性错误预算

1. 确认 Fast Burn 的 5 分钟和 1 小时窗口、或 Slow Burn 的 30 分钟和 6 小时窗口均超过阈值；单窗口尖峰
   不构成该告警。
2. 检查控制面副本健康、数据库连接、OIDC/JWKS 可达性、请求量与 5xx 路由分布。不要查看实际 URL 参数。
3. 若刚完成灰度发布，停止继续放量；符合发布治理回滚条件时回滚到已验证摘要，禁止重新使用可漂移标签。
4. 若是依赖故障，启用已审核的熔断/降级策略；不得默认放行授权或把持久任务改成不持久执行。
5. 恢复后至少观察一个长窗口，确认错误率低于目标并核对审计链完整，再结束事件。

<a id="http-latency"></a>
## HTTP 延迟 SLO

1. 确认一分钟级请求量门槛已满足，排除低流量时的无意义比值。
2. 按路由模板检查 p99，不使用具体路径；对比数据库池、外部身份服务和 Worker 队列延迟。
3. 检查 CPU/内存限制、并发上限和重试放大。不得通过移除预算、超时或租约 fencing 来降低表面延迟。
4. 灰度版本相关时停止放量并依据发布门禁回滚；容量相关时只在租约安全约束内扩容。
5. 验证 99% 请求重新落在一秒内，并确认错误率没有因快速失败而升高。

<a id="agent-worker"></a>
## Agent Worker 成功率

1. 比较 `failed`、`lease_lost` 和 `retry_scheduled`，确认失败不是正常的 `awaiting_approval`、
   `awaiting_tool` 或续跑事件。
2. 检查数据库租约、心跳、身份重验证、Provider 熔断和成本/token 预算；只使用规范化错误码。
3. 若 lease lost 增长，停止盲目扩容并检查时钟、数据库延迟和终止宽限期；绝不手工复用旧 fencing token。
4. 若依赖故障，保留 durable checkpoint 并让有限重试预算接管；不得直接重放可能已计费的模型请求。
5. 通过一个合成任务和积压趋势确认恢复，随后检查失败预算及重复副作用计数。

<a id="model-provider"></a>
## 模型 Provider 失败率

1. 确认请求速率门槛满足，并按已注册的低基数 Provider ID 和规范化失败类型定位。
2. 检查熔断器、并发等待、网络出口、Provider 配额和路由策略；不要记录远端错误正文。
3. 只有策略、驻留、分级、token 和成本边界均允许时才启用已配置的故障转移候选。
4. 不得为恢复而把内部/受限数据发送到未批准 Provider，也不得无限重试可能已成功计费的请求。
5. 用公开、无敏感内容的合成请求验证，确认失败比率和成本均回落。

<a id="model-cost"></a>
## 模型成本异常

1. 确认一小时增量是实际计量计数，不是 Prometheus 重置或重复采集造成的跳变。
2. 按 Provider ID 检查 token 增量、失败重试、循环 turn 数和并发量；不按租户或用户拆分指标。
3. 暂停非必要批量任务并维持已批准的硬成本预算；不能用删除审计或使用未注册低价 Provider 代替治理。
4. 如果版本变更导致放大，停止灰度并按不可变制品摘要回滚。
5. 恢复后将经批准的预算调整纳入配置评审，不在告警现场临时扩大预算。

<a id="telemetry-export"></a>
## 遥测导出与队列容量

1. 检查 Collector 健康、发送失败计数和持久队列占用；确认 Prometheus 自身仍能抓取 Collector 指标。
2. 检查上游证书链、固定的集群内 egress gateway/遥测后端服务、DNS 和最小出站 NetworkPolicy。
3. 不得禁用 TLS 校验、开放全网出站或把上游凭据写进 ConfigMap/日志。
4. 上游短暂中断时依赖有界重试和 PVC 持久队列；接近容量时优先恢复上游，不通过删除队列掩盖丢数。
5. 恢复后确认队列稳定回落、失败计数停止增长，并抽查 Trace 后端只包含最小披露属性。

## 部署与轮换约定

部署者必须预先创建两个 Secret，仓库不提供示例值：

- `coifesp-otel-receiver-tls`：包含 `tls.crt` 和 `tls.key`，证书 DNS SAN 覆盖 Collector Service；
- `coifesp-otel-upstream`：包含 `endpoint`、`authorization` 和 `ca.crt`。`endpoint` 应指向位于带
  `coifesp.dev/telemetry-upstream=true` namespace 标签中的集群内后端或 egress gateway。

应用 namespace 必须标记 `coifesp.dev/telemetry-client=true`，监控 namespace 必须标记
`coifesp.dev/monitoring=true`。生产控制面的 Trace 地址使用 Collector 的 HTTPS OTLP/HTTP Service 地址，
并信任接收证书 CA。证书和上游凭据轮换应先增加新信任，再滚动 Collector，最后撤销旧材料。

部署依赖 Prometheus Operator 的 `PrometheusRule` 与 `ServiceMonitor` CRD，以及支持 StatefulSet PVC 的
StorageClass。部署前必须运行 `scripts/check_observability_assets.py`；集群端还需执行策略、证书、抓取和
告警路由验收。
