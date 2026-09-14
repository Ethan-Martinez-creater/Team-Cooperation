# GitHub 原生适配器

适配器是 Tool Worker 与 GitHub REST API 之间的单团队小服务，复用当前连接器协议，不引入新的用户登录或 OIDC 系统。
它支持 Issue 创建、Actions workflow dispatch 和指定提交的 check-runs 查询。主程序仍通过已批准的连接器目录、Tool Job 和既有审批执行操作。

## 启动前需要准备

- 一个明确允许测试的 GitHub 仓库。不要默认使用主项目仓库进行写入测试。
- 一个限于这些仓库的 GitHub fine-grained PAT 或有效的 GitHub App installation token。按实际启用功能配置 Issues 写、Actions 写权限。读取公开仓库 Checks 无需额外 Checks 权限；私有资源须核对 Checks 读权限。令牌失效时须在部署环境更新，不在模型上下文中传递。
- 选择接入方式：本机开发使用下述 embedded 模式，无需域名；独立部署使用 HTTPS 域名及证书/反向代理。独立进程只监听 `127.0.0.1:8011`。
- 一个现有、可写的持久目录保存 SQLite 操作账本。不要把账本作为可丢弃缓存，也不要让不同团队共用同一账本及认证配置。

请在本机私有环境中设置以下变量，不要把实际令牌提交到 Git 或发给 Agent：

```text
COIFESP_GITHUB_ADAPTER_CLIENT_ID=team-a-github
COIFESP_GITHUB_ADAPTER_CLIENT_SECRET=<至少32字符的专用随机密钥>
COIFESP_GITHUB_TOKEN=<限仓库令牌>
COIFESP_GITHUB_REPOSITORIES=["owner/test-repo"]
COIFESP_GITHUB_ADAPTER_LEDGER=E:/your-existing-private-directory/github-adapter.sqlite3
```

从项目环境执行 `python -m coifesp_harness.connectors.github_adapter`；安装项目后也可用 `coifesp-github-adapter`。
环境不完整时启动失败，不生成默认密钥或偷偷改用任意仓库。

Tool Worker 配置/批准连接器 revision 时使用：

```json
{
  "connector_id": "github-main",
  "tenant_id": "team-a",
  "base_url": "https://your-adapter-domain.example",
  "token_endpoint": "https://your-adapter-domain.example/oauth/token",
  "client_id": "team-a-github",
  "client_secret_env": "COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET",
  "scopes": ["github.adapter"],
  "allowed_paths": ["/v1/github/issues", "/v1/github/workflow-dispatches", "/v1/github/commit-checks"],
  "max_classification": "internal",
  "timeout_seconds": 15,
  "max_response_bytes": 1048576,
  "max_attempts": 3,
  "circuit_failure_threshold": 5,
  "circuit_cooldown_seconds": 30
}
```

部署 JSON 为上述对象的数组。既有 active revision 审批和部署 connector ID 允许范围仍然生效。此处 scope 仅为内部适配器调用权限，不是 GitHub OAuth scope。

注意：连接器加载器要求密钥引用名称符合 `COIFESP_CONNECTOR_*_CLIENT_SECRET`。因此实际填写
`client_secret_env` 时使用 `COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET`，其值须与适配器的
`COIFESP_GITHUB_ADAPTER_CLIENT_SECRET` 相同；两者是同一个内部服务密钥，不是 GitHub Token。

## 本机开发：无需域名

Tool Worker 在非 production 环境显式设置 `COIFESP_GITHUB_ADAPTER_MODE=embedded`，并加载以上适配器环境变量。
Embedded 适配器使用一组 Token、仓库 allowlist 和持久 ledger，因此运行时只允许一个 GitHub connector 租户；多个团队需要分别使用外部 HTTPS 适配器及各自的凭据与仓库 allowlist，不能把多个团队仓库合并进同一个 embedded 配置。
连接器的 `base_url` 使用 `https://coifesp-github-adapter.invalid`，`token_endpoint` 使用
`https://coifesp-github-adapter.invalid/oauth/token`。这是固定的进程内路由标识，不访问 DNS、不监听端口、
无需 hosts 配置、证书或公开域名；不要尝试在浏览器中打开它，也不需要单独启动 8011 进程。

实际 GitHub 请求仍由 NativeGitHubClient 使用 HTTPS 发往 api.github.com。既有服务认证、批准的连接器目录、
Tool Job、审批及持久账本继续使用。其他 HTTPS 连接器的传输方式保持原样。
未配置 mode 时仍为 external；配置错误不自动回退，production 拒绝 embedded 模式。

## 重试边界

写入前先持久记录幂等键及请求摘要。相同键、相同参数且已成功的请求返回已有最小回执；改变参数返回 409。
网络超时、进程崩溃或回执保存失败可能使远端结果未知，此时原键返回 `write_outcome_unknown_do_not_resend`，不会再次写入。
这不是远端 exactly-once 承诺，也不保证所有失败都能自动恢复。必须先人工检查 GitHub 对应仓库/Actions 历史，
确认外部结果后决定后续操作；不能清空账本或自动换新键来“修复”。只读检查查询可以重新执行。

账本不保存 Issue 正文、workflow inputs 或 GitHub token，只保存加密密钥派生的请求摘要及最小响应。轮换内部客户端密钥前，
应完成现有待处理写入；旧请求摘要会因密钥轮换不匹配而被拒绝，不能为此清除账本。

## 真实验收顺序

1. 用户提供测试仓库名、测试 commit SHA、必需检查名称和 workflow 文件/ref，并在本机配置令牌。
2. 先执行只读 check-runs 查询，核对仓库/SHA/检查名称及待决结果，确认无越界访问。
3. 经用户允许在测试仓库创建一个标记清楚的 Issue；重复相同 Job 不应多建 Issue。
4. 经用户允许触发指定测试 workflow；不把“dispatch 已接受”当成“检查已成功”。
5. 从任务验收消费检查结果，确认私有验证资料可下载、陈旧提交不能被批准。

未经以上实测，模拟供应商测试通过不代表真实账号权限、网络和平台行为已验收。GitLab、文档平台、Calendar、IM/Email
需要用户指定实际使用的平台及测试范围，不自动选平台或开通账号。

## 原生接口依据

客户端使用 GitHub REST API `2026-03-10`。Issue 创建依照 [Issues API](https://docs.github.com/en/rest/issues/issues#create-an-issue)，
workflow 调度依照 [Workflows API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)，
提交检查依照 [Check runs API](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)。
检查结果超过 100 项或仍有下一页时不会被声明完整；未完成查询不能据此通过任务验收。
