# 配置说明

单元测试不需要真实 LLM API 或 PostgreSQL；模型连通性、迁移和数据库冒烟检查使用项目负责人填写的
本地 `.env` 或部署平台 Secret，代码只读取环境变量。

仓库根目录的 `.env.example` 只列变量名。当前配置加载器不会隐式读取任意目录中的密钥文件；应用入口
必须显式选择是否加载本项目的 `.env`，防止误用同一机器上的其他项目凭据。

生产模式至少需要：

```dotenv
COIFESP_ENV=production
COIFESP_DATABASE_URL=postgresql+psycopg://...
COIFESP_OIDC_ISSUER=https://...
COIFESP_OIDC_AUDIENCE=...
COIFESP_AUDIT_KEY_ID=当前审计签名密钥版本
COIFESP_AUDIT_SIGNING_KEY=由KMS注入的高熵值
COIFESP_ENVELOPE_SIGNING_KEY=由KMS注入的高熵值
COIFESP_MEMORY_KEY_ID=当前Memory加密密钥版本
COIFESP_MEMORY_MASTER_KEY=Base64URL编码的32字节独立随机密钥
COIFESP_LLM_API_KEY=...
COIFESP_LLM_PROVIDERS_JSON=由安全审阅后的Provider注册表
```

禁止把值提交到 Git。开发阶段的 `.env.example` 只会包含变量名和解释，不提供貌似可用的弱默认密钥。

## 多 Provider LLM Gateway

旧的 `COIFESP_LLM_PROVIDER`、`COIFESP_LLM_MODEL`、`COIFESP_LLM_BASE_URL` 和
`COIFESP_LLM_API_KEY` 继续用于最小连通性检查。生产 Gateway 必须另外提供
`COIFESP_LLM_PROVIDERS_JSON`；它是严格字段、最多 32 项的 JSON 数组，未知字段会导致启动失败。
API Key 不得直接写进 JSON，`api_key_env` 只能引用 `COIFESP_*API_KEY` 环境变量。
可选 `tokenizer_encoding` 绑定本地 tiktoken encoding（例如 `cl100k_base`）；配置后路由、上下文窗口和
调用 token 上限使用该模型注册项的本地精确序列化计数。未配置时明确使用保守 UTF-8 估算，不宣称精确。

下面是一个外部 DeepSeek Provider 的安全起点。它将最大数据分级限制为 `public`，因此不能接收团队内部
资料；`region` 是组织自己的策略标签，不是云厂商地域声明，在法务/安全确认数据驻留前应保持
`external_unverified`。示例中的模型能力、上下文和价格依据 2026-08-06 的 DeepSeek 官方文档；上线时
仍须重新核对。输入价格使用 cache miss 单价进行保守预算：

```dotenv
COIFESP_LLM_API_KEY=由部署Secret注入
COIFESP_LLM_PROVIDERS_JSON='[{"provider_id":"deepseek_v4_flash","kind":"openai_compatible","model":"deepseek-v4-flash","base_url":"https://api.deepseek.com","api_key_env":"COIFESP_LLM_API_KEY","capabilities":["tool_calling","streaming","json_output"],"max_data_classification":"public","region":"external_unverified","external":true,"context_window_tokens":1000000,"max_output_tokens":384000,"input_microusd_per_million_tokens":140000,"output_microusd_per_million_tokens":280000,"priority":10,"max_concurrency":8,"timeout_seconds":60,"max_retries":2}]'
COIFESP_LLM_MAX_FAILOVER_ATTEMPTS=2
COIFESP_LLM_CONCURRENCY_WAIT_SECONDS=2
COIFESP_LLM_CIRCUIT_FAILURE_THRESHOLD=3
COIFESP_LLM_CIRCUIT_COOLDOWN_SECONDS=30
```

价格字段的单位是“每一百万 token 对应的微美元数”，例如 `$0.14 / 1M token` 写成 `140000`。
若要把团队内部或更高分级数据发送到外部模型，必须同时经过组织级数据处理审阅，并显式提高 Provider
的 `max_data_classification` 和每次 Run 的 `allow_external_egress`；仅修改其中一处仍会失败关闭。
完整语义见 [多 Provider LLM Gateway](model-gateway.md)。当前官方模型和价格页面：
[DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)。

### 本地 INTERNAL 演示项目授权

经项目负责人明确授权后，`local + development` 环境可以让指定的外部 Provider 处理
`INTERNAL` 级演示项目上下文。推荐在仓库根目录创建被 Git 忽略的
`.env.local-model-authorization`，只写 Provider ID，不写 API Key：

```dotenv
COIFESP_LOCAL_EXTERNAL_INTERNAL_PROVIDERS=deepseek_v4_flash
```

Provider ID 必须已经存在于 `COIFESP_LLM_PROVIDERS_JSON`，并且该 Provider 必须声明
`external=true`。`scripts/start_local_workers.py` 仅在启动出的本地子进程环境中把名单内
Provider 的 `public` 上限提升为 `internal`；它不会降低原本的 `confidential` 或
`restricted` 上限，也不会改写 `.env`。控制面、共享 Agent Worker 和 Tool Worker 必须在
变更后全部重启，确保会话、Planner、Exchange 与团队 Agent 使用同一份精确 allowlist。

该开关在 `production` 或 `builtin` 认证模式下会令配置校验失败，默认空值仍保持
`INTERNAL` 上下文禁止外部出站。生产 OIDC、旧单租户 Worker 配置和正式数据策略不受影响。

### 生产 INTERNAL 外部 Provider 授权

生产 OIDC 部署默认仍禁止把 INTERNAL 上下文发送给外部 Provider。项目负责人明确完成数据处理
审批后，可以用独立的生产 allowlist 精确授权已注册的 Provider：

```dotenv
COIFESP_PRODUCTION_EXTERNAL_INTERNAL_PROVIDERS=deepseek_v4_flash
```

该配置仅允许在 `production + oidc` 下使用。名单中的 Provider 必须声明
`external=true`，并将 `max_data_classification` 明确配置为 `internal` 或更高；
未列入名单、只声明 `public`、拼写错误或未注册的 Provider 都会启动失败。此项只授权模型
路由，不改变项目资源共享范围，也不允许向其他团队传播 team-private 数据。

## Worker 服务身份

本地 Keycloak、真实 OIDC、Agent Worker client-credentials 和最小权限目录读取已经完成验收。公网
生产环境仍必须使用 HTTPS/WSS、外部 Secret 管理和密钥轮换。

Agent Worker、Tool Worker、目录读取器使用三个不同 confidential client，密钥不得复用：

```dotenv
COIFESP_WORKER_CLIENT_ID=coifesp-agent-worker
COIFESP_WORKER_CLIENT_SECRET=由Keycloak生成
COIFESP_TOOL_WORKER_CLIENT_ID=coifesp-tool-worker
COIFESP_TOOL_WORKER_CLIENT_SECRET=由Keycloak单独生成
COIFESP_DIRECTORY_CLIENT_ID=coifesp-directory-reader
COIFESP_DIRECTORY_CLIENT_SECRET=由Keycloak单独生成
```

生产部署默认使用受控的多租户共享 Worker 池，而不是为每个账号常驻一组 Worker。两个进程分别配置
租户允许列表，通常应保持一致：

```dotenv
COIFESP_WORKER_TENANTS=tenant-a,tenant-b,tenant-c
COIFESP_TOOL_WORKER_TENANTS=tenant-a,tenant-b,tenant-c
```

OIDC client 表示平台级 Worker 服务身份，不绑定某个永久租户。Agent Run 和 Tool Job 自身携带权威
`tenant_id`；Worker 只有在该值属于自身允许列表时才可领取任务，仓储查询、租约、审计、连接器和工具
调用继续按任务租户隔离。Agent Worker 只接受 `agent_worker` 角色，Tool Worker 只接受
`tool_worker` 角色，`execution_worker` 或另一类 Worker 的角色不能替代。

旧变量 `COIFESP_WORKER_TENANT_ID` 和 `COIFESP_TOOL_WORKER_TENANT_ID` 仅保留给已有单租户部署平滑
升级。它们分别不能与对应的复数变量同时设置，冲突时启动会失败；新生产部署不得再使用旧变量。共享池
允许列表应保持有界，并由部署配置审阅后变更，不能通过用户请求动态扩大。

A2A 所需的本地真实 OIDC 环境已经完成验收；当前只实现并测试 Agent Card、端点校验和最小任务
交付消息构造，不挂载可执行的入站 A2A 路由，原因是协议业务路由、授权适配和持久 TaskStore 尚未
实现。MCP 客户端核心不依赖 OIDC；连接具体远端 MCP 服务时，
其 HTTPS URL、服务凭据和本地工具授权策略必须由部署 Secret/控制面配置提供，凭据不得写入 Agent
Card、工具描述、日志或审计详情。

当前完整协议边界见 [MCP / A2A 协议实施基线](protocol-implementation-baseline.md)。

## 可观测性配置

OpenTelemetry、Prometheus 和结构化日志全部由显式环境开关控制。当前没有 OTLP Collector 时保持
`COIFESP_TELEMETRY_ENABLED=false`，无需填写占位地址。生产环境打开 Metrics 后必须配置独立的
`COIFESP_METRICS_BEARER_TOKEN`，不能复用审计、Envelope、Memory 或 LLM 密钥。完整参数和部署边界
见 [可观测性与秘密安全](observability.md)。

## 安全连通性检查

配置完成后可执行：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_connections.py
```

脚本对数据库只执行 `SELECT 1`，对模型只发送一个最小请求，并且不会打印数据库密码、API Key 或签名
密钥。也可以使用 `--database-only` 或 `--llm-only` 单独检查。

配置多 Provider 注册表后，使用流式路径验证完整 Gateway：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_model_gateway.py --provider-id deepseek_v4_flash
```

该脚本固定使用 `PUBLIC` 测试消息、256 token 输出硬上限和单 Provider allowlist，不会输出模型正文或
远端错误响应体。使用 `--non-streaming` 可单独验证非流式路径。

完成迁移后可执行加密 Memory 与 RLS 冒烟检查：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_memory.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_audit.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_governance.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_execution.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_collaboration.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_tool_jobs.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_tool_batch.py
E:\miniconda3\envs\bettafish\python.exe scripts\check_postgres_memory_lifecycle.py
```

该脚本会验证 Alembic 版本、强制 RLS、租户策略、数据库账号权限、原子幂等和密文往返。它只创建一个
随机测试记录，最后按租户和明确记录 ID 清理，不打印连接凭据或 Memory 密钥。若数据库账号具有
`SUPERUSER` 或 `BYPASSRLS`，检查会失败；生产应用账号不得拥有这些属性。

Memory 加密密钥必须独立于审计和消息签名密钥。本地开发使用 32 字节随机值；生产环境由 KMS/HSM
提供 envelope encryption 和轮换，`COIFESP_MEMORY_KEY_ID` 用于识别密文所使用的密钥版本。
Agent checkpoint 与 steering/follow-up 正文从该 master key 使用不同 HKDF domain 和租户上下文派生
各自的数据密钥，不直接复用 Memory 数据密钥。轮换 key ID 前必须保留历史密文的解密路径。

`COIFESP_AUDIT_KEY_ID` 不是秘密，只是审计签名密钥的稳定版本名称，例如 `audit-v1`。轮换时新增
`audit-v2`，历史验证仍需保留旧版本的验证能力；不得修改已经写入事件所记录的 key ID。

## 在线密钥轮换

版本化 keyring 使用 JSON 引用独立环境变量，密钥材料不得直接内联到 JSON：

```dotenv
COIFESP_AUDIT_KEY_ID=audit-v2
COIFESP_AUDIT_KEYS_JSON=[{"key_id":"audit-v1","key_env":"COIFESP_AUDIT_KEY_V1"},{"key_id":"audit-v2","key_env":"COIFESP_AUDIT_KEY_V2"}]
COIFESP_ENVELOPE_KEY_ID=envelope-v2
COIFESP_ENVELOPE_LEGACY_V1_KEY_ID=envelope-v1
COIFESP_ENVELOPE_KEYS_JSON=[{"key_id":"envelope-v1","key_env":"COIFESP_ENVELOPE_KEY_V1"},{"key_id":"envelope-v2","key_env":"COIFESP_ENVELOPE_KEY_V2"}]
COIFESP_MEMORY_KEY_ID=memory-v2
COIFESP_MEMORY_KEYS_JSON=[{"key_id":"memory-v1","key_env":"COIFESP_MEMORY_KEY_V1"},{"key_id":"memory-v2","key_env":"COIFESP_MEMORY_KEY_V2"}]
```

对应的 `*_KEY_V1`、`*_KEY_V2` 值由部署 Secret/KMS 注入。配置 versioned keyring 后应移除旧的单值
变量，避免两套活动密钥定义发生冲突。发布顺序固定为：先部署“旧+新、旧为活动”，再切换活动 key ID，
执行并审计数据重加密，验证所有历史数据，最后才移除旧密钥。Envelope v2 会把 key ID 纳入签名；迁移
期间 `COIFESP_ENVELOPE_LEGACY_V1_KEY_ID` 明确指定无 key ID 的历史 v1 信封使用哪个旧密钥，迁移完成后
删除该设置即可失败关闭 v1 验证。

Memory 搜索的关键词候选使用按租户和 key 版本派生的 HMAC 盲索引，索引中不保存明文词项。部署迁移
`20260813_21` 后应执行 `scripts/check_memory_search_index_inventory.py`；若报告未完成，必须由运维面按
明确 tenant 调用 `MemorySearchIndexService.backfill_batch`，不能跨租户解密扫描。混合语义检索只有在
显式配置实现 `MemoryEmbeddingProvider` 的受审阅 Provider 后才可启用；外部 Provider 还要求逐请求
允许外发且其数据分级上限必须覆盖候选 Memory，未配置时系统失败关闭而不会生成伪语义向量。

Memory 删除采用两阶段流程：申请后立即撤销召回，随后由不同的 `memory_privacy_officer` 批准物理清除；
活动法律保留会阻止清除。拒绝申请会恢复原生命周期状态。审计只保存请求 ID、Memory ID 和理由摘要，
不保存被删除正文。

`MemoryKeyRotationService.rotate_batch` 对单个租户按最多 500 条记录加行锁、乐观版本检查，在同一事务中
提交密文和 `memory.key_rotation` 签名审计。它不会跨租户扫描；运维面必须显式提供租户、操作者和来源
key ID，循环到返回 `complete=true`。Memory、Agent checkpoint/control 与 Tool Job 都支持活动版本写入和
历史版本读取，幂等摘要也接受仍加载的历史 key 版本；在这些表都迁移并抽样解密验证前禁止移除旧 key。
