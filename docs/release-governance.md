# 灰度发布、回滚与发布门禁

本切片是一个不访问集群、注册表、监控后端或外部 API 的离线决策器。它读取受版本控制的兼容矩阵、
发布计划和观察快照，严格校验后输出机器可读 JSON 报告。平台流水线只能消费报告，不能绕过其中的
`promotion_authorized`；实际流量切换和回滚执行仍由部署平台负责。

## 安全边界

- MCP/A2A 的直接实现、包导出、配置、路由和协议测试继续冻结。本矩阵明确将 `mcp`、`a2a` 列入
  `excluded_frozen_protocols`，且只允许六类内部接口：`db_schema`、`control_plane`、`worker`、
  `checkpoint`、`envelope`、`audit`。这不是 MCP/A2A 能力扩展或兼容声明。
- 所有文档采用严格 JSON：拒绝重复键、未知字段、缺字段、非有限数字和 prerelease 版本。
- 矩阵的 `default_policy` 必须是 `deny`。未明确列出的 release、策略或 transition 一律拒绝。
- 每个受支持 release 必须声明六类内部接口版本；每条 transition 必须逐项声明 `unchanged` 或
  `bidirectional`，并与两端接口版本一致。`rollback_safe` 必须由矩阵明确给出。
- 发布制品同时绑定 artifact、OCI image、SBOM 和 provenance 的 SHA-256 digest；image 只接受
  `name@sha256:...`，不接受可变 tag。每阶段观察也必须回报相同 artifact/image 身份。
- 请求人、构建服务和三个审批人相互分离；`artifact_attestor`、`release_manager`、
  `risk_approver` 三个角色缺一不可且每项批准都绑定准确的 artifact 与矩阵 digest。

## 灰度状态与决策

阶段流量必须严格递增、首阶段小于 100%、末阶段等于 100%。每一阶段都有最小观察窗和最小请求数。
阶段缺样本时为 `hold`；完整通过后只授权下一阶段（`promote`）；最终阶段通过才是 `complete`。

门禁同时检查：不可变身份、观察窗、样本数、availability SLO、相对 SLO 的 error-budget burn rate 和
p95 latency。完整样本触发任一 SLO/预算失败，或部署身份漂移时，若计划启用自动回滚且矩阵声明
`rollback_safe`，决策为 `rollback`；否则为 `blocked`。未明确支持的 transition、矩阵 digest 漂移、
阶段乱序和未知阶段均默认拒绝。

## 离线验收

示例矩阵摘要是：

```text
sha256:36d612e8378970de4e4fcdcab1e861ee43dc28b32c921049871563a2bdabc485
```

示例命令（Windows）：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_release_governance.py ^
  --matrix deploy\release\compatibility-matrix.json ^
  --plan deploy\release\release-plan.json ^
  --observations deploy\release\observations-pass.json ^
  --report .test-tmp\release-report.json
```

退出码：`0` 表示可推进或已完成，`2` 表示合法但必须 hold/blocked/rollback，`1` 表示文档或 I/O
校验失败。stdout 和 `--report` 均为稳定字段的 JSON；报告不含凭据、原始遥测或个人自由文本。

## 生产平台仍需集成

部署平台需要把报告验证接到 CI/CD required check，并负责签名/验证审批和 provenance、从可信遥测
系统产生只读观察快照、分阶段调整流量、执行 digest 固定的回滚目标、记录发布审计和限制 break-glass。
生产接入还必须验证矩阵摘要来自受保护分支，并用有时效且防重放的身份签名替代示例静态批准。
