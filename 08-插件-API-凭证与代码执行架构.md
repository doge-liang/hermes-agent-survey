# Hermes Agent 插件、API、凭证与代码执行架构

> **一句话**:这份报告把 Hermes 的「外围与执行层」拆开讲清楚——它怎么对外暴露一套 REST API(让别的程序像调 OpenAI 那样调它)、怎么用插件机制让第三方扩展能力、怎么管理多把 API 凭证并在限流时自动故障转移、以及它怎么安全地让 LLM「写一段 Python 脚本去批量调用工具」和「派生子 agent 并行干活」。这些都属于「Hermes 作为一个 agent 框架,在真正发请求给推理后端之前 / 之外做的工程支撑」。
>
> **读者画像**:本报告假设你懂推理服务、KV cache、LLM 工程,但完全不了解 Hermes 内部。下文每个 Hermes 专有概念第一次出现时都会先用一句话解释再使用;裸 `file:line` 坐标都会配上「这一行在做什么」。
>
> **与其他报告的关系**:本报告偏「静态架构」——读源码归纳出有哪些组件、各自职责。真正的「线上发了什么请求」实测在报告 12–15;把这些信号映射到 Dynamo 风格 KV/调度的设计在报告 16;每个信号的逐条提取配方在报告 17。本报告涉及的源码坐标均来自源码树(`/mnt/d/Workspace/project/hermes-agent`);如需验证「真实安装下的行为」,仍须以用户实际安装的 `~/.hermes` 为准(参见全仓记忆「两个安装版本差异」)。

---

## 0. 全局导览:这五大块各管什么

在钻进细节之前,先建立一张地图。Hermes 不只是「一个会聊天的 CLI」,它对外、对内都有一层基础设施:

| 模块 | 一句话职责 | 对应章节 |
|------|-----------|----------|
| REST API 服务器 | 把 Hermes 包装成一个 HTTP 服务,让外部程序用 OpenAI 兼容协议调用它,并管理持久化会话与异步 run | §1 |
| 插件系统 | 第三方代码在不改主干的前提下挂载工具、钩子、命令、平台适配器 | §2 |
| 凭证管理 | 同一个 provider 配多把 key,限流时自动切换;区分「自己拥有」与「借用引用」的密钥 | §3 |
| 代码执行工具 | 让 LLM 写 Python 脚本、通过 RPC 回调 Hermes 的工具,把多步工具链压成一次推理 | §4 |
| 子代理委托 | 派生隔离的子 agent 去做子任务,支持并行与有限层级的再委托 | §5 |
| 终端 / Shell Hooks | 在 6 种环境里跑命令,以及在特定事件触发用户自定义脚本 | §6、§7 |

这些模块互相之间是松耦合的:API 服务器调度 run,run 里跑的 agent 可能用代码执行工具或委托子 agent,这些动作又都受凭证管理和审批门控约束。下面逐块展开。

---

## 一、REST API 服务器(`gateway/platforms/api_server.py`)

### 1.1 它解决什么问题

Hermes 本身是个命令行 agent。但很多场景需要「让另一个程序(Web 前端、第三方 UI、自动化脚本)来驱动 Hermes」。最省事的办法,是让 Hermes 装成一个大家都已经会调的接口——**OpenAI 兼容 API**。这样现成的 OpenAI SDK、LangChain 之类的客户端几乎不改代码就能把后端换成 Hermes。`api_server.py` 就是这层 HTTP 门面。

它对外暴露三组端点:OpenAI 兼容端点、Session 管理端点、Run 管理端点。下面分别解释。

### 1.2 端点体系

**(A)OpenAI 兼容端点**——给「已经在用 OpenAI」的客户端无缝替换:

- `POST /v1/chat/completions` —— 标准的 Chat Completions 格式(一问一答,本身无状态)。Hermes 在此之上叠了一个**可选的会话连续性**:客户端通过 `X-Hermes-Session-Id` 头部带上一个会话 ID,Hermes 就能把多次独立请求串成一个有记忆的会话。
- `POST /v1/responses` —— OpenAI 的 Responses API 格式(有状态)。它靠 `previous_response_id` 把这一次请求接到上一次的响应上,从而重建历史。
- `GET /v1/responses/{response_id}` / `DELETE /v1/responses/{response_id}` —— 读取 / 删除某条已存储的响应。
- `GET /v1/models` —— 返回模型列表(Hermes 把自己作为一个可用模型登记进去)。
- `GET /v1/capabilities` —— 机器可读的能力描述,给外部 UI 探测「这个后端支持哪些特性」。

**(B)Session 管理端点**——这是 Hermes 自有的、比 OpenAI 更丰富的一套持久化会话接口。「Session」在 Hermes 里指一段带历史的对话,落在本地 SessionDB(SQLite)里:

- `GET /api/sessions` —— 列出客户端可见的会话。
- `POST /api/sessions` —— 创建一个空会话。
- `GET/PATCH/DELETE /api/sessions/{session_id}` —— 读取、更新、删除某个会话。
- `GET /api/sessions/{session_id}/messages` —— 读取该会话的消息历史。
- `POST /api/sessions/{session_id}/fork` —— 基于 SessionDB 的血缘(lineage)分支出一个新会话(从某个点叉开,各走各的)。
- `POST /api/sessions/{session_id}/chat[/stream]` —— 在一个已持久化的会话上继续对话(`/stream` 后缀走流式)。

**(C)Run 管理端点(异步执行)**——当一次 agent 执行可能很久(多轮工具调用),同步等待并不现实。Run 把「执行」做成一个可轮询、可订阅事件、可中途审批 / 中断的异步对象:

- `POST /v1/runs` —— 启动一个 run,**立刻返回**(HTTP 202 + run_id),不阻塞等结果。
- `GET /v1/runs/{run_id}` —— 查询当前 run 状态。
- `GET /v1/runs/{run_id}/events` —— 以 SSE(Server-Sent Events,服务器推送流)订阅结构化的生命周期事件,包括工具进度。
- `POST /v1/runs/{run_id}/approval` —— 处理一个挂起的审批(某些危险操作需要人工放行)。
- `POST /v1/runs/{run_id}/stop` —— 中断一个正在运行的 agent。

### 1.3 几个核心设计点(配上「为什么这么做」)

- **`ResponseStore`(`api_server.py:342`)**:一个 SQLite 支持的 LRU(最近最少使用淘汰)存储。Responses API 的「有状态」靠的就是它——`previous_response_id` 指过来时,Hermes 从这里把历史调出来重建上下文。用 SQLite 是为了进程重启后历史还在;用 LRU 是为了不无限膨胀。
- **`_IdempotencyCache`(`api_server.py:584`)**:基于幂等键的 TTL(存活时间)+ LRU 存储。它保证「同一个请求重发不会被执行两遍」——客户端因网络抖动重试时,Hermes 认出这是同一个键,直接返回上次结果。`_idem_cache` 在 `:629` 实例化为全局单例。
- **认证**:通过 `X-Hermes-Session-Key` 头部携带 API key 做 Bearer 认证(`:885` 起的 `_extract...session_key` 逻辑;`:901` 读取该头部)。值得注意的是,这把「认证用的 key」(`X-Hermes-Session-Key`)和「标识会话用的 id」(`X-Hermes-Session-Id`)是**两个独立的东西**:前者管「你有没有权限」,后者管「这是哪一段对话」。
- **Session 连续性的稳定 ID**:客户端可以自己给一个 session_id;如果不给,Hermes 会**从对话内容算一个 SHA-256 指纹**派生出稳定 ID(`:633` 的 `sha256(...)` 计算指纹,`:652` 取前 16 位作为种子)。这样「内容相同的对话」会落到同一个会话上,实现隐式连续性。
- **流式**:SSE 支持,事件流里夹带工具进度,前端可以实时显示「agent 正在调用某工具」。

---

## 二、插件系统

### 2.1 它解决什么问题

一个 agent 框架最怕「什么功能都往主干塞」。Hermes 的做法是给出一套插件契约:第三方(或官方的可选模块)把自己的工具、钩子、命令、平台适配器打包成一个插件目录,Hermes 在启动时发现并加载它们。主干只认契约,不认具体实现。

### 2.2 发现机制:四个来源,后来者覆盖先来者

Hermes 从四个位置找插件,**后面的来源会覆盖同名的前面来源**(这给了「用户 / 项目级定制压过内置默认」的能力):

1. **内置插件**:`<repo>/plugins/<name>/` —— 随 Hermes 一起发布的官方插件。
2. **用户插件**:`~/.hermes/plugins/<name>/` —— 当前用户全局安装的。
3. **项目插件**:`./.hermes/plugins/<name>/` —— 当前项目目录下的,优先级最高,适合「这个项目专属」的扩展。
4. **Pip 安装插件**:任何 Python 包只要暴露 `hermes_agent.plugins` 这个 entry-point 组(Python 打包里的「入口点」声明),就会被自动发现——不需要把文件放进上面三个目录。

### 2.3 插件种类:五类,加载策略各不相同

不同种类的插件,Hermes 对它们的「默认是否启用」处理不一样。下表是参考,表后逐行解释:

| 种类 | 加载策略 | 说明 |
|------|----------|------|
| `standalone` | `plugins.enabled` 选择加入 | 拥有自己的 hooks/tools |
| `backend` | 自动加载(内置);`plugins.enabled` 控制(用户) | 核心工具的可插拔后端 |
| `exclusive` | 独立发现系统 | 恰好一个活跃提供者(如 memory) |
| `platform` | 自动加载(内置);`plugins.enabled` 控制(用户) | 网关消息平台适配器 |
| `model-provider` | `providers/__init__.py` 管理 | 模型提供者配置文件 |

逐行解读:

- **`standalone`(独立插件)**:自带一套钩子和工具,默认**不**开,要在配置 `plugins.enabled` 里显式列出才加载。这是「选择加入(opt-in)」语义——避免装了就乱跑。
- **`backend`(后端插件)**:它是某个核心工具的「可替换实现」。比如核心工具是「搜索」,backend 插件给出具体用哪个搜索引擎。内置 backend 自动加载;用户提供的同样受 `plugins.enabled` 控制。
- **`exclusive`(排他插件)**:同一类职责**只能有一个活跃提供者**,典型是 memory(记忆)——不能同时有两套记忆后端在写。它走一套独立的发现系统来保证「恰好一个」。
- **`platform`(平台插件)**:网关侧的「消息平台适配器」,负责把 Telegram / Slack 这类外部平台的消息桥接进来。内置的自动加载,用户的受 `plugins.enabled` 控制。
- **`model-provider`(模型提供者插件)**:不是普通插件,而是一份「模型提供者配置文件」,由 `providers/__init__.py` 专门管理。

### 2.4 PluginContext API:插件能向 Hermes 注册什么

插件的入口是一个 `register(ctx)` 函数,Hermes 在加载时把一个**上下文对象 `ctx`** 传进来。插件通过 `ctx` 上的方法,把自己的能力挂到 Hermes 各个扩展点上:

- `ctx.register_tool(...)` —— 注册一个工具到全局工具注册表(让 LLM 能调用它)。
- `ctx.register_hook(hook_name, callback)` —— 把一个回调挂到 15 个生命周期钩子之一(见 §2.5)。
- `ctx.register_command(...)` —— 注册一个 slash 命令(用户在对话里敲 `/xxx` 触发)。
- `ctx.register_cli_command(...)` —— 注册一个 CLI 子命令(命令行 `hermes xxx`)。
- `ctx.register_platform(...)` —— 注册一个网关平台适配器。
- `ctx.register_auxiliary_task(...)` —— 注册一个 AUX LLM 任务(「辅助 LLM」指主对话之外、用来做摘要 / 分类等小任务的次级 LLM 调用)。
- `ctx.register_context_engine(...)` —— 注册一个上下文引擎(管理上下文如何组装 / 压缩;**只允许注册一个**,因为它是全局唯一职责)。
- `ctx.llm` —— 一个「受信任插件专用」的托管 LLM 访问门面。受信任的插件可以借它直接发起 LLM 调用,而不必自己管 key / 路由。

### 2.5 15 个生命周期钩子:在对话的哪些时刻插入逻辑

钩子(hook)= Hermes 在执行流程的某个固定时刻回调你的代码,让你能观察或改写当时的数据。Hermes 把整个 agent 生命周期切成若干时刻,插件可以挂在这些点上。按职责分组如下:

- **工具相关**:`pre_tool_call`(工具执行前)、`post_tool_call`(工具执行后)——可用于审计、改参、拦截。
- **LLM 调用相关**:`pre_llm_call`(发给 LLM 前)、`post_llm_call`(LLM 返回后)。
- **API 请求相关**:`pre_api_request`(发出底层 API 请求前)、`post_api_request`(收到响应后)——比 LLM 钩子更贴近 wire,适合改 header / body。
- **Session 相关**:`on_session_start`、`on_session_end`、`on_session_finalize`、`on_session_reset`——会话开始、结束、最终化、重置时各触发一次。
- **输出转换相关**:`transform_terminal_output`、`transform_tool_result`、`transform_llm_output`——分别改写「终端输出」「工具结果」「LLM 输出」(`transform_llm_output` 的实际触发点见 `conversation_loop.py:4588`、`:4596`,失败时 `:4608` 记录告警但不中断)。
- **其他**:`subagent_stop`(子 agent 停止)、`pre_gateway_dispatch`(网关分发前)、`pre_approval_request`(发出审批请求前)、`post_approval_response`(收到审批结果后)。

### 2.6 已安装插件

源码树 `plugins/` 下有 21 个插件目录(本报告记录的计数),涵盖:browser、context_engine、dashboard_auth、disk-cleanup、google_meet、hermes-achievements、image_gen、kanban、memory、model-providers、observability、platforms、security-guidance、spotify、teams_pipeline、video_gen、web 等。

> 说明:插件目录数量会随版本和具体安装而变,实际数量请以你部署的那棵树为准(源码树与用户实际安装的 `~/.hermes` 可能有差异,参见全仓记忆「两个安装版本差异」)。这里保留报告生成时的计数 21。

---

## 三、凭证管理系统

### 3.1 它解决什么问题

跑一个长时间的 agent,经常会撞到「单把 API key 被限流」的墙。Hermes 的凭证系统让你给同一个 provider 配**多把 key**,在某把被限流时自动切到下一把;同时它还要区分「这把 key 是我自己拥有的(可以落盘持久化)」还是「只是引用了别处的(不该把秘密值写到我的磁盘上)」。这套逻辑分三层。

### 3.2 三层架构

**第一层:凭证池(`credential_pool.py`)—— 多凭证故障转移**

凭证池把同一 provider 的多把 key 当成一个池子来调度。核心数据结构是 `PooledCredential`(`:129`),每个条目带这些字段:provider、id、label、`auth_type`(认证类型)、`priority`(优先级)、source(来源)、token、错误状态、速率限制冷却信息。

- **4 种选择策略**(决定「下一次该用哪把 key」):
  - `fill_first`(`STATUS`/`STRATEGY_FILL_FIRST`,`:95`)—— **默认**,把第一把用满了再用第二把(对「按 key 计费」友好)。
  - `round_robin`(`:96`)—— 轮流用,雨露均沾。
  - `random`(`:97`)—— 随机挑。
  - `least_used`(`:98`)—— 优先用「用得最少」的那把。
- **冷却 / 租约 / 死位状态机**:每把 key 有三种状态(`:55`–`:63`):
  - `STATUS_OK`(`"ok"`)—— 正常可用。
  - `STATUS_EXHAUSTED`(`"exhausted"`)—— 被限流,进入**冷却**;冷却时长按触发限流的 HTTP 状态码决定(`:249` 的 `_cooldown...` 逻辑),冷却到期后会恢复。
  - `STATUS_DEAD`(`"dead"`)—— 永久失败(比如 key 本身无效),即使等再久也不会好,直接踢出选择。
  这个状态机的意义:`OK → EXHAUSTED(冷却)→` 自动恢复,而真正坏掉的走 `→ DEAD` 不再浪费请求。
- **软租约并发控制**:`acquire_lease()` / `release_lease()` 给「同一把 key 同时被多少个并发任务用」加了个软上限。选择时**优先给租约最少的 key**(`:451` 按 priority 排序、`:455` 的 `_active_leases` 记录在用数),避免一把 key 被并发挤爆。
- **多进程 token 同步**:OAuth 类凭证可能在另一个进程里被刷新了。凭证池会从共享存储同步,确保跨进程的 token 刷新能被本进程看到,不会用着已经过期的旧 token。

**第二层:凭证来源(`credential_sources.py`)—— 统一移除契约**

「来源」指一把 key 是从哪冒出来的(环境变量、某个 OAuth 流程、GitHub Copilot 等)。这一层的关键设计是:每个来源都注册一个 `remove_fn` 回调(`:100`),负责「干净地移除这把 key」——它要做三件事:清理磁盘上的痕迹、**抑制重新种子(reseed)**(防止删掉后又被自动加回来,`:259` 注释解释了抑制 oauth 源的 reseed 路径)、返回诊断信息(告诉用户还需要手动 unset 哪些 shell 环境变量,`:35`)。这样「删一把 key」是个统一、可预期的操作,而不是每种来源各删各的、留一堆残渣。具体来源的 `remove_fn` 注册见 `:395`(Copilot)、`:401`(env)、`:406`(Claude Code)、`:411`(Hermes PKCE)、`:416`(Nous 设备码)等。

**第三层:凭证持久化(`credential_persistence.py`)—— 磁盘边界**

这一层管「哪些 key 可以写到磁盘,哪些不能」。核心是区分「拥有」和「借用」:

- `is_borrowed_credential_source()`(`:103`)—— 判断一个来源是「借用 / 仅引用」还是「拥有 / 可持久化」。默认策略是:任何带非空 source 的条目都按「借用 / 仅引用」对待(`:18` 注释)。
- `sanitize_borrowed_credential_payload()`(`:151`)—— 对「借用来源」的负载做清洗,**剥掉原始秘密值**(`:163` 判断 borrowed 后剥离),只留引用信息。这样 Hermes 不会把不属于自己的秘密落盘,避免泄露。

### 3.3 外部密钥源(`secret_sources/`):Bitwarden 集成

Hermes 支持在进程启动时从 **Bitwarden Secrets Manager**(一个密钥托管服务,通过 `bws` CLI 访问)拉取 API key,注入为环境变量。设计上有几个克制点(`secret_sources/__init__.py:5`、`bitwarden.py`):

- **非破坏性**:只设置「尚未存在」的环境变量——已经设了的不覆盖(`:5` 注释明确「only set values for env vars」未设的)。
- **失败不阻塞启动**:拉密钥失败了,Hermes 照常启动,不会因为 Bitwarden 不可达就起不来。
- **自举(bootstrap)只需一把 token**:`bws` 二进制首次使用时自动装到 `<hermes_home>/bin/bws`(版本钉死在 `_BWS_VERSION`,`bitwarden.py:59` = `"2.0.0"`),用户只需提供一把 `BWS_ACCESS_TOKEN` 作为引导密钥,其余所有 provider key 都可以放进 Bitwarden。

---

## 四、代码执行工具(`tools/code_execution_tool.py`)

### 4.1 程序化工具调用(PTC):为什么需要它

普通的工具调用是「LLM 说要调工具 A,框架调 A,把结果塞回去,LLM 再说调工具 B……」——每一步都要往返一次推理,多步链路非常费 token 和延迟。

**程序化工具调用(Programmatic Tool Calling,PTC)** 换了个思路:让 LLM **一次性写一段 Python 脚本**,脚本里通过 RPC(远程过程调用)去调 Hermes 的各个工具,把「调 A、拿结果、据此调 B、再调 C」这整条链折叠进**一次推理轮次**完成。LLM 写一次脚本,框架跑完整段,只把最终 stdout 还给 LLM。

### 4.2 两种传输方式

脚本要在子进程里跑,但脚本里调工具时又得「回到父进程」真正执行(父进程才有工具的真实实现和凭证)。这个「子进程→父进程」的回调通道有两种实现:

**(A)本地后端(UDS,Unix 域套接字)**:

1. 父进程生成一个 RPC 函数存根模块 `hermes_tools.py`(`:11`、`:259` 的 `generate_hermes_tools_module`)。脚本 `import hermes_tools` 后,调用里面的函数其实是发 RPC。
2. 父进程打开一个 Unix 域套接字(`chmod 0o600`,只有属主可读写,`:368` 用 `AF_UNIX`),启动一个 RPC 监听线程。
3. 在子进程里运行 LLM 写的脚本。
4. 脚本里的工具调用通过 UDS 回传父进程,由父进程分派给真实工具执行,再把结果传回。
5. **Windows 后备**:`AF_UNIX` 在 Windows Python 上不可靠,所以 Windows 上回退到**回环 TCP**(loopback,仅绑定本机,`:51`–`:52` 注释、`:362` 仅本机绑定)。

**(B)远程后端(文件 RPC)**:当脚本要在一台远程机器上跑、没法直接共享套接字时,改用文件来传:

1. 把存根 + 脚本通过 base64 编码后用 `echo '...' | base64 -d > file` 的方式船运到远程(`:697`–`:705`;注释 `:697` 解释为何用 `echo | base64 -d` 而非 stdin 管道——某些环境 stdin 不可靠)。
2. 父侧一个轮询线程读「请求文件」→ 分派 → 写「响应文件」。
3. 远程脚本轮询响应文件,拿到结果后继续。

### 4.3 沙箱:让 LLM 写的脚本不能乱来

LLM 写的脚本毕竟是不可信代码,Hermes 给它套了多层约束:

- **可用工具白名单**:脚本只能调 `SANDBOX_ALLOWED_TOOLS`(`:61`)与「当前 session 启用的工具」的**交集**(`:271`、`:886`)。这个白名单恰好是 **7 个**工具:`web_search`、`web_extract`、`read_file`、`write_file`、`search_files`、`patch`、`terminal`。
- **资源限制**(默认值,可经 `config.yaml → code_execution.*` 覆盖):
  - 超时 **5 分钟**(`DEFAULT_TIMEOUT = 300` 秒,`:72`)。
  - 最多 **50 次**工具调用(`DEFAULT_MAX_TOOL_CALLS = 50`,`:73`;`:529` 处计数到上限即拒绝)。
  - stdout 上限 **50 KB**(`MAX_STDOUT_BYTES = 50_000`,`:74`;超出时按 40% 头 + 60% 尾截断,`:1010`–`:1011`、`:1300`–`:1301`)。
  - stderr 上限 **10 KB**(`MAX_STDERR_BYTES = 10_000`,`:75`)。
- **环境变量清理**:`_scrub_child_env()`(`:136`、`:1223` 处对子进程调用)在把环境变量交给子进程前,先**剥掉一切像 secret 的变量**——凡名字里含 `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL` 等子串的都拦掉(`_SECRET_SUBSTRINGS`,`:90`)。这样即使脚本恶意 `print(os.environ)`,也读不到凭证。(历史踩坑:`:96` 注释记录,曾经宽泛地放行 `HERMES_` 前缀,结果泄露了像 `HERMES_BASE_URL`、`HERMES_*_WEBHOOK` 这类不含 secret 子串的配置;后来收紧成只显式放行少数定位 / profile 变量。)
- **其他安全措施**:ANSI 转义剥离 + 秘密编辑(把输出里的秘密打码)+ 进程组终止(超时时连同子进程一起杀,不留孤儿)+ 审批门控(危险操作仍要放行)。
- **两种执行模式**:`project`(用项目当前激活的 venv,脚本能用项目依赖)/ `strict`(隔离到临时目录,什么都不带,最干净)。

---

## 五、子代理委托(`tools/delegate_tool.py`)

### 5.1 它解决什么问题

有些任务最好「派一个独立的子 agent 去专心做」——既不让子任务的中间噪声污染父 agent 的上下文,又能并行处理多个子任务。委托(delegation)就是 Hermes 的子 agent 机制。父 agent 的上下文只看到「我委托了一个任务」和「子任务返回的摘要」,中间过程被隔离(`:15` 注释)。

### 5.2 隔离模型:每个子 agent 拿到什么

派生一个子 agent 时,它得到一个干净受限的环境:

- **全新 conversation**:没有父 agent 的历史,从零开始,专注本子任务。
- **自己的 task_id**:独立标识,便于跟踪 / 中断。
- **受限工具集——总是移除这 5 个工具**(`:47`–`:51`,每个都注释了原因):
  - `delegate_task`(`:47`)—— **不许递归委托**(否则会无限派生)。
  - `clarify`(`:48`)—— **不许跟用户交互**(子 agent 在后台跑,没法问用户)。
  - `memory`(`:49`)—— **不许写共享 MEMORY.md**(避免多个子 agent 互相踩)。
  - `send_message`(`:50`)—— **不许跨平台副作用**(子 agent 不该自己往外发消息)。
  - `execute_code`(`:51`)—— 子 agent 应该**一步步推理**,而不是又去写脚本绕过约束。
  - 此外还排除了若干 toolset(`_EXCLUDED_TOOLSET_NAMES`,`:122` = `debugging`、`safe`、`delegation`、`moa`、`rl`)。
- **聚焦的系统提示词**:只针对该子任务裁剪过的 system prompt。

### 5.3 两种角色:叶节点 vs 协调器

委托默认是「扁平」的——父派子,子不能再派。但有时子任务本身又需要拆分,于是有两种角色(`:313` 的角色规范化逻辑):

- **`leaf`(叶节点,默认)**:不能再往下委托。来路不明 / 空的角色字符串都被强制归一成 `leaf`(`:321`、`:325` 处对未知角色降级并告警)。
- **`orchestrator`(协调器)**:**保留委托工具集**,可以派生自己的工作者,形成「父 → 协调器 → 工作者」的链。
- **`max_spawn_depth` 控制深度上限**:`MAX_DEPTH = 1`(`:133`,默认扁平——父在深度 0,子在深度 1,孙子被拒绝,除非抬高);可配置的上限被钳制在 `[1, 3]` 区间(`_get_max_spawn_depth`,`:394`–`:395`;`_MAX_SPAWN_DEPTH_CAP = 3`,`:137`;`:420` 处 `max(_MIN_SPAWN_DEPTH, min(_MAX_SPAWN_DEPTH_CAP, ival))` 做钳制)。也就是说**默认深度 1,最大 3**。

### 5.4 单个子 agent 的纵向执行流程

派一个子 agent 跑起来,完整流程是(按顺序):

1. **凭证租约**:从共享凭证池(§3.2)拿一个软租约,确保并发的子 agent 不会把同一把 key 挤爆。
2. **心跳**:一个后台线程每 **30 秒**(`_HEARTBEAT_INTERVAL = 30`,`:514`)触摸一次父时间戳,告诉网关「我还活着」,防止被当成空闲而超时杀掉。配套有过时阈值:连续 15 个周期(15×30s=450s)轮次间空闲算 stale(`_HEARTBEAT_STALE_CYCLES_IDLE = 15`,`:522`),连续 40 个周期(40×30s=1200s)卡在同一工具算 stale(`_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40`,`:523`)。
3. **执行**:在 `ThreadPoolExecutor` 工作线程里跑,审批回调走非交互式——默认是 `_subagent_auto_deny`(自动拒绝,安全,与叶节点的工具黑名单一致),只有配 `delegation.subagent_auto_approve: true` 才换成自动放行(`:67`–`:103`)。
4. **硬超时 + 诊断转储**:默认 **600 秒**(`DEFAULT_CHILD_TIMEOUT = 600`,`:513`)的硬上限;若子 agent 一次 API 调用都没发就卡住,会触发零 API 调用诊断转储,帮助排查。
5. **File-state 协调**:多个 agent 可能读写同一批文件,Hermes 会在读到「已过时」的文件时提醒(stale read 提醒)。
6. **清理**:`finally` 块里收尾——停心跳、注销子 agent、释放凭证租约,保证不漏资源。

### 5.5 并行执行

批量任务时,多个 `_run_single_child` 在同一个 `ThreadPoolExecutor` 里**同时跑**,用 `FIRST_COMPLETED`(谁先完成先收谁)配合中断检查轮询来收割结果(`:28` 导入 `ThreadPoolExecutor`)。这样一批子任务能真正并行,而不是排队。

---

## 六、终端执行(`tools/terminal_tool.py`)

### 6.1 六个后端环境

终端工具让 agent 跑 shell 命令,而且能选在哪种环境里跑——共六种后端:**Local(本机)/ Docker / Singularity / SSH / Modal / ManagedModal / Daytona**(`:5`–`:6` 列出 local、Docker、Modal、SSH、Singularity、Daytona;`:12` 注释区分了 Modal 的「直连」与「托管网关」两种模式,即 ManagedModal)。从「直接在本机跑」到「容器隔离」再到「云沙箱」,覆盖不同的隔离与算力需求。

### 6.2 关键安全特性

让 agent 跑任意命令是高危操作,Hermes 加了几道闸:

- **危险命令审批**:通过合并守卫(consolidated guard)放行——它把 **tirith**(策略守卫)与「危险命令检测」合在一起判断(`_consolidated guard`,`:261`、`:279`)。判定安全返回 `None`,否则返回错误信息字符串拦下来。
- **sudo 密码管道**:把 `sudo ...` 重写成 `sudo -S -p ''`(`:547`、`:762`),让 sudo 从 stdin 读密码、且不打印提示符,从而实现「无头(non-interactive)密码输入」。注意有例外:本机若配了 `NOPASSWD` 的 sudoers,就不强行走这条密码管道路径(`:799`–`:801`),且这套处理被限定在 local 后端(`:801`,Docker/SSH/Modal 等各管各的,`:814` 提到尾随换行是必须的——sudo -S 按行读密码)。
- **Workdir 验证**:工作目录只允许安全字符白名单,挡掉注入。
- **复合后台重写**:修复了 `A && B &` 这种「复合命令 + 后台」会导致 subshell 等待错乱的 bug(把它重写成正确形态)。
- **前台 / 后台指导**:遇到「长期运行的服务器型命令」,自动提示加 `background=true`,免得 agent 把自己挂在一个永不返回的命令上。

---

## 七、Shell Hooks(`agent/shell_hooks.py`)

### 7.1 它解决什么问题

Shell Hooks 让**用户**配置「当某个特定事件发生时,执行哪个 shell 脚本」——这是给用户的扩展点(区别于 §2 给开发者的插件钩子)。配置来自 `cli-config.yaml` 的 `hooks:` 块(`:4`、`:175` 解析)。

### 7.2 Wire 协议:脚本和 Hermes 怎么对话

约定很简单(`:30`–`:51`):

- **stdin(输入)**:Hermes 把一个 JSON 通过管道喂给脚本,告诉它「发生了什么事件、相关数据是什么」。
- **stdout(输出)**:脚本回一个 JSON(可选——回别的或空都按「无意见」处理)。这个 JSON 可以表达两类意图:
  - **拦截决策**:`{"decision": "block", "reason": "..."}`(Claude-Code 风格,`:44`)或 `{"action": "block", "message": "..."}`(Hermes 原生风格,`:45`)——告诉 Hermes「别让这个动作发生」。
  - **上下文注入**:往对话里塞一段额外上下文。

### 7.3 安全约束

- **`shell=False`**(`:17`):脚本以 `shell=False` 方式启动,**杜绝 shell 注入**这一类经典坑。
- **超时**:默认 **60 秒**(`DEFAULT_TIMEOUT_SECONDS = 60`,`:83`),上限 **300 秒**(`MAX_TIMEOUT_SECONDS = 300`,`:84`;非法 timeout 值会回退默认并告警,`:325`–`:330`)。
- **JSON 响应解析**:只认结构化 JSON,其余忽略。
- **非零退出码**:被**记录但不阻止**——脚本挂了只是记一笔日志,不会因此卡住主流程。
- **优先级**:核心 block 决策在平局时压过 shell-hook 的 block(`:15` 注释「block decisions win ties over shell-hook blocks」)。

---

## 八、小结:这一层在「KV 调度」视角下的意义

本报告讲的都是 Hermes 的「外围与执行支撑层」,看似和推理后端的 KV / 调度无关,但有两条间接联系值得点出:

1. **凭证池的故障转移和会话亲和有张力**:多 key 轮换(§3.2)会让「同一会话的连续请求落到不同 key / 不同上游路由」,这对依赖会话亲和的 KV cache 复用是不利的;真正的会话亲和键(session_id / prompt_cache_key 等)走的是另一套机制(详见报告 17 与报告 12)。
2. **代码执行(§4)与子代理委托(§5)会显著改变请求形态**:PTC 把多步工具链折叠成一次推理,委托则派生出带独立上下文的子 agent——这两者都会影响「一个部署里到底有多少种前缀、各自多长、复用率多高」。要估算真实 KV 收益,必须把这些执行路径产生的请求也纳入观测(观测方法见报告 15 的真实 CLI 测试台,信号映射见报告 16)。

> 配套阅读:报告 12–15(各传输路径的 agent hint 实测)、报告 16(Dynamo 风格 KV/调度的信号映射与测试台设计)、报告 17(每个信号的逐条提取配方)。
