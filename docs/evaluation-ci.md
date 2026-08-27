# 签名评测与 CI 门禁

生产评测套件必须使用 Ed25519 签名，并由独立的信任文档列出允许的公钥。门禁先对 suite 的规范 JSON
计算 SHA-256，再验证 `suite_digest` 和签名；重复 JSON key、未知字段、未知 signer、内容篡改、畸形
base64url 和不稳定 schema 都会默认拒绝。私钥不属于仓库，也不能通过命令行或环境变量传给门禁。

签名 suite 必须固定 `evaluation_instant`。PDP 回归会把它注入生产 `PolicyEngine`，确保 grant 到期测试不依赖
CI 机器当前时间。`PolicyEngineEvaluationExecutor` 支持 `resource_access`、`tool_execution` 和
`disclosure` 三类真实 PDP 操作；输入采用精确字段 schema，不接受额外权限字段。

运行 required check：

```powershell
E:\miniconda3\envs\bettafish\python.exe -B scripts\check_evaluation_gate.py `
  --trust deploy\evaluation\trusted-keys.json `
  --suite deploy\evaluation\pdp-regression.signed.json `
  --report evaluation-report.json
```

退出码 `0` 表示所有阈值通过，`2` 表示评测有效但 gate 失败，`1` 表示输入、摘要或签名无效。
CI 平台必须把该命令配置成合并和发布所需的 required check，并把 trust document 作为受 CODEOWNERS/
保护分支约束的安全配置。仓库不附带伪造的“生产可信公钥”或签名套件；部署者应在受控签名流程中生成。

真实 Agent 评测通过 `AgentLoopEvaluationExecutor` 和 `AsyncDeterministicEvaluationRunner` 进入生产
`AgentLoop`。CI 应注入可复现、无公网和无费用的受控 ModelProvider/Tool；预发布环境再以明确预算运行真实
Provider suite，报告只保留断言结果、失败代码和内容摘要，不保存 prompt、模型正文或 canary 值。
