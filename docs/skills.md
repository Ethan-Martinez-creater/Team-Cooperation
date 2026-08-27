# Skills 安全供应链

Skill 是按需加载的工作说明，不是权限容器。即使 Skill 已签名，它也只能声明运行所需的工具；最终可用
工具仍由主体身份、Tool Registry 和策略决策点共同决定。

## 包目录

```text
skills/
  <tenant_id>/
    <skill-name>/
      <semver>/
        SKILL.md
        SKILL.sig
```

`SKILL.sig` 是对 `SKILL.md` 原始 UTF-8 字节的 Ed25519 detached signature，再使用 Base64 编码。
信任根按租户和 `signer_key_id` 隔离，一个租户信任的公钥不会自动成为全局信任根。

## Manifest

```yaml
---
name: review-contract
version: 1.0.0
description: Review a shared API contract.
tenant_id: team-a
classification: INTERNAL
compartments:
  - project-x
required_tools:
  - read_file
signer_key_id: release-key-1
---
这里是按需加载的 Skill 正文。
```

加载过程验证：

- 包路径必须与 `tenant_id/name/version` 完全一致；
- 禁止符号链接和目录逃逸；
- 文件大小、UTF-8、YAML 字段和 Semantic Version 必须有效；
- detached signature 必须来自该租户的受信 Ed25519 公钥；
- 主体必须通过租户、classification 和 compartment 检查；
- `required_tools` 必须是当前主体已经拥有的工具子集。

签名只证明来源与完整性，不证明正文天然安全。正文进入上下文时标记为
`instruction_trust="untrusted"`，不得进入系统指令层，也不能覆盖安全策略。
