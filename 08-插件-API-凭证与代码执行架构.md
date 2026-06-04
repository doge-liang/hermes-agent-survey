# Hermes Agent 插件、API、凭证与代码执行架构

## 一、REST API服务器 (gateway/platforms/api_server.py)

### 端点体系

**OpenAI兼容：**
- `POST /v1/chat/completions` — Chat Completions格式（X-Hermes-Session-Id头部实现session连续性）
- `POST /v1/responses` — Responses API（通过previous_response_id连接）
- `GET /v1/models` / `GET /v1/capabilities` — 模型列表和能力描述

**Session管理：**
- `GET/POST /api/sessions` — 列出和创建session
- `GET/PATCH/DELETE /api/sessions/{id}` — 读取、更新、删除
- `GET /api/sessions/{id}/messages` — 读取历史
- `POST /api/sessions/{id}/fork` — 分支session
- `POST /api/sessions/{id}/chat[/stream]` — 持久化session对话

**Run管理（异步执行）：**
- `POST /v1/runs` — 启动run，返回202
- `GET /v1/runs/{id}` / `GET /v1/runs/{id}/events` — 状态和SSE事件流
- `POST /v1/runs/{id}/approval` / `POST /v1/runs/{id}/stop` — 审批和中断

### 核心设计

- `ResponseStore`：SQLite支持的LRU存储，用于Responses API历史重建
- `_IdempotencyCache`：基于键的TTL+LRU存储，支持幂等请求
- 认证：Bearer token认证（`X-Hermes-Session-Key`头部）
- Session连续性：客户端可提供session_id，或从对话指纹（SHA-256）派生稳定ID
- Streaming：SSE支持，包含工具进度事件

## 二、插件系统

### 2.1 发现机制

四个来源（后来源覆盖先来源）：
1. **内置插件**：`<repo>/plugins/<name>/`
2. **用户插件**：`~/.hermes/plugins/<name>/`
3. **项目插件**：`./.hermes/plugins/<name>/`
4. **Pip安装插件**：暴露 `hermes_agent.plugins` entry-point组

### 2.2 插件种类

| 种类 | 加载策略 | 说明 |
|------|----------|------|
| `standalone` | plugins.enabled选择加入 | 拥有自己的hooks/tools |
| `backend` | 自动加载（内置）；plugins.enabled控制（用户） | 核心工具的可插拔后端 |
| `exclusive` | 独立发现系统 | 恰好一个活跃提供者（如memory） |
| `platform` | 自动加载（内置）；plugins.enabled控制（用户） | 网关消息平台适配器 |
| `model-provider` | providers/__init__.py管理 | 模型提供者配置文件 |

### 2.3 PluginContext API

`register(ctx)` 函数接收的上下文提供：
- `ctx.register_tool(...)` — 注册工具到全局注册表
- `ctx.register_hook(hook_name, callback)` — 注册15个生命周期钩子之一
- `ctx.register_command(...)` — 注册slash命令
- `ctx.register_cli_command(...)` — 注册CLI子命令
- `ctx.register_platform(...)` — 注册网关平台适配器
- `ctx.register_auxiliary_task(...)` — 注册AUX LLM任务
- `ctx.register_context_engine(...)` — 注册上下文引擎（仅允许一个）
- `ctx.llm` — 受信任插件的托管LLM访问门面

### 2.4 15个生命周期钩子

- **工具**：`pre_tool_call`、`post_tool_call`
- **LLM调用**：`pre_llm_call`、`post_llm_call`
- **API请求**：`pre_api_request`、`post_api_request`
- **Session**：`on_session_start`、`on_session_end`、`on_session_finalize`、`on_session_reset`
- **输出转换**：`transform_terminal_output`、`transform_tool_result`、`transform_llm_output`
- **其他**：`subagent_stop`、`pre_gateway_dispatch`、`pre_approval_request`、`post_approval_response`

### 2.5 已安装插件

21个插件目录，包括：browser、context_engine、dashboard_auth、disk-cleanup、google_meet、hermes-achievements、image_gen、kanban、memory、model-providers、observability、platforms、security-guidance、spotify、teams_pipeline、video_gen、web等。

## 三、凭证管理系统

### 3.1 三层架构

**凭证池 (credential_pool.py) — 多凭证故障转移：**
- `PooledCredential`：provider、id、label、auth_type、priority、source、token、错误状态、速率限制冷却
- 4种选择策略：fill_first（默认）、round_robin、random、least_used
- 冷却/租约/死位状态机：STATUS_OK → STATUS_EXHAUSTED(冷却) → STATUS_DEAD(永久失败)
- 软租约并发控制：`acquire_lease()` / `release_lease()`，最小租约凭证优先，并发限制
- 多进程token同步：从共享存储同步以处理跨进程OAuth刷新

**凭证来源 (credential_sources.py) — 统一移除契约：**
- 每个来源注册 `remove_fn` 回调：清理磁盘 + 抑制重新种子 + 返回诊断

**凭证持久化 (credential_persistence.py) — 磁盘边界：**
- `is_borrowed_credential_source()`：区分"借用/仅引用" vs "拥有/可持久化"
- `sanitize_borrowed_credential_payload()`：剥离借用来源的原始秘密值

### 3.2 外部密钥源 (secret_sources/)

Bitwarden Secrets Manager集成：从BWS拉取API密钥 → 注入为环境变量 → 非破坏性（仅设置尚未存在的变量）→ 失败不阻塞启动

## 四、代码执行工具 (code_execution_tool.py)

### 程序化工具调用（PTC）

让LLM编写通过RPC调用Hermes工具的Python脚本，将多步工具链折叠到单次推理轮次。

### 两种传输

**本地后端（UDS）：**
1. 生成UDS RPC函数存根模块 `hermes_tools.py`
2. 打开Unix域套接字（`chmod 0o600`），启动RPC监听线程
3. 在子进程中运行LLM脚本
4. 工具调用通过UDS回传父进程分派
5. **Windows后备：** TCP回环（AF_UNIX不可靠）

**远程后端（文件RPC）：**
1. 存根 + 脚本通过base64编码的 `echo | base64 -d > file` 船运到远程
2. 轮询线程读取请求文件 → 分派 → 写入响应文件
3. 脚本轮询响应并继续

### 沙箱

- 允许的工具：仅 `SANDBOX_ALLOWED_TOOLS` ∩ session启用工具（web_search、read_file、write_file等7个）
- 资源限制：5分钟超时、最多50次工具调用、50KB stdout、10KB stderr
- 环境变量清理：`_scrub_child_env()` 剥离secret（KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL...）
- 安全：Ansi剥离 + 秘密编辑 + 进程组终止 + 审批门控
- 执行模式：project（活动venv）/ strict（隔离临时目录）

## 五、子代理委托 (delegate_tool.py)

### 隔离模型

每个子agent获得：
- 新conversation（无父历史）
- 自己的task_id
- 受限工具集——总是移除：delegation(无递归)、clarify(无用户交互)、memory(无共享MEMORY.md写入)、send_message(无跨平台副作用)、execute_code(子agent应逐步推理)
- 聚焦的系统提示词

### 角色：叶节点 vs 协调器

- `leaf`（默认）：不能进一步委托
- `orchestrator`：保留委托工具集，可生成自己的工作者
- `max_spawn_depth` 控制深度上限（默认=1，最大=3）

### 纵向执行

1. 凭证租约（从共享池获取软租约）
2. 心跳（后台线程每30秒触摸父时间戳，防网关空闲超时）
3. ThreadPoolExecutor + 非交互式审批
4. 硬超时（默认600秒）+ 零API调用诊断转储
5. File-state协调（过时读取提醒）
6. 清理（finally块：停止心跳、注销子agent、释放租约）

### 并行执行

批量任务：`ThreadPoolExecutor` 中多个 `_run_single_child` 同时运行，`FIRST_COMPLETED` + 中断检查轮询。

## 六、终端执行 (terminal_tool.py)

六个后端环境：Local / Docker / Singularity / SSH / Modal / ManagedModal / Daytona

**关键安全特性：**
- 危险命令审批：通过合并守卫（tirith + 危险命令检测）
- sudo密码管道：重写为 `sudo -S -p ''` 无头密码输入
- Workdir验证：仅安全字符白名单
- 复合后台重写：修复 `A && B &` subshell-wait bug
- 前台后台指导：长期运行的服务器命令自动提示 `background=true`

## 七、Shell Hooks (shell_hooks.py)

允许用户配置当特定事件发生时执行的shell脚本。

**Wire协议：** stdin(JSON) → stdout(JSON: block decision / context injection)

**安全：** shell=False、60秒默认超时(上限300秒)、JSON响应解析、非零退出码被记录但不阻止
