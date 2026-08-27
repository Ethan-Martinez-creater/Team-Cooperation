# COIFESP 本地 Keycloak 部署

该配置面向本地真实 OIDC 联调，不使用 H2、默认管理员密码、Direct Access Grant 或隐式授权流。
Keycloak 使用独立 PostgreSQL 数据库和最小权限账号；UI 使用 Authorization Code + PKCE S256；Worker
使用独立 confidential service-account client。另有 `coifesp-directory-reader` 服务客户端，只给它的
service account 分配 `realm-management` 的 `view-users`、`query-users`、`query-groups`，用于读取
Worker 所需的实时用户、团队和角色信息。

Realm 导出提供 `lead`、`contributor`、`reviewer`、`observer`、`tool_approver`、`agent_worker` 和
`tool_worker` 角色，
并把 `tenant_id`、`clearance`、`compartments`、顶层 `roles` 和 API audience 写入 token。用户应加入唯一
团队组，并由管理员授予最小必要 Realm Role。不得允许用户自行修改这些安全属性。

`keycloak.env.example` 只是一份变量清单。填好的 `keycloak.env` 必须保留在 `E:\keyclock\runtime`，不得
放入 Git。生产部署还必须使用 HTTPS、反向代理、外部 Secret 管理、数据库备份和集群缓存；本地 HTTP
仅绑定 `127.0.0.1`。

运行时不得复用 bootstrap 管理员。`coifesp-agent-worker`、`coifesp-tool-worker` 和
`coifesp-directory-reader` 的客户端密钥必须分别生成并复制到项目本地 `.env`，不得提交、打印或写入
普通日志。`coifesp-tool-worker` 的 service account 只分配 `tool_worker` Realm Role，不分配
`agent_worker`、目录管理角色或任何人类协作角色。

项目 `.env` 中还需填写：

```dotenv
COIFESP_TOOL_WORKER_TOKEN_ENDPOINT=http://127.0.0.1:8080/realms/coifesp/protocol/openid-connect/token
COIFESP_TOOL_WORKER_CLIENT_ID=coifesp-tool-worker
COIFESP_TOOL_WORKER_CLIENT_SECRET=Keycloak Credentials 页面生成的独立密钥
COIFESP_TOOL_WORKER_TENANT_ID=team-a
```

填写后运行 Tool Worker 身份验收；验收脚本只应输出状态，不得输出密钥或 token。
