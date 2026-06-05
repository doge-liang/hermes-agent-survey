# Hermes Agent CLI与配置系统架构深度分析

> **一句话**:这份报告讲的是"用户从命令行敲下 `hermes` 之后,到真正开始一段对话之间,这套 CLI 框架替你做了哪些事"——它怎么把命令路由到对应功能、怎么把分散在四五个地方的配置和密钥合并成一份最终生效的设置、怎么在没有图形界面的终端里画出可交互的菜单、怎么管理多家模型提供商的认证、以及一堆为了让首次启动更快、进程更易识别而做的工程取舍。
>
> **读者画像**:你懂推理服务 / KV cache / LLM 工程,但完全没读过 Hermes 源码。下文每出现一个 Hermes 专有概念,都会先用一句话解释"它是什么"再展开。文中所有 `file:line` 坐标都配了"这行在干什么"的说明。
>
> **它在整套调研里的位置**:报告 12–17 关注的是"Hermes 给推理后端发了什么 agent hint"(agent hint = agent 随推理请求下发、辅助后端做 KV/调度决策的元数据)。本报告则退一层,讲"这些请求背后的 CLI 与配置骨架长什么样"。其中两个点和后续报告强相关,会在正文里点出交叉引用:一是配置里的 `toolsets`(工具集)如何影响最终发出去的工具数量(详见报告 15 §1 与报告 17 §2.1 的勘误);二是 `run_agent.py` 的真实位置(在仓库根,详见报告 17 §2.1)。

---

## 导言:为什么需要单独看 CLI 与配置层

一个 LLM agent 真正"聪明"的部分在对话循环、工具调用、上下文压缩里;但在那之前,有一整套"脚手架"决定了 agent 启动时拿到的是什么样的初始状态——用哪个模型、连哪个提供商、带哪些工具、读到哪份配置、用谁的密钥。这一层就是本报告的主题。

理解它的价值在于:**后面报告里"真实 CLI 跑出来的请求比 import-driver 丰富得多"这个反复出现的结论,根因就在这一层**。import-driver(在 Python 里直接 `new` 一个 `AIAgent` 并调 `run_conversation()`)是从对话循环的"半路"切进去的,绕过了本报告描述的全部脚手架;而真实 CLI(子进程跑 `hermes chat`)会完整走一遍配置解析、密钥加载、工具组装、提供商解析,所以它发出的请求才是生产里后端真正会收到的样子。换句话说,本报告解释的就是"那段被 import-driver 跳过的前半程"。

---

## 一、入口点与命令路由

### 1.1 双入口设计:一个老的交互式客户端,一个新的统一命令行

Hermes 有两个并存的程序入口,分工不同:

- **`cli.py`(15,783 行)** —— 一个基于 `prompt_toolkit` 的交互式 REPL 客户端。"REPL"是 Read-Eval-Print-Loop 的缩写,指"读一行输入→处理→打印结果→回到等待输入"的交互循环;`prompt_toolkit` 是 Python 里做这种带行编辑、补全、历史的终端交互的库。这个文件实现的是传统的 TUI(Text User Interface,纯文本终端界面)聊天循环——你坐在终端前一句一句和模型对话的那个画面。它体量巨大(近 1.6 万行),是早期的主力客户端。

- **`hermes_cli/main.py`** —— 新的、统一的 `hermes` 命令行入口。它用 `argparse`(Python 标准库里解析命令行参数的工具)把 `hermes <子命令> ...` 这种形式路由到各个功能模块。也就是说,你在终端敲 `hermes chat`、`hermes config view`、`hermes doctor` 时,是 `main.py` 在做"这是哪个子命令、该交给谁处理"的分发。

两者的关系是:`main.py` 是统一门面,`chat` 子命令(交互式聊天)最终仍会落到 `cli.py` 那套 REPL 逻辑上;而其余几十个子命令(配置、诊断、认证、网关……)各有自己的处理函数。

### 1.2 完整子命令树:`hermes` 能做的所有事

下面这棵树列出了 `hermes` 顶层支持的全部子命令。把它当成"这个 CLI 的功能地图"来看——后面各章会展开其中和配置 / 认证 / 诊断相关的几个;这里先建立全貌。

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

几个对后续报告有用的提示:`chat` 是默认子命令(直接敲 `hermes` 不带参数就进它),报告 15 的真实 CLI 测试台正是用 `hermes chat -q "..." --provider ...` 来驱动 Hermes 的;`fallback`(回退提供商链)和 `auth`(凭证池)对应第四章会讲的多提供商认证;`gateway`(消息网关)是把同一套 agent 接到 Telegram / Discord 等平台的服务,报告 17 的 I17 信号(gateway 的 session_key + config_signature)就来自这里。`acp` 里的 ACP 是 Agent Client Protocol,一种让编辑器(如 IDE)把 Hermes 当后端 agent 调用的集成协议。

---

## 二、配置系统:四层来源如何合并成"最终生效的一份"

LLM 工具最常见的运维痛点之一就是"我明明改了配置,为什么没生效"——通常是因为有多个配置来源在打架。Hermes 把这件事显式分层,规则清晰。理解这套分层,就能回答"某个设置到底从哪来、谁覆盖谁"。

### 2.1 配置分层:普通设置与密钥走两套不同的优先级

Hermes 把"普通配置项"(模型、工具集、压缩开关等)和"密钥 / 凭证"(API key 等)分成两套来源链来管理,因为它们的安全要求不同。

**普通配置的优先级,从高到低:**

1. **环境变量** —— 运行时最高优先级。临时 export 一个变量就能压过文件里的设置,适合一次性覆盖。
2. **`~/.hermes/config.yaml`** —— 用户配置文件,日常持久化设置都写这里。`~/.hermes/` 是 Hermes 在用户家目录下的工作目录。
3. **`DEFAULT_CONFIG`** —— 硬编码的后备默认值,内置在 `hermes_cli/config.py`(源码里 `DEFAULT_CONFIG` 这个大字典定义在 `config.py:802`)。你的 `config.yaml` 没写到的项,统统从这里取默认值。

这条链的含义是:**最终生效的配置 = 默认值打底,被用户文件覆盖,再被环境变量覆盖**。

**密钥 / 凭证的来源,按加载顺序:**

1. **`~/.hermes/.env`** —— 用户密钥文件。它带 `override=True` 加载(见 §2.3),意思是它会**覆盖掉 shell 里可能残留的、陈旧的同名 export**——这是为了避免"你以为在用新 key,实际进程里还是几个月前 export 的旧 key"这种坑。
2. **项目 `.env`** —— 仅作开发后备,优先级低,带 `override=False`(不覆盖已存在的值)。
3. **Bitwarden 密钥管理器(BSM)** —— 可选的外部密钥源,适合团队 / 企业把密钥集中存在密码管理器里而不落地到磁盘。
4. **进程环境变量** —— 进程启动时本就带的环境。

### 2.2 config.yaml 的加载流程:带缓存的"解析一次,重复复用"

负责读 `config.yaml` 的核心函数是 `load_config()`(源码 `config.py:4993`)。它的流程为了避免"每次取配置都重新读盘 + 重新解析 YAML"而带了一层内存缓存:

1. **`ensure_hermes_home()`**(`config.py:749`)—— 先确保 `~/.hermes/` 目录存在(没有就创建)。这是后续一切的前提。
2. **检查内存缓存** —— 缓存的键是 `(path, mtime_ns, size)` 三元组,即"文件路径 + 纳秒级修改时间 + 文件大小"。用这三个值当指纹,意味着只要文件没被改过,就认定缓存仍然有效;一旦文件被编辑(mtime 或 size 变了),指纹对不上,缓存自动失效。
3. **缓存命中** —— 返回一份 `deepcopy`(深拷贝)。为什么要深拷贝?因为配置是个嵌套字典,如果直接把缓存对象交出去,调用方一改它就污染了全局缓存;深拷贝保证每个调用方拿到的是独立副本。
4. **缓存未命中** —— 走完整解析链:解析 YAML → 用 `_deep_merge`(`config.py:4743`,把用户配置逐层合并进默认配置的函数)与 `DEFAULT_CONFIG` 合并 → 规范化键名 → 用 `_expand_env_vars`(`config.py:4763`)把配置值里写的 `${ENV_VAR}` 占位符展开成真实环境变量值 → 把结果存入缓存。

这里还有一个热路径优化函数 **`load_config_readonly()`**(`config.py:5010`):它和 `load_config()` 几乎一样,但**跳过第 3 步的 deepcopy**,直接把缓存对象返回。代价是调用方必须保证只读、不修改;收益是每次调用省下约 135 微秒。对于那些一秒钟要取很多次配置的热点路径,这点省下来很可观。代码里能看到当缓存确实需要重建时,它会从 `copy.deepcopy(DEFAULT_CONFIG)`(`config.py:5049`)起步。

写配置则走 **`save_config()`**(`config.py:5167`),它的步骤强调安全与原子性:获取文件锁(防并发写)→ 检查是否是"托管部署"(某些受管环境不允许随意改配置)→ 保留 env 模板 → **原子 YAML 写入**。"原子写入"的做法是:先写到一个临时文件,`fsync`(强制刷盘,确保数据真落到磁盘而非停在缓存),再 `rename`(重命名)到目标路径。因为同一文件系统内的 rename 是原子操作,这样就保证了"要么看到旧文件,要么看到完整的新文件",绝不会出现"写到一半断电留下半个损坏文件"的情况。

### 2.3 .env 加载(env_loader.py):把密钥安全地灌进进程

密钥加载由 `load_hermes_dotenv()`(`hermes_cli/env_loader.py:212`)负责。它做的事比"读个文件"复杂得多,每一步都对应一个真实会咬人的坑:

1. **修复损坏的 .env 文件** —— 实践中 `.env` 经常因为编辑器或脚本出错把多行串联成一行,这一步先把这种"串联行"拆回正常格式,否则后面解析会出错。
2. **加载 `~/.hermes/.env`,`override=True`** —— 用户密钥,覆盖式加载(理由见 §2.1:压掉 shell 里的陈旧 export)。
3. **加载项目 `.env`,`override=False`** —— 开发后备,不覆盖已有值。
4. **净化凭证类环境变量里的非 ASCII 字符** —— 这一步很关键且不直观:API key 最终是要放进 HTTP 请求头(header)里的,而 HTTP 头按规范必须是纯 ASCII。如果 key 里混进了不可见的非 ASCII 字符(复制粘贴时很容易带入),请求会在底层报错;所以这里提前把它们净化掉。
5. **应用外部密钥源(Bitwarden Secrets Manager)** —— 如果启用了 BSM,从它那里拉取密钥。
6. **同进程内幂等** —— 每个进程最多从 BSM 拉取一次。"幂等"指重复调用结果不变;这里具体是避免一个进程反复去打 BSM,既慢又可能触发限流。

**安全约束(这两条是硬性的):**

- `.env` 文件被强制设为 `0o600` 权限——即"仅文件所有者可读写,组和其他用户完全没权限"。密钥文件如果对同机其他用户可读就是泄露隐患,所以这里强制收紧。
- **拒绝写入"已知拒绝列表"里的键**,典型如 `LD_PRELOAD`、`PYTHONPATH`、`PATH` 等。原因是这些环境变量能改变程序加载哪些动态库 / 从哪里 import 模块 / 执行哪个二进制——如果一个被篡改的 `.env` 能往进程里塞 `LD_PRELOAD`,等于获得了任意代码执行的入口。把它们拉黑,是防止"通过配置文件做提权 / 注入"的防线。

---

## 三、Curses TUI 渲染器(curses_ui.py):在纯文本终端里画交互菜单

很多场景下用户需要"从一个列表里选一项 / 勾几项"——选提供商、选模型、勾工具集。在没有图形界面的终端里,这要靠 `curses` 来实现。`curses` 是 Unix 上控制终端光标、颜色、按键的底层库;它能把整个终端屏幕当画布来画可交互的界面。Hermes 把这套封装在 `hermes_cli/curses_ui.py` 里。

核心是一个通用的菜单事件循环 `_run_curses_menu()`(`curses_ui.py:350`)——它处理按键、移动高亮、过滤、确认 / 取消这些通用逻辑;在它之上暴露三个公共 API,对应三种选择语义:

| API | 类型 | 用途 |
|-----|------|------|
| `curses_radiolist()` | 单选 | 从一组里选恰好一项(如选提供商) |
| `curses_checklist()` | 多选 | 勾选 / 取消多项(如选工具集) |
| `curses_single_select()` | 带取消的单选 | 选一项,或返回 `None`(用户取消) |

对应源码:`curses_checklist`(`curses_ui.py:531`)、`curses_radiolist`(`curses_ui.py:622`)、`curses_single_select`(`curses_ui.py:742`)。三者共享同一个底层循环,只是在"能选几个、能不能取消"上做约束——这就是为什么它们行为一致、维护成本低。

**几个值得点名的功能特性:**

- **Vim 风格导航(j/k)+ 方向键** —— 同时支持两套移动方式,照顾不同习惯的用户。
- **搜索 / 过滤** —— 按 `/` 键打开内联搜索框,用**模糊匹配**(fuzzy match,即输入 `op4` 也能命中 `claude-opus-4` 这种非连续子串的匹配)。这套模糊匹配算法是从 TypeScript 版的 `fuzzyScore` 移植过来的(源码注释 `curses_ui.py:62-63` 明确写了它"忠实移植自 ui-tui 和 web 两处的 `fuzzy.ts`",实现函数为 `_fuzzy_score@curses_ui.py:113`)。**这样做的因果是**:Hermes 有三个界面(终端 TUI、Web、这套 curses),如果各自用不同的排序算法,同一个搜索词在三处给出的候选顺序会不一样,体验割裂;统一移植同一份算法,保证三处对模型 id 的排序完全一致。
- **回退模式** —— 当 `curses` 在某些"真 TTY 但环境不规范"的终端上初始化失败时,退化成"打印带编号的列表,让你输数字选"的朴素模式,保证功能不至于完全不可用。
- **输入刷新** —— `curses.wrapper()`(curses 的标准入口包装器)返回之后,主动排空操作系统输入缓冲里残留的、陈旧的转义序列。否则用户在菜单里按的方向键转义码可能"漏"到菜单退出之后,污染下一段输入。

---

## 四、认证系统:统一管理 34 家提供商的多种登录方式

LLM 工具要面对一个现实:每家模型提供商的认证方式都不一样——有的给 API key,有的走 OAuth,有的要走 AWS 的凭证链。Hermes 把这些差异收敛进一张注册表,再用统一的解析逻辑选出"这次该用谁"。

### 4.1 提供商注册表(auth.py):一张表描述所有提供商怎么认证

`PROVIDER_REGISTRY`(定义于 `hermes_cli/auth.py:167`)是一个字典,登记了 **34 个提供商**的配置。每个提供商标注它用哪种认证类型,共 **4 种类型**(源码 `auth.py:155` 的字段注释和各条目的 `auth_type` 取值可见):

- **`oauth_device_code`** —— "设备授权流程"。典型是 Nous Portal。设备码流程指的是:你在终端发起登录,它给你一个码,让你在浏览器里输码授权——适合没法直接在程序里弹浏览器的环境(`auth.py:171`)。
- **`oauth_external`** —— 借用外部已有的 OAuth 凭证。用于 OpenAI Codex、xAI Grok、Qwen、Google Gemini CLI 等(`auth.py:180/194/200/206`)。
- **`oauth_minimax`** —— MiniMax 自家的定制 OAuth 流程,因为它不完全套用标准流程,单列一类(`auth.py:301`)。
- **`api_key`** —— 最朴素的"贴一个 API key"。用于 OpenAI、Anthropic、Gemini、DeepSeek 等。
- **`aws_sdk`** —— 走 AWS Bedrock 的凭证链(`auth.py:429`),即复用 AWS SDK 那套"从环境变量 / 配置文件 / IAM 角色逐级找凭证"的机制。

(说明:源码里 `auth.py:155` 那行注释只罗列了前三种 + `api_key`,但 `aws_sdk` 作为第五种 `auth_type` 在 `auth.py:429` 确有其条目;因此实际是 4 种 OAuth/key 类 + Bedrock 这一类,共同覆盖上面列举的所有提供商。)

**插件系统会自动扩展这张表**:位于 `plugins/model-providers/<name>/` 目录下的提供商会被自动合并进 `PROVIDER_REGISTRY`(合并逻辑在 `auth.py:444` 起,会检查 `if _pp.name in PROVIDER_REGISTRY` 避免重复)。这意味着第三方 / 自研提供商不必改核心代码,丢个插件目录就能接进来——这一点对"自研后端冒充某个 provider"的玩法(详见报告 17 §4 的 host 门控重评估)是有用的基础设施。

### 4.2 提供商解析:"用哪个 provider"是怎么定下来的

当你不指定提供商(`requested="auto"`)时,`resolve_provider()`(`auth.py:1464`)按下面这条优先级链从上往下找,命中即停:

1. **`auth.json` 里活跃的 OAuth 提供商** —— 你已经登录过的那个,优先用。
2. **CLI 显式传了 api_key** → 解析为 openrouter。
3. **环境变量 `OPENAI_API_KEY` 或 `OPENROUTER_API_KEY` 存在** → openrouter。
4. **按各提供商逐个检查它对应的 API key 环境变量**(谁有 key 用谁)。
5. **AWS Bedrock 凭证链**(`auth.py:5733` 处对 `auth_type == "aws_sdk"` 的处理印证了这条作为后段兜底)。
6. **最终兜底:openrouter**。

这条链解释了一个常见现象:用户没显式指定、又恰好 export 了 `OPENAI_API_KEY`,结果请求走了 openrouter——因为第 3 步就命中了。

### 4.3 凭证池系统(auth_commands.py):一个提供商配多把 key 的轮换与冷却

当你对同一个提供商有多把 key(为了提高额度 / 绕限流),Hermes 提供"凭证池"把它们按提供商分组管理(逻辑在 `auth_commands.py`)。三个机制:

- **选择策略** —— 决定每次从池里挑哪把 key:`fill_first`(用满第一把再用下一把)、`round_robin`(轮流)、`least_used`(挑用得最少的)、`random`(随机)。不同策略适配不同的限流模型。
- **冷却状态机** —— 每把 key 有三种状态:`STATUS_OK`(可用)→ `STATUS_EXHAUSTED`(用尽,进入冷却期,过段时间会恢复)→ `STATUS_DEAD`(永久失效,比如 key 被吊销)。这套状态机让"撞到限流"不至于直接全盘失败,而是把该 key 冷却一会儿、换别的继续。
- **多进程 token 同步** —— 多个 Hermes 进程可能同时在跑;当其中一个刷新了 OAuth token,其它进程需要看到最新的 token。这里通过共享存储同步,避免"A 进程刷新了 token,B 进程还在用过期的"。

---

## 五、斜杠命令系统(commands.py):CLI 与网关共享的唯一真理来源

在交互式聊天里,用户会敲 `/model`、`/background` 这类"斜杠命令"来改设置或触发动作。Hermes 把所有斜杠命令的定义集中在 `COMMAND_REGISTRY`(`hermes_cli/commands.py`)里——大约 **60 个** `CommandDef` 数据类。"唯一真理来源"(single source of truth)的意思是:CLI 客户端和消息网关都从这同一份注册表读命令定义,不各自维护一份,从而保证两个界面里"有哪些命令、叫什么、怎么补全"完全一致。

`CommandDef` 是一个冻结的(`frozen=True`,即创建后不可改)数据类(`commands.py:46`),每个字段的含义如下:

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

逐字段解释:`name` 是命令主名;`description` 是给人看的说明;`category` 把命令归类(会话 / 配置 / 工具与技能 / 信息 / 退出),用于在帮助里分组展示;`aliases` 是别名(比如 `/bg`、`/btw` 都映射到 `/background`);`args_hint` 是参数提示文本,提醒用户该命令接什么参数;`subcommands` 是可用 Tab 补全的子命令列表;`cli_only` / `gateway_only` 这对布尔标志限定命令的可用范围——有些命令只在交互式 CLI 有意义,有些只在网关里有意义。

注册表一旦定义好,一批**派生结构会被自动构建出来**,这样运行时查询无需重复计算:

- `_COMMAND_LOOKUP`(`commands.py:241`)—— 名字 / 别名到 `CommandDef` 的快速查表,`lookup` 时会 `lstrip("/")` 把前导斜杠去掉再查(`commands.py:249`)。
- `COMMANDS_BY_CATEGORY`(`commands.py:268`)—— 按 category 分好组的命令,直接用于分组展示。
- `SUBCOMMANDS`(`commands.py:278`)—— 每个命令的可补全子命令;它还会从 `args_hint` 里解析出 `on|off|status` 这种"竖线分隔的可选值"补进来(`commands.py:294`)。
- `GATEWAY_KNOWN_COMMANDS`(`commands.py:304`)—— 网关认识的命令集合(`frozenset`),用于网关侧快速判断"这条斜杠命令我处不处理"。
- 此外还有汇总用的 `COMMANDS`。

最后一个关键函数 `should_bypass_active_session()`:它表示**运行时所有已解析的斜杠命令都绕过会话队列**。含义是——当一段对话正在进行(agent 可能正在忙)时,用户敲的斜杠命令(比如 `/model` 切模型)不该排在普通消息后面慢慢等,而要立即生效;"绕过会话队列"就是给斜杠命令开的这条快速通道。

---

## 六、诊断系统(doctor.py):一条命令体检整个环境

`hermes doctor` 解决的是"装好了但跑不起来 / 跑得不对,到底哪儿出了问题"。核心是 `run_doctor()`(`doctor.py:448`),它把一次体检拆成多个检查小节,逐项跑并汇报:

1. **安全公告检查** —— 比对当前装的依赖里有没有"已知有问题的损坏包版本"。
2. **Python 环境** —— Python 版本是否满足,是否在虚拟环境里(virtualenv 检测)。
3. **所需包检查** —— 必需的依赖是否都装齐。
4. **配置文件验证** —— 检查 `.env`、`config.yaml` 是否存在、格式是否正确。
5. **系统级依赖** —— `git`、`ssh`、`docker`、`node`、`ffmpeg` 等外部命令行工具是否可用(很多 Hermes 功能依赖这些外部程序)。
6. **API 连接性探测** —— 对每个提供商跑一个专用探测函数,确认 key 有效、网络通。
7. **技能发现** —— 统计技能(skill)的已安装 / 可选 / 禁用数量。
8. **工具可用性** —— 报告哪些工具集已加载、哪些不可用。

两个有用的开关:`--fix` 会**自动创建缺失的文件**来修常见问题(源码里多处会提示用户 "Run 'hermes doctor --fix'",如 `doctor.py:850/890`);`--ack <id>` 把某条已知安全公告**静默掉**(acknowledge,即"我知道这条了,别再提醒"),其快速处理路径在 `doctor.py:457` 起,会把这个 ack 持久化,使用示例见 `doctor.py:526`。

---

## 七、Termux 快速路径:让安卓上的启动从 ~2s 降到 ~200ms

Termux 是 Android 上的一个终端模拟器 / Linux 环境,很多人用它在手机上跑 Hermes。但手机上的 Python 导入(import)比桌面慢得多,完整加载 Hermes 的所有模块要好几秒,对"我只是想看一下版本号"这种轻量操作来说太亏。

为此有两个快速路径函数(都在 `hermes_cli/main.py`):`_try_termux_fast_cli_launch()`(`main.py:12240`)和 `_try_termux_fast_tui_launch()`(`main.py:12313`)。它们的思路是:**在 Termux/Android 上,对那些不需要完整功能的轻量命令,跳过耗时的重型导入**,直接走一条精简路径返回结果。启动时会先尝试这两条快路径(`main.py:12375` 和 `:12377`),命中就短路返回。

实测效果:`hermes --version` 能在约 **200ms** 内完成,而走完整导入要约 **2s**——对高频的轻量操作,这是 10 倍量级的体验差异。

---

## 八、进程标题:让 `ps` 里能认出 Hermes 进程

当系统里跑着一堆 Python 进程时,在 `ps` / 任务管理器里它们往往都显示成 `python`,根本分不清谁是谁。`_set_process_title()`(`main.py:68`)把当前进程的标题改成可识别的名字(如 `hermes`),方便运维定位。它按平台用一条三级回退策略,逐级尝试,谁可用用谁:

1. **`setproctitle`** —— 一个专门改进程标题的第三方库。它是 opt-in 依赖(可选安装),装了就优先用,效果最好、限制最少。
2. **ctypes 调 `prctl(PR_SET_NAME)`** —— Linux 路径(`main.py:99` 处 `libc.prctl(15, b"hermes", ...)`,注释说明 `PR_SET_NAME = 15`)。`prctl` 是 Linux 的进程控制系统调用;这条路有个硬限制:**进程名最多 15 个字符**(内核层面的限制)。
3. **ctypes 调 `pthread_setname_np`** —— macOS 路径(`main.py:102`),通过给线程命名来影响显示。
4. **Windows** —— no-op(空操作),即在 Windows 上这套机制不适用,直接什么都不做、安全跳过。

"ctypes"指 Python 直接调 C 库函数的方式;这里用它在没有 `setproctitle` 第三方库时,也能靠操作系统原生的系统调用把标题设上,保证基本能力不依赖可选依赖。

---

## 附:与后续报告的衔接

本报告描述的脚手架,正是后续 agent-hint 报告里"真实 CLU 路径"的前半程基础:

- **配置 → 工具组装**:这里讲的 `config.yaml` 里 `toolsets` 字段,经过 CLI 这一层转换后会带上核心工具集,最终出现在出站请求里——但要注意"配置层的空列表"和"内部参数的空列表"是两个不同的层,不能混为一谈(完整因果详见报告 15 §1 与报告 17 §2.1 的勘误;那里实测真实 CLI 在 `toolsets: []` 配置下出站仍带 29 个工具,而这个 29 是环境 / 配置相关的捕获值,不是常量)。
- **提供商解析 → host 门控**:本报告 §4 的提供商注册与解析,决定了请求最终发往哪个 `base_url`;而 Hermes 是否注入某些 agent hint,又取决于这个 base_url 的主机名是否命中特定域名(host 门控机制,详见报告 15 §4.3 与报告 17 §4)。
- **`run_agent.py` 的位置**:对话循环主体在仓库根的 `run_agent.py`(不是 `agent/run_agent.py`),这一点在报告 17 §2.1 已勘误;本报告涉及的入口分发(`main.py`)最终会路由到那里。
