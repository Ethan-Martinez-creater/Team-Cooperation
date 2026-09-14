# Team Cooperation

Team Cooperation（代码包名 `coifesp-harness`）是一个 **Agent-first 的多团队协作系统**。它把 Agent 放在项目协作的主流程中：用户围绕项目持续对话，Agent 结合项目上下文协助规划、拆解任务、处理文件、生成交付内容，并作为不同团队之间的沟通桥梁。

本项目的重点是 Harness Agent 的完整协作体验，而不是把系统设计成传统项目管理后台。权限、审批、数据隔离和审计用于保证 Agent 能在团队边界内可靠工作，但不是产品页面的中心。

## 当前实现

### 项目与会话工作台

- 用户可查看或创建项目，进入项目后自动使用该账号在该项目中的唯一会话；用户无需手工管理会话。
- 项目主页面采用对话驱动布局，Agent 对话贯穿项目规划、执行、讨论和交付过程。
- 工作台将概览、数据、跨团队协作、计划和任务收拢为项目内功能，而不是拆成彼此割裂的后台页面。
- 支持项目参与团队与职责管理；项目负责人可把已建立协作关系的团队加入项目。
- 页面刷新后可恢复当前项目与本地演示身份，快速切换项目时会隔离旧请求与旧事件流。

### Agent 协作能力

- Agent Run 采用持久化队列、租约、心跳、fencing、检查点和失败恢复机制。
- Agent Worker 与 Tool Worker 默认以共享多租户池运行；任务租户取自权威持久化记录，单个账号或团队不需要各自常驻一组 Worker。
- 支持流式回复、继续对话、上下文窗口装配、早期上下文压缩提示和终态投影重放。
- 支持 OpenAI-compatible、OpenAI 和 Anthropic 模型 Provider，并提供并发限制、预算、故障转移和熔断能力。
- 支持工具调用、风险审批、幂等执行、Tool Worker 和隔离执行 profile。
- 支持可按需加载的 Skills、项目上下文、长期 Memory 和内容寻址 Artifact。

### 数据与交付物

- 用户可在项目对话中附加本地文件，也可上传代码、文档、表格、演示文稿、PDF、构建产物和测试报告。
- 文件可保持团队私有，或以项目只读资源的方式共享给项目参与团队。
- 上下文装配会依据项目、团队、分级和共享范围筛选数据，避免把团队私有信息带入跨团队回复。
- 支持代码/文档工作区、版本化交付物、完整性校验和项目资源管理。

### 多团队 Agent 沟通

- 发起团队可让 Agent 根据当前会话生成 Exchange 草稿，并在人工编辑确认后发送。
- 一个 Exchange 可同时面向多个接收团队，每个团队拥有独立状态与上下文快照。
- 接收团队可让自己的 Agent 起草回复，人工确认后再返回发起团队。
- 共享上下文按团队边界投影；未授权的团队私有数据不会传给其他团队 Agent。
- 起草、投影、失败释放和进程重启重放均具备幂等恢复路径。

### 计划、任务与协作治理

- Agent 可生成包含阶段、负责团队、约束和验收信息的项目计划草稿。
- 计划经人工确认后物化为阶段建议或团队任务，审批失败可安全重试且不会重复创建。
- 支持团队任务的接受、拒绝、排期调整、交付与验收，以及协作通知和待办入口。
- 支持有界 Specialist Agent 委派、任务结果回执、工具等待与验证/返工编排。
- 对外发送、敏感工具和关键状态变更保留明确的人工确认点。

### 身份与运行模式

- `local`：内置 lead、contributor、reviewer 三类演示身份，适合单机体验多团队协作，无需外部身份服务。
- `builtin`：系统内置账号、团队注册与会话续期流程。
- `oidc`：可选的组织身份接入，兼容 Keycloak 等 OIDC Provider。

OIDC 只是可选运行方式。体验 Harness 核心功能时，建议优先使用 `local` 模式。

## 技术结构

- 后端：Python 3.11+、FastAPI、Pydantic、SQLAlchemy
- 数据库：PostgreSQL、Alembic 版本化迁移
- 前端：服务端静态资源 + 原生 HTML/CSS/JavaScript
- Agent 运行时：独立 Agent Worker、Tool Worker、持久化 Run/Checkpoint/Projection
- Worker 拓扑：共享多租户 Agent/Tool Worker 池，兼容旧单租户变量；生产继续使用 OIDC 平台服务身份
- 模型接口：OpenAI-compatible、OpenAI、Anthropic
- 可观测性：OpenTelemetry、Prometheus、结构化日志
- 可选协议边界：MCP Streamable HTTP、A2A Agent Card/任务接口

MCP 与 A2A 已保留协议边界和基线实现，但当前产品重点是站内 Agent-first 团队协作，不以继续扩展协议功能为当前目标。

## 仓库内容

```text
.
├─ src/coifesp_harness/   核心应用、工作台、Agent/Tool Worker 与领域模块
├─ alembic/               PostgreSQL 数据库迁移
├─ tests/                 自动化回归、契约与故障恢复测试
├─ scripts/               配置、连通性、迁移和运行验收脚本
├─ docs/                  当前架构、配置、协议和使用说明
├─ deploy/                Keycloak、Kubernetes、可观测性及恢复示例资产
├─ .env.example           不含凭据的配置模板
├─ alembic.ini            Alembic 配置
└─ pyproject.toml         Python 包、依赖与命令入口
```

仓库不会提交以下本地或中间内容：

- `.env`、密钥、Token、OAuth Client Secret 和本地凭据；
- 数据库文件、Artifact 实体、日志、缓存、虚拟环境和测试临时目录；
- 迭代计划、审批记录、整改报告和一次性修补脚本；
- 本机 IDE、Codex 或浏览器验收产生的工作文件。

因此，仓库中的 `docs/` 是系统当前设计和使用说明，不包含开发过程中的阶段性计划与审核记录。

## 本地启动

### 1. 创建环境并安装

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 2. 配置环境

复制 `.env.example` 为 `.env`，至少配置：

```dotenv
COIFESP_ENV=development
COIFESP_AUTH_MODE=local
COIFESP_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/coifesp
COIFESP_LLM_PROVIDER=your_provider_id
COIFESP_LLM_MODEL=your_model
COIFESP_LLM_BASE_URL=https://your-provider.example/v1
COIFESP_LLM_API_KEY=your-local-secret
COIFESP_ARTIFACT_STORE_ROOT=./runtime-data/artifacts
```

`COIFESP_LLM_PROVIDER` 必须与 Provider 注册配置中的 ID 完全一致。`.env` 已被 Git 忽略，不要把真实凭据复制到 README、测试或示例文件。

### 3. 初始化数据库

先创建空的 PostgreSQL 数据库，再执行：

```bash
alembic upgrade head
```

### 4. 启动控制面与 Worker

本地演示推荐使用启动器。它会启动控制面，以及一组可处理三个演示租户的共享 Agent/Tool Worker，并把 PID、日志与 Artifact 写入被 Git 忽略的 `runtime-data/`：

```bash
python scripts/start_local_control_plane.py
python scripts/start_local_workers.py
```

也可以分别运行底层入口：

```bash
python -m uvicorn coifesp_harness.control_plane:create_application --factory --host 127.0.0.1 --port 8000
```

```bash
python -m coifesp_harness.worker_main
```

需要执行异步工具任务时，再启动：

```bash
python -m coifesp_harness.tool_worker_main
```

浏览器打开 <http://127.0.0.1:8000/app/>，选择本地演示身份即可进入工作台。

## 验证

运行全量自动化测试：

```bash
python -m pytest
```

仓库中的 `scripts/check_*.py` 用于按需验证 PostgreSQL、Worker、模型网关、OIDC、Artifact、审计、可观测性和恢复能力。正式环境配置与检查说明见：

- [配置说明](docs/configuration.md)
- [数据库与迁移](docs/database.md)
- [用户协作工作台](docs/user-workspace.md)
- [总体架构](docs/architecture.md)
- [Agent Run](docs/agent-runs.md)
- [模型网关](docs/model-gateway.md)
- [Artifact 存储](docs/artifact-storage.md)

## 外部集成边界

仓库已经实现 GitHub 原生适配器，支持查询 commit checks，以及经人工审批创建 Issue、触发 Actions workflow；这些操作通过持久 Tool Job、稳定幂等键和加密回执接入任务验收。使用方仍需为自己的部署提供允许的仓库、最小权限 GitHub Token 或 GitHub App installation token，仓库不会附带任何真实凭据。配置说明见 [GitHub 原生适配器](docs/github-adapter.md)。

GitLab、Email、Calendar、IM 等其他纵向平台尚未提供原生实现；未配置这些平台不影响站内 Harness 协作与 GitHub 闭环。

## License

Apache-2.0
