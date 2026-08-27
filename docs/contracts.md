# 版本化协作契约与变更影响

该模块把团队间真正需要共享的“接口”作为独立安全对象管理，不要求生产团队公开实现、内部文档或完整工作区。

## 角色与边界

- 生产任务负责人可登记契约，只有生产方租户的 `lead` 能发布正式版本。
- 消费任务负责人或消费方 `lead` 声明依赖、确认影响及最终接受结果。
- 生产方 `lead` 可以提出补救方案，但不能替消费方关闭风险。
- 契约、版本、依赖和影响都使用显式 `visible_to_tenants`；PostgreSQL 强制 RLS 是数据库层的第二道隔离。

## 发布与影响规则

- 版本使用严格 SemVer；正式依赖不接受 prerelease。
- 版本必须单调递增，并通过 `predecessor_version` 做乐观并发控制。
- 内容使用 SHA-256 寻址；同一内容的新版本不会产生虚假影响。
- major 版本变化始终按 breaking 处理，生产方不能把它降级为 compatible。
- 每个消费依赖生成独立影响记录。`action_required` 或 `blocking` 影响未进入 `accepted` 时，关联任务不能验收为完成。
- 接受影响会原子推进依赖基线；旧影响不能覆盖更晚版本，防止基线回退。

## 可靠性

所有命令带租户级幂等键；状态、追加式事件、签名审计与跨团队 outbox 在同一数据库事务提交。事件序号用 PostgreSQL 事务级 advisory lock 串行化。事件表拒绝 UPDATE、DELETE 与 TRUNCATE。

数据库验收：`E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_contracts.py`。
