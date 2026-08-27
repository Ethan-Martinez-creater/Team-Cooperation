# OS 级 Sandbox

## 安全边界

编程工具不得在 Tool Worker 宿主进程中直接运行命令。生产实现使用 Docker 或 Podman 创建一次性 OCI
容器，并强制以下属性：

- 镜像必须是管理员 allowlist 中的 `repository@sha256:<64 hex>`，不接受 tag；
- 调用使用 argv，不经过 shell；模型只能选择管理员发布的 profile，不能选择镜像或宿主路径；
- profile 自带严格的参数 JSON Schema，固定参数和用户参数分别验证；
- `--network none`、只读容器根文件系统、`CAP_DROP=ALL`、`no-new-privileges`；
- 固定非 root UID/GID、PID/内存/CPU/tmpfs/时间和 stdout+stderr 合计上限；
- 每个 Job 仅挂载自己的工作区，工作区由租户和 Job ID 所有权标记绑定；
- 超时、输出超限或 Worker 取消时按确定性容器名 stop/rm，防止孤儿容器；
- 容器日志驱动关闭，有界 stdout/stderr 作为加密 Tool Job result 保存。

这不是多租户恶意代码隔离的最终形态。公网生产环境还应使用独立 Linux Worker 节点，并根据威胁模型
选择 gVisor、Kata Containers 或 microVM；Windows Docker Desktop 适合本地验收，不作为多租户生产
隔离声明。

## 配置

```dotenv
COIFESP_SANDBOX_RUNTIME=docker
COIFESP_SANDBOX_WORKSPACE_ROOT=E:\Graduate_work_folder\Agent_develop\Project\COIFESP_Agent\Project_Cooperate\.sandbox-workspaces
COIFESP_SANDBOX_PROFILES_JSON=[{"profile_id":"python.isolated","image":"python@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844","executable":"/usr/local/bin/python","fixed_arguments":["-I","-B"],"arguments_schema":{"type":"array","prefixItems":[{"const":"-c"},{"type":"string","minLength":1,"maxLength":32768}],"items":false,"minItems":2,"maxItems":2},"timeout_seconds":30,"memory_bytes":268435456,"cpu_count":1.0,"pids":64,"output_bytes":1048576,"tmpfs_bytes":67108864,"workspace_access":"read_write"}]
```

当前本地验收 profile 锁定到官方 Python 3.13.7 Alpine 3.22 的 amd64 manifest digest。它只用于本地
功能和隔离验收，不等于上线供应链批准。镜像构建、SBOM、漏洞扫描、签名验证和私有镜像仓库访问
策略仍属于上线供应链门禁；上线不能从公共仓库在执行时临时拉取未审核镜像。

配置后检查运行时：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_sandbox_runtime.py
```

完整隔离验收：

```bat
E:\miniconda3\envs\bettafish\python.exe scripts\check_sandbox_isolation.py
```

2026-08-12 已在 Docker Desktop 29.7.2 / WSL2 上通过真实验收：digest 锁定、UID/GID 65532、只读根、
`network=none`、capability 全部移除、no-new-privileges、PID/内存/CPU 限制、Job 工作区、输出合计预算、
超时和输出超限清理均已验证，无遗留容器。Docker Desktop 本机存在 Electron GPU 加速异常，启动时需
使用 `Docker Desktop.exe --disable-gpu`；该参数只影响桌面 UI 渲染。
