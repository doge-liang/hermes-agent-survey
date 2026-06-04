# Hermes Agent CLI与配置系统架构深度分析

## 一、入口点与命令路由

### 1.1 双入口设计

- **`cli.py`** (15,783行)：基于prompt_toolkit的交互式REPL客户端，传统TUI聊天循环
- **`hermes_cli/main.py`**：统一的 `hermes` CLI入口，argparse子命令路由

### 1.2 完整子命令树

```
hermes
├── chat — 交互式聊天（默认）
├── model — 模型选择器
├── fallback — 回退提供商链管理 (list/add/remove/clear)
├── secrets bitwarden — Bitwarden密钥管理器 (enable/disable/status)
├── migrate xai — 迁移已退役模型
├── gateway — 消息网关管理 (start/stop/restart/status/install/uninstall/setup)
├── proxy — 反向代理管理
├── setup — 交互式设置向导
├── login / logout — OAuth设备授权
├── auth — 凭证池管理 (add/list/remove/reset/status/logout/spotify)
├── sessions — 会话管理 (list/browse/rename/delete)
├── config — 配置管理 (view/edit/set/wizard)
├── doctor — 系统诊断 (--fix, --ack)
├── logs — 日志文件查看器 (--follow, --since, errors)
├── debug — 调试报告上传
├── version / update / uninstall — 版本管理
├── cron — 定时任务管理
├── dashboard — Web UI仪表板
├── send — 消息发送
├── acp — ACP服务器（编辑器集成）
├── postinstall — 安装后脚本
└── honcho — 内存集成管理
```

## 二、配置系统

### 2.1 配置分层

优先级从高到低：
1. **环境变量** — 运行时最高优先级
2. **`~/.hermes/config.yaml`** — 用户配置
3. **`DEFAULT_CONFIG`** — 硬编码后备（内置于 `hermes_cli/config.py`）

密钥来源：
1. `~/.hermes/.env` — 用户密钥（覆盖陈旧shell导出）
2. 项目 `.env` — 仅开发后备
3. Bitwarden密钥管理器 — 可选外部密钥源
4. 进程环境变量

### 2.2 config.yaml加载流程

`load_config()`：
1. `ensure_hermes_home()` 创建 `~/.hermes/`
2. 检查内存缓存（以 `(path, mtime_ns, size)` 为键）
3. 缓存命中：返回 `deepcopy`
4. 缓存未命中：解析YAML → `_deep_merge` + DEFAULT_CONFIG → 规范键名 → `_expand_env_vars` → 存入缓存

`load_config_readonly()`：热路径优化，跳过deepcopy（节省~135us/调用）

`save_config()`：获取锁 → 检查托管部署 → 保留env模板 → 原子YAML写入（临时文件 + fsync + 重命名）

### 2.3 .env加载 (env_loader.py)

`load_hermes_dotenv()`：
1. 修复损坏的.env文件（拆分串联行）
2. 加载 `~/.hermes/.env` (override=True)
3. 加载项目 `.env` (override=False)
4. 净化凭证env var非ASCII字符（API密钥必须纯ASCII作为HTTP头）
5. 应用外部密钥源（Bitwarden Secrets Manager）
6. 同进程内幂等（每进程最多从BSM拉取一次）

**安全：** `.env`文件强制 `0o600` 权限，拒绝写入已知拒绝列表中的键（`LD_PRELOAD`、`PYTHONPATH`、`PATH`等）

## 三、Curses TUI渲染器 (curses_ui.py)

通用菜单事件循环 `_run_curses_menu()`，三个公共API：

| API | 类型 | 用途 |
|-----|------|------|
| `curses_radiolist()` | 单选 | 选择一项（如选提供商） |
| `curses_checklist()` | 多选 | 切换选择（如选工具集） |
| `curses_single_select()` | 带取消单选 | 选一项或返回None |

**功能特性：**
- Vim风格导航（j/k）+ 方向键
- 搜索/过滤：`/` 键打开内联搜索，使用模糊匹配（移植自TypeScript `fuzzyScore`，跨TUI和Web界面行为一致）
- 回退模式：当curses在真实TTY上失败时，使用数字选择回退
- 输入刷新：curses.wrapper()返回后排空操作系统缓冲中的陈旧转义序列

## 四、认证系统

### 4.1 提供商注册表 (auth.py)

`PROVIDER_REGISTRY`：34个提供商配置，支持4种认证类型：
- **oauth_device_code**：Nous Portal（设备授权流程）
- **oauth_external**：OpenAI Codex、xAI Grok、Qwen、Google Gemini CLI
- **oauth_minimax**：MiniMax自定义流程
- **api_key**：OpenAI、Anthropic、Gemini、DeepSeek等
- **aws_sdk**：Bedrock凭证链

插件系统自动扩展注册表：`plugins/model-providers/<name>/` 中的提供商被合并。

### 4.2 提供商解析

`resolve_provider(requested="auto")` 优先级：
1. `auth.json` 中活跃OAuth提供商
2. 显式CLI api_key → openrouter
3. `OPENAI_API_KEY` 或 `OPENROUTER_API_KEY` env var → openrouter
4. 按提供商检查API密钥env var
5. AWS Bedrock凭证链
6. 兜底：openrouter

### 4.3 凭证池系统 (auth_commands.py)

支持多凭证按提供商分组：
- **选择策略**：fill_first、round_robin、least_used、random
- **冷却状态**：STATUS_OK → STATUS_EXHAUSTED(冷却) → STATUS_DEAD(永久失败)
- **多进程token同步**：从共享存储同步token以处理跨进程OAuth刷新

## 五、斜杠命令系统 (commands.py)

`COMMAND_REGISTRY`：约60个 `CommandDef` 数据类，是CLI和网关中所有斜杠命令的**唯一真理来源**：

```python
@dataclass(frozen=True)
class CommandDef:
    name: str                    # "background", "model", "yolo"
    description: str             # 人类可读
    category: str               # "Session", "Configuration", "Tools & Skills", "Info", "Exit"
    aliases: tuple[str, ...]    # ("bg", "btw") 用于 /background
    args_hint: str              # "<prompt>" 或 "[on|off|status]"
    subcommands: tuple[str, ...] # 可制表符补全的子命令
    cli_only: bool              # 仅交互式CLI可用
    gateway_only: bool          # 仅消息网关可用
```

派生结构自动构建：`_COMMAND_LOOKUP`、`COMMANDS`、`COMMANDS_BY_CATEGORY`、`SUBCOMMANDS`、`GATEWAY_KNOWN_COMMANDS`

`should_bypass_active_session()`：运行时所有已解析斜杠命令都绕过会话队列。

## 六、诊断系统 (doctor.py)

`run_doctor()` 执行系统范围诊断，组织为多个检查部分：
1. 安全公告检查（已知损坏包版本）
2. Python环境（版本、虚拟环境检测）
3. 所需包检查
4. 配置文件验证（`.env`、`config.yaml`）
5. 系统级依赖（git、ssh、docker、node、ffmpeg等）
6. API连接性探测（每个提供商专用探测函数）
7. 技能发现（已安装/可选/禁用计数）
8. 工具可用性（已加载/不可用工具集）

`--fix` 标志自动创建缺失文件，`--ack <id>` 静默已知安全公告。

## 七、Termux快速路径

`_try_termux_fast_tui_launch()` 和 `_try_termux_fast_cli_launch()`：
- 在Termux/Android上跳过耗时的导入以加快启动
- `--version` 在 ~200ms 内完成（而非 ~2s）

## 八、进程标题

`_set_process_title()` 三级策略：
1. `setproctitle`（opt-in dep）
2. ctypes `prctl(PR_SET_NAME)`（Linux，15字符限制）
3. ctypes `pthread_setname_np`（macOS）
4. Windows no-op
