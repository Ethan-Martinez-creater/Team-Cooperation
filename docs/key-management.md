# KMS/Vault 密钥提供器与轮换

`coifesp_harness.key_management` 是生产 KMS/Vault 的隔离边界。它不读取环境变量中的密钥材料，也不提供“provider 不可用时使用明文”的回退。

密钥引用必须是版本固定的 `kms://tenant.example/purpose/name/versions/v17` 或 `vault://…` URI。`purpose` 必须是已支持的 keyring 用途，URI 租户域必须与调用租户完全相同；不允许查询参数、凭据、`latest` 或未固定版本。

部署时实现 `KeyProvider.fetch` 和 `KeyProvider.retire`，由 SDK 以工作负载身份访问 KMS/Vault。Provider 返回 32 字节 `bytearray`；`KeyCache` 最多缓存 300 秒，并在过期替换和 `close()` 时清零缓存副本。日志、异常和 `repr` 不得记录 provider 响应或密钥材料。

`ExistingKeyringAdapter` 是通向现有 audit、collaboration envelope、memory、agent checkpoint/control、tool job 与 semantic checkpoint keyring 的唯一兼容桥。它可在迁移期构造 active new-write + old dual-read keyring；调用方须在进程退出时关闭 cache。已有 keyring 的内部拷贝由其自身生命周期管理，不能被此边界回收。

轮换按持久化计划执行：`prepare`（双版本可解析）→ `dual-read` → `new-write` → `reencrypt`（有 checkpoint，可恢复且可重试）→ `retire` → `complete`。`RotationStore` 在生产中必须以业务数据重加密和 checkpoint 在同一事务内实现。失败的 batch 不推进 checkpoint；重复的推进操作幂等。key custodian 负责 prepare/retire，rotation operator 负责切写与重加密，且 retirement 必须由未参与 prepare 或 reencrypt 的独立 custodian 批准。只有重加密报告完成且外部 completion gate 确认可双读已无旧密文时，才会调用 provider retire。Provider 的版本退休操作必须幂等，因为进程可能在 provider 已成功但轮换计划尚未持久化时重试；退休成功后本地 cache 会立即清零并驱逐旧版本。
