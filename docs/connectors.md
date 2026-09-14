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
- 共享 Tool Worker 只能加载其允许租户集合内的连接器；每次执行都按 Tool Job 的权威租户重新解析该租户的 active revision，禁止跨租户复用凭据。

应用层固定 origin 不能替代生产网络控制。生产环境仍必须使用 DNS/egress proxy/防火墙 allowlist，防止
DNS rebinding、供应商域名被劫持或意外访问内网。共享 Worker 池应按受控租户集合分片，并通过 Secret 管理系统按 connector revision 注入独立凭据；高敏感或合规要求特殊的团队仍可部署专用池。

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

## GitHub 适配器（Phase 11 首个切片）

GitHub 仍通过固定 origin 的受审阅适配器接入，不允许模型构造 GitHub API URL、Header、Token 或
OAuth scope。一个连接器只有同时声明以下三个固定路径时，Agent/Tool Worker 才公开 GitHub 工具：

```text
/v1/github/issues
/v1/github/workflow-dispatches
/v1/github/commit-checks
```

对应工具为：

- `github.create_issue`：创建 Issue；高风险、必须审批，审批视图展示仓库和标题，正文仅展示摘要与长度。
- `github.dispatch_workflow`：在明确 ref 上触发 Actions workflow；高风险、必须审批。
- `github.get_commit_checks`：按 40 位 commit SHA 查询有界检查结果；低风险只读操作。

三种调用都作为 durable Tool Job 执行，并沿用 Tool Job 的稳定 provider idempotency key。Tool Worker
启动配置中的 connector ID 只是部署允许范围；真正执行时会从数据库重新解析该租户最新的 `active`
revision。尚未审批、已拒绝或已禁用的 revision 不可执行，Secret 仍只从 active revision 引用的环境变量
读取。

当前代码提供 COIFESP 到 GitHub adapter 的稳定合同及原生 GitHub 适配服务。除 MockTransport/fixture
回归外，项目测试仓库已经完成 commit checks、审批后创建 Issue、审批后触发 workflow、持久回执和
重放不重复副作用的真实闭环验收。正式部署仍须提供自己的允许仓库、最小权限令牌及网络出口配置，
详见 [GitHub 适配器配置](github-adapter.md)。

### GitHub adapter 响应与持久回执

Adapter 必须返回下列结构。仅返回 HTTP 成功或 `accepted: true` 不足以构成执行凭证。

```json
{"repository":"owner/repo","issue_number":42}
```

```json
{"repository":"owner/repo","dispatch_id":"dispatch-1","workflow":"verify.yml","ref":"main","accepted":true}
```

```json
{"repository":"owner/repo","commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","complete":true,"checks":[{"id":1,"name":"pytest","status":"completed","conclusion":"success"}]}
```

`complete` 表示该次检查查询已包含所有分页结果；最多接收 100 项检查。Adapter 应在分页尚未完成或
结果超出范围时返回 `complete: false` 及有界结果。检查 ID 不可重复；名称重复时验证保持 PENDING。

GitHub 工具将验证后的观察投影为 `coifesp.github-receipt.v1`，包含租户、Run、Job、call、连接器、
仓库、参数摘要、幂等键摘要和回执摘要。回执随 Tool Job 成功状态在同一数据库事务中加密保存，
无需增加另一张回执表；原始 Issue 正文、workflow inputs 和任意 adapter 附加字段不会进入回执。

内部服务可使用 `GitHubReceiptReader.read(tenant_id=..., run_id=..., job_id=...)` 读取已提交回执。
重启后的读取只访问持久 Tool Job，不重新创建 Issue 或触发 Actions。远端调用已成功但本地事务尚未
提交的窗口仍依赖 adapter 对稳定幂等键的持久去重，不能仅凭发送该 Header 宣称远端恰好一次执行。

`GitHubReceiptReader.evaluate_checks` 需要显式给出期望仓库、commit SHA 和必需检查名称。仅完整且
全部必需检查成功时返回 PASS；缺项、未完成、重复名称、neutral/skipped 均保持 PENDING；明确的
失败、取消或超时返回 FAIL。

### 接入任务验收

已接受的任务契约可使用以下 verification_policy；仓库、完整 SHA 和必需检查名称在契约中固定，
不能用 Agent 的成功描述或另一个提交的结果代替：

```json
{"criteria":[{"criterion_id":"ci","type":"tool_check","required":true,"tool":"github.get_commit_checks","github":{"connector_id":"github-main","repository":"owner/repo","commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","required_checks":["pytest"]}}]}
```

TaskVerification 为此创建绑定提交 Run、验收主体和 criterion 的持久只读 Tool Job。Worker 完成后，
验收器读取加密回执并参与既有验收聚合；只有其他必需条件也满足且提交仍有效，任务才能通过。
旧提交、失效快照不会因 GitHub 检查通过而被误批准。不完整观察保持 PENDING；传输失败或非法回执
不冒充业务 FAIL。授权的显式 retry_tools 可重新查询这些未决结果，最多 10 次尝试，保留旧 Job 关联，
不启动无界轮询。只配置 GitHub、不配置 sandbox 时也接通 Worker 的验收恢复链路。

有效观察生成 `coifesp.github-verification-evidence.v1` JSON 摘要，包含验收主体摘要、criterion、
状态、回执摘要和尝试次数，不包含原始供应商响应或 workflow inputs。它与验收更新在同一数据库事务
中登记为目标团队的私有项目资料；重放不会重复发布。成员可在项目概览的资料卡片点击下载，后端继续
执行资料访问校验。共享验收视图不附加该私有资料 ID。

上述任务验收链路已通过本地持久数据库、Tool Worker、模拟供应商响应和专用 GitHub 测试仓库验收。
三个固定路径的原生映射由 `github_native.py` 实现，独立服务 `github_adapter.py` 提供协议入口及持久
写入去重。写入结果未知时拒绝自动重发；本地账本与适配器重放已验证不会重复当前操作，但这不构成
对 GitHub 远端所有故障窗口的绝对 exactly-once 承诺。
