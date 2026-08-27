# 用户协作工作台

控制面现在同源提供 `/app/` 工作台，普通团队成员无需手工构造 API 请求。首版覆盖：

- Keycloak Authorization Code + PKCE 登录和退出；
- 协作总览、我的项目、跨项目任务收件箱；
- 主导者创建项目、添加成员、创建计划、开启讨论、分配任务和验收；
- 配合者参与讨论、批准计划、接受/开始任务并提交制品引用；
- 审批中心；
- 创建 Agent Run、查看经过授权投影的对话正文和实时 SSE 生命周期事件；
- 对运行中的 Agent 调整方向，对已完成运行继续追问；
- 在项目内直接上传不可变资料、网页预览，并按团队私有/项目内只读/可保存传播等级限制下载和分享；
- 响应式桌面/移动布局、键盘可用的原生表单和 `aria-live` 操作反馈。
- 可搜索、游标分页的团队目录和关系状态；团队管理员从选择器发起合作申请，不再手工填写团队标识。
- 团队任务支持优先级（低/普通/高/紧急）、截止时间和逾期识别；未接受任务的期限由来源团队直接维护，已接受任务的期限必须经对方团队确认的变更提议才会生效，所有变更进入项目活动流。
- 协作收件箱按“逾期优先、其次优先级、再按截止时间”排序，并提供全部、临期（48 小时）、逾期、我负责快速筛选；收件箱 Agent 简报包含服务端计算的 `priority`、`due_at`、`is_overdue` 和排序规则说明。
- 账户级通知中心：项目活动按唯一投影键生成站内通知（任务、消息、资料、议题、Agent、临期、逾期七类），支持未读/全部/已归档页签、类别与项目筛选、逐条已读/归档/恢复和“本页全部已读”；通知偏好可按类别开关、设置临期窗口、IANA 时区和跨午夜静默时段（静默时段只影响主动提示，不阻止通知创建）。

## Keycloak 客户端

沿用现有公开客户端 `coifesp-local-ui`，必须启用 Standard Flow 和 PKCE `S256`，关闭 Direct Access
Grants、Implicit Flow、Service Accounts 和 Client Authentication。配置：

- Valid redirect URI：`http://127.0.0.1:<控制面端口>/app/`；生产使用实际 HTTPS origin；
- Web origin：与控制面 origin 完全一致；
- Post logout redirect URI：同 origin 的 `/app/`；
- `COIFESP_UI_OIDC_CLIENT_ID=coifesp-local-ui`；
- `COIFESP_OIDC_AUTHORIZED_PARTIES` 必须包含同一 client ID。

工作台只公开 issuer、client ID、audience 和回调地址，不需要也不接受 Client Secret。Access Token 仅放在
浏览器 `sessionStorage`，关闭标签页后失效；生产仍要求 HTTPS。当前退出会清除本地 Token，Keycloak 全局
单点退出、静默续期和 Token Refresh 属于下一阶段会话体验。

本地验收账号使用 `coifesp-admin`。Realm User Profile 已注册只允许管理员编辑的 `tenant_id`、`clearance`
和 `compartments` 属性；账号当前映射到 `team-a`、`restricted`、`program-1`，并拥有
`artifact_publisher` Realm Role。临时密码不得写入仓库，首次登录必须修改。

## 当前产品边界

这是第一版可操作工作台，不代表产品层已经全部完成。Agent 工作台不会返回 system prompt、工具参数或
工具原始结果；浏览器使用带 Bearer Header 的 HTTP 控制命令和流式 `fetch`，不把 Token 放进 WebSocket
URL。项目资料的上传、下载和安全预览已经接入核心项目路径。跨项目收件箱现已汇总任务待办和按账户维护的
未读活动，并支持直接跳回对应项目；收件箱内可启动“优先级梳理”或“状态简报” Agent，查看当前账户自己的
历史运行并重新打开会话。Agent 的可信指令和实时收件箱简报由服务端注入，浏览器不能伪造上下文，且运行不会
自动执行跨团队操作。首页统计也已切换为团队项目、协作待办、未读动态和 Agent 运行。
团队目录和核心项目表单的团队/内部账户选择器已经接通。仍需：Git、Issue、文档和办公连接器工作区，
以及 Agent 工具装配、Skills 选择和登录续期体验（对应实施计划迭代 2–4）。工作台不会把这些未接线能力
显示为已经可用。
