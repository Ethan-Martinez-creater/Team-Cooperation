# 办公连接器安全网关

## 信任边界

模型和 Agent 不能提交 URL、Host、OAuth scope、Authorization Header、Client ID 或 Secret。它们只能
选择当前租户目录中已经注册的 `connector_id`，以及工具定义中固定的操作。实际 HTTPS origin、OAuth
token endpoint、最小 scope、相对路径 allowlist、数据最高分级、超时/重试/响应限额由 Tool Worker 的
部署配置决定。

连接器客户端统一强制：

- HTTPS 且 URL 不含用户名、密码、query 或 fragment；
- base URL 不含路径，操作使用精确相对路径 allowlist；
- 拒绝 localhost 和 IP literal，`follow_redirects=false`、`trust_env=false`；
- OAuth client-credentials 使用连接器专属密钥，Secret 只通过独立环境变量引用；
- 供应商请求携带 durable Tool Job 的稳定 `Idempotency-Key`；
- 仅对明确的瞬态网络/HTTP 状态有限重试，重试保持相同幂等键；
- 有界响应、有限熔断、规范化错误码，不把供应商响应正文写入日志；
- 目录只能包含 Tool Worker 固定租户的连接器，禁止单进程持有多个团队的办公密钥。

应用层固定 origin 不能替代生产网络控制。生产环境仍必须使用 DNS/egress proxy/防火墙 allowlist，防止
DNS rebinding、供应商域名被劫持或意外访问内网。建议按团队部署独立 Tool Worker 和独立 Secret 注入。

## `office.send_message`

消息发送是高风险工具，必须经过持久审批。审批页面显示连接器 ID 与目标，正文只显示 SHA-256 和字符
数，不显示原文。正文作为加密 Tool Job 参数保存，发送结果同样加密。跨团队内容不能直接依靠办公工具
披露；必须先经过协作治理/披露授权生成明确共享的交付物，再由接收或发送团队自己的连接器执行。

## 配置结构

`COIFESP_CONNECTORS_JSON` 是严格 JSON 数组；未知字段会导致启动失败。Secret 不得写入 JSON：

```dotenv
COIFESP_CONNECTOR_TEAMS_CLIENT_SECRET=由部署Secret注入
COIFESP_OFFICE_DATA_CLASSIFICATION=internal
COIFESP_CONNECTORS_JSON=[{"connector_id":"teams-main","tenant_id":"team-a","base_url":"https://供应商API域名","token_endpoint":"https://供应商身份域名/oauth/token","client_id":"最小权限应用ID","client_secret_env":"COIFESP_CONNECTOR_TEAMS_CLIENT_SECRET","scopes":["供应商最小发送scope"],"allowed_paths":["/v1/messages"],"max_classification":"internal","timeout_seconds":15,"max_response_bytes":1048576,"max_attempts":3,"circuit_failure_threshold":5,"circuit_cooldown_seconds":30}]
```

`/v1/messages` 是 COIFESP 通用连接器适配器契约，不等同于 Teams/Slack 的原生路径。真实供应商接入应
实现受审阅的适配器服务，将固定契约映射到供应商 API；不能把任意供应商 URL 暴露给 Agent。
