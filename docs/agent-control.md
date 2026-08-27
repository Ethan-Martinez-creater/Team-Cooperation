# Agent Steering 与 Follow-up 控制流

该控制流允许任务所有者或显式的 Run Controller 在 Agent 执行期间调整方向，并在任务完成后追加工作。
它不是进程内 WebSocket 队列：每条指令先以密文写入 PostgreSQL，Worker 只在 Agent Loop 的安全轮次
边界读取，指令状态与新的加密 checkpoint 在同一事务提交。

## 状态和并发语义

指令类型为 `steer` 或 `follow_up`，生命周期为 `pending → applied` 或 `pending → rejected`。指令 ID 在
单个 Run 内幂等；相同 ID 和相同内容重试返回原记录，相同 ID 更换内容会失败。提交方必须携带
`expected_run_version`，避免基于过期 Run 状态操作。

- `steer` 用于尚未完成的 Run；已完成 Run 不接受伪装成 steering 的新任务。
- `follow_up` 可在执行期间作为下一轮用户指令，也可在 `completed` 后原子地把 Run 重新置为 `queued`。
- Worker 把控制正文写入带 `coifesp.agent-control.v1` 标记的用户消息。Repository 会逐字段核验消息、
  数据库密文和控制游标，不能只报告序列号来伪造“已应用”。
- 如果指令在模型给出最终答案后、完成 checkpoint 提交前到达，完成事务会改为 `queued`，Worker 返回
  `continuation_queued`，不会错误报告完成或丢失指令。
- Run 进入不可恢复的失败态时，剩余指令记录为 `rejected`，并保存规范化原因和时间。

`agent_run_commands` 强制启用 PostgreSQL RLS。指令身份、密文、提交者和创建时间不可修改；进入
`applied`/`rejected` 后，生命周期字段也不可修改；删除和 truncate 由数据库触发器拒绝。

## WebSocket 协议

端点为 `/v1/agent-runs/{run_id}/control`，必须满足：

- `Authorization: Bearer ...` 请求头；禁止把 Token 放在 URL 查询参数；
- 显式协商 `Sec-WebSocket-Protocol: coifesp.control.v1`；
- 通过 OIDC 验证后仍执行租户隔离以及 owner/controller 授权；
- 只接受 UTF-8 JSON 文本帧，拒绝二进制帧和超过 70,000 字节的协议帧；指令正文上限为
  65,536 UTF-8 字节。

连接成功后服务端发送 `control.ready`。客户端提交：

```json
{
  "type": "command.submit",
  "command_id": "client-generated-id",
  "command_type": "steer",
  "content": "Use the reviewed API contract.",
  "expected_run_version": 3
}
```

`command.accepted` 只返回 ID、序列、类型和状态，不回显正文。业务冲突返回带稳定错误码的
`control.error`，不会把数据库异常或秘密写入协议。

断线后客户端发送：

```json
{
  "type": "session.resume",
  "after_command_sequence": 0,
  "after_event_sequence": 12
}
```

服务端返回当前授权范围内的 `session.snapshot`，并持续推送 `run.events`。快照中的指令正文仅对已通过
控制授权的主体可见；生命周期事件、审计事件、确认包和日志均不含正文。单批最多返回 100 条指令和
500 条事件，`has_more_*` 指示客户端继续分页。服务端每 15 秒发送 `control.keepalive`；客户端也可发送
`{"type":"ping"}`。

## 加密和部署边界

控制正文使用 AES-256-GCM，AAD 绑定租户、Run、序列、指令 ID、类型和 key ID。当前从
`COIFESP_MEMORY_MASTER_KEY` 通过独立的 HKDF domain 派生 Agent Control 租户密钥，因此不会复用
Memory 或 checkpoint 的实际数据密钥；生产 KMS 应保留对应 key ID 的历史解密能力。

反向代理必须允许 WebSocket Upgrade，保持 Authorization 与子协议请求头，并把单帧上限配置为不高于
应用协议允许值。不要开启记录 WebSocket 正文的代理调试日志。真实 OIDC 环境尚未提供，因此当前已经
完成代码、SQLite 测试和 PostgreSQL 安全冒烟，但生产公网启用仍随真实 OIDC 联调保持暂停。

验证命令：

```bat
E:\miniconda3\envs\bettafish\python.exe -m pytest tests\test_agent_control.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_agent_runs.py
```
