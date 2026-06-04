# Hermes Agent Web、TUI、ACP与批处理架构

## 一、Web前端架构

**技术栈：** React 19 + TypeScript + Vite 7 + Tailwind CSS 4 + React Router DOM v7 + @xterm/xterm

### 核心架构

```
BrowserRouter → I18nProvider → ThemeProvider → SystemActionsProvider → App
```

**App.tsx — 完整SPA：**
- 响应式侧边栏（折叠动画icon-only模式）
- 导航系统：内置路由 + 插件路由动态合并
  - 内置路由：/sessions、/analytics、/models、/logs、/cron、/skills、/plugins、/mcp、/channels、/webhooks、/pairing、/profiles、/config、/env、/system、/docs、/chat
  - 插件通过 `PluginManifest.tab` 注册：可新增、覆盖、隐藏页面
  - `buildNavItems()` 支持 `position: "after:X"` / `"before:X"` 排序
- 嵌入式聊天：`ChatPage` 作为持久化宿主，PTY/xterm/WebSocket在页面切换时不卸载
- 插件slot位置：backdrop、header-banner、header-left/right、pre-main、post-main、overlay

**Vite配置：**
- 自定义插件 `hermesDevToken()` 从运行中后端抓取会话token
- `/api` 路由代理到后端（含WebSocket支持）
- 构建输出到 `../hermes_cli/web_dist`（Python包内）

## 二、TUI Gateway

Agent与终端UI（Ink/Node.js）之间的桥接进程，通过stdio JSON-RPC 2.0通信。

### 组件关系

```
TUI(Ink/Node) → stdin → entry.py → server.dispatch → pool thread → transport.write → stdout → TUI
                                                                   ↳ WsPublisherTransport → dashboard
```

**entry.py — 主入口：**
- 通过stdin读取JSON-RPC请求行，通过stdout写入响应
- 首次启动发送 `gateway.ready` 事件
- 信号处理：SIGPIPE→SIG_IGN、SIGTERM→日志+优雅关闭、SIGINT→SIG_IGN
- MCP工具发现：后台daemon线程中运行，避免阻塞TUI启动
- Sidecar发布者：`HERMES_TUI_SIDECAR_URL` 设置时通过WebSocket mirror

**transport.py — 传输抽象层：**
- `Transport` 协议：`write(obj)` → bool
- `StdioTransport`：线程安全stdout写入，精细BrokenPipeError分类
- `TeeTransport`：主传输 + N个尽力而为辅助传输
- `ContextVar` 绑定：`_current_transport` 让每个请求上下文传输路由正确

**ws.py — WebSocket传输：**
- `WSTransport`：JSON帧→asyncio WebSocket
- 线程安全：`write()` 通过 `run_coroutine_threadsafe` 从池线程调用
- disconnect时回退到 `_stdio_transport`

**render.py — 渲染桥接：**
- 将对 `agent.rich_output` 函数的调用路由到Python侧渲染器
- `render_message()`、`render_diff()`、`make_stream_renderer()`

**slash_worker.py — 持久斜杠命令工作器：**
- 每个TUI会话维护一个 `HermesCLI` 实例
- 协议：`{id, command}` → `{id, ok, output|error}`
- 通过替换 `Console.file` 缓冲区捕获Rich输出

## 三、ACP适配器 (acp_adapter/)

将Hermes Agent作为ACP stdio服务器暴露给编辑器（Zed、VS Code、JetBrains）。

### 核心组件

**server.py — HermesACPAgent：**
- 协议操作：list_sessions（游标分页）、new_session、load_session、resume_session、fork_session、set_session_model、set_session_mode
- 提示处理：ThreadPoolExecutor上运行同步AIAgent（最多4 worker）
- 编辑器编辑快照：为write_file/patch捕获编辑前后图像
- 计划/待办集成：将Hermes的todo工具结果转换为Zed原生AgentPlanUpdate panel

**session.py — SessionManager：**
- 线程安全ACP会话到AIAgent实例映射
- WSL路径自动转换（`C:\...` → `/mnt/c/...`）

**tools.py — 工具映射：**
- 将50+ Hermes工具映射到ACP的ToolKind（read/edit/search/execute/think/fetch/other）
- 60个 `_POLISHED_TOOLS` 白名单，精心设计以呈现良好效果

**permissions.py — 审批桥接：**
- 将Hermes的 `prompt_dangerous_approval()` 桥接到ACP的 `request_permission()`
- 选项："Allow once"、"Allow for session"、"Allow always"、"Deny"、"Deny always"

**edit_approval.py — 编辑审批：**
- `ContextVar` 门控：仅ACP设置，CLI/网关会话绕过
- 策略：`ask`(总是提示)、`workspace_session`(工作区内自动)、`session`(全局自动)

### 注册表 (agent.json)

```json
{
  "id": "hermes-agent",
  "name": "Hermes Agent",
  "distribution": {
    "uvx": {"package": "hermes-agent[acp]==0.15.1", "args": ["hermes-acp"]}
  }
}
```

通过 `uvx` 在兼容ACP的编辑器中无缝安装。

## 四、批量轨迹生成 (batch_runner.py)

- **多进程并行：** `multiprocessing.Pool` 在CPU核心间分配提示
- **检查点/恢复：** 每个完成轨迹写入JSONL，支持 `--resume` 增量运行
- **工具集分布：** 每批采样自16个预定义分布（default、image_gen、research、science、development等）
- **工具统计提取：** `_extract_tool_stats()` 从消息历史解析tool_calls，跟踪成功/失败
- **轨迹格式：** `from/value` 对格式，包含 `<tool_call>/<tool_response>` XML标记

## 五、轨迹压缩 (trajectory_compressor.py)

### 压缩策略

**目标：** 在保留训练信号质量的同时压缩轨迹到目标token预算。

1. 保护关键轮次：系统提示词、第一轮人类消息、第一轮GPT消息、第一轮工具响应、最后N轮(N=4)
2. 仅压缩中间轮次：从第2轮工具响应开始的可压缩区域
3. 基于token的压缩：用单条摘要消息替换可压缩区域中的工具响应
4. 配置：目标最大15250 tokens、摘要750 tokens、分词器使用 `moonshotai/Kimi-K2-Thinking`

## 六、SQLite状态存储 (hermes_state.py, ~3956行)

### Schema (版本14)

**表：**
- `sessions`：id、source、user_id、model、system_prompt、parent_session_id（压缩链）、token统计、billing字段、title等
- `messages`：自增id、session_id(FK)、role、content、tool_call_id、tool_calls(JSON)、token_count、reasoning、reasoning_content、reasoning_details、codex数据等
- `state_meta`：任意key/value状态
- `compression_locks`：session_id(PK)、holder(pid:tid:nonce)、expires_at

### FTS5全文搜索 — 双表设计

1. **`messages_fts`**：unicode61分词器，处理拉丁文本
2. **`messages_fts_trigram`**：trigram分词器，处理CJK子串搜索（默认分词器将CJK字符拆为单token）

**触发条件：** INSERT/UPDATE/DELETE触发器自动维护两个FTS索引

**搜索逻辑：**
- CJK检测（`_contains_cjk()`）→ 3+ CJK字符路由到trigram表
- 短CJK（1-2字符）→ LIKE回退
- 非CJK → unicode61 FTS5 + BM25排名 + snippet()
- 三种排序模式：None(仅FTS5排名)、newest、oldest
- 上下文：每个匹配结果 + 前后1条消息

## 七、Docker容器构建

**多阶段构建：** Debian 13.4 + s6-overlay监督 + uv包管理器 + Node 22 LTS

**关键层：**
- s6-overlay v3.2.3.0：PID 1 /init → s6-svscan非阻塞回收僵尸进程，监督主hermes、仪表板和每个配置文件的网关
- Playwright Chromium：用于浏览器自动化
- Python依赖层缓存：`uv sync --frozen`
- 用户重新映射：`HERMES_UID` 可覆盖（默认10000）
- s6服务定义：每个profile的网关通过cont-init动态注册
- Docker exec填充程序：通过 `s6-setuidgid hermes` 透明重新执行

## 八、工具集与平台映射

**工具集系统 (toolsets.py)：**
- 叶子工具集~40个：web、vision、terminal、browser、skills、todo、memory等
- 组合工具集：debugging、safe、hermes-gateway
- 平台工具集~15个：hermes-cli、hermes-cron、hermes-acp及所有消息平台
- 共享核心(`_HERMES_CORE_TOOLS`)：55个工具在所有平台工具集中共享

**工具集分布 (toolset_distributions.py)：**
- 16个预定义分布，用于批量运行期间的概率工具集选择
- `sample_toolsets_from_distribution()` 按百分比独立掷骰子
