# Hermes Agent Web、TUI、ACP与批处理架构

> **一句话**:前面的报告(01–08)讲的是 Hermes 的"核心大脑"——对话循环、传输层、工具系统、上下文管理。本报告讲的是包在大脑外面的**一圈"外壳"**:用户怎么和 Hermes 交互(浏览器 Web 控制台 / 终端 TUI / 编辑器 ACP),离线怎么批量生成训练数据(批处理 + 轨迹压缩),会话状态存在哪里(SQLite),以及整个系统怎么打包成一个容器跑起来(Docker)。这些都不是推理热路径,但理解它们才能看清"一个请求是从哪个入口进来的、最后落到哪个 `AIAgent`"。
>
> **读者画像**:你懂推理服务 / KV cache / LLM 工程,但完全没看过 Hermes 内部。本报告里凡是 Hermes 专有的概念(工具集 toolset、`AIAgent`、会话 session、压缩 compaction 等),第一次出现都先用一句话解释再用。
>
> **本报告定位**:这是一份**架构调研**(自顶向下读代码,梳理模块职责与数据流),不含 PASS/FAIL 实测断言——那些在报告 13–15。本报告里所有结论是"读源码读出来的结构事实",不是"打 mock 打出来的 wire 实测"。两者的区别很重要:架构调研描述"代码长什么样",实测验证描述"请求真发出来是什么值"。

---

## 0. 先建立全局图:Hermes 有几个"入口",它们怎么共用一个大脑

Hermes 的核心是一个叫 `AIAgent` 的类——它持有一次会话(session)的全部状态(消息历史、工具集、模型配置),`AIAgent.run_conversation()` 就是跑一轮"收到用户消息 → 调 LLM → 可能调工具 → 再调 LLM …"的对话循环。**本报告讲的所有东西,最终都是为了把用户的输入喂给某个 `AIAgent`,再把它的输出渲染回去。**

入口有这么几类,它们的差别只在"前端长什么样、用什么协议通信":

| 入口 | 给谁用 | 通信方式 | 本报告章节 |
|------|--------|---------|-----------|
| **Web 控制台** | 浏览器里的运维 / 管理界面 | HTTP + WebSocket(经后端 API) | 第一节 |
| **TUI Gateway** | 终端里的富交互界面(Ink) | stdio 上的 JSON-RPC 2.0 | 第二节 |
| **ACP 适配器** | 编辑器(Zed / VS Code / JetBrains) | ACP over stdio | 第三节 |
| **批处理 runner** | 离线批量生成训练轨迹 | 直接在 Python 里多进程驱动 | 第四、五节 |

而它们共享的底座是:**SQLite 状态存储**(会话与消息持久化,第六节)、**Docker 容器**(把整套东西打包跑起来,第七节)、**工具集系统**(决定每个入口能用哪些工具,第八节)。

下面逐个展开。

---

## 一、Web 前端架构

Hermes 自带一个浏览器里的**管理控制台**(不是给终端用户聊天用的主界面,而是给运维 / 配置 / 观测用的),它是一个标准的现代单页应用(SPA,Single-Page Application,即整个站点只加载一次 HTML、之后靠 JS 在前端切换页面)。

**技术栈**:React 19 + TypeScript + Vite 7 + Tailwind CSS 4 + React Router DOM v7 + @xterm/xterm。
这里值得单独点出的是 **@xterm/xterm**——它是一个在浏览器里渲染**终端模拟器**的库。Hermes 的 Web 控制台里能嵌一个真正的终端(连到后端的 PTY,即伪终端),这个能力就来自它。

### 核心架构:Provider 层层包裹

React 应用的根部是一串"Provider"嵌套(Provider 是 React 的依赖注入机制,外层 Provider 提供的上下文,内层所有组件都能读到):

```
BrowserRouter → I18nProvider → ThemeProvider → SystemActionsProvider → App
```

从外到内的含义是:`BrowserRouter`(路由,决定 URL 对应哪个页面)→ `I18nProvider`(国际化 / 多语言)→ `ThemeProvider`(主题 / 明暗色)→ `SystemActionsProvider`(全局系统操作,比如重启服务这类动作)→ `App`(真正的应用主体)。这种"层层包裹"是 React 的标准写法:把全局能力放在外层 Provider 里,让任意深处的子组件都能直接取用,不必逐层手动传参。

### App.tsx — 一个完整的 SPA

`App.tsx` 是整个控制台的主体,承担了这些职责:

- **响应式侧边栏**:支持折叠动画,折叠后进入"只剩图标(icon-only)"模式,适配窄屏。
- **导航系统:内置路由 + 插件路由动态合并**。这是 Web 端最值得理解的设计——导航条不是写死的,而是"内置页面"与"插件注册的页面"在运行时合并出来的。
  - **内置路由**(开箱即有的页面):`/sessions`、`/analytics`、`/models`、`/logs`、`/cron`、`/skills`、`/plugins`、`/mcp`、`/channels`、`/webhooks`、`/pairing`、`/profiles`、`/config`、`/env`、`/system`、`/docs`、`/chat`。从名字就能看出控制台覆盖的面:会话管理、分析、模型、日志、定时任务、技能、插件、MCP(Model Context Protocol,一种工具 / 资源接入协议)、消息通道、webhook、配对、配置档(profile)、配置、环境变量、系统、文档、聊天。
  - **插件注册**:插件通过 `PluginManifest.tab` 这个字段往导航里注册自己的页面,而且能力很强——可以**新增**一个页面、**覆盖**一个已有页面,甚至**隐藏**某个页面。也就是说插件不只是"加东西",还能改写控制台已有的导航。
  - **排序控制**:负责拼出最终导航项列表的函数是 `buildNavItems()`,它支持用 `position: "after:X"` 或 `"before:X"` 来精确指定"我这一项放在 X 之后 / 之前",插件因此能把自己插到导航条的任意位置。
- **嵌入式聊天的持久化宿主**:聊天页 `ChatPage` 被设计成一个"持久宿主"——意思是当你在控制台里切到别的页面再切回来时,聊天用的 PTY(伪终端)、xterm(终端渲染)、WebSocket(实时连接)**不会被卸载重建**。这点对体验很关键:终端会话和实时连接如果每次切页都重连,既慢又会丢状态;Hermes 让 `ChatPage` 常驻,切页只是把它藏起来。
- **插件 slot(挂载点)位置**:除了能注册整页,插件还能往界面的几个固定"插槽"里塞 UI,这些 slot 是:`backdrop`(背景层)、`header-banner`(顶栏横幅)、`header-left` / `header-right`(顶栏左 / 右)、`pre-main` / `post-main`(主内容前 / 后)、`overlay`(浮层)。有了这套 slot,插件能在不改主代码的前提下往界面各处注入元素。

### Vite 配置:开发期与构建期的两个关键设计

Vite 是这套前端的构建 / 开发服务器工具,Hermes 的 `vite.config.ts` 里有三处值得注意的定制:

- **`hermesDevToken()` 自定义插件**:开发时前端要调后端 API,但后端的会话需要 token。这个自定义 Vite 插件会**从正在运行的后端自动抓取会话 token**,省去开发者手工配置——开发体验上的一个小巧设计。
- **`/api` 路由代理**:开发服务器把 `/api` 开头的请求**代理到后端**,并且**带 WebSocket 支持**。这样前端开发时不用关心后端跑在哪个端口,统一走 `/api` 即可,WebSocket(实时双向连接,聊天和日志流要用)也能透传。
- **构建输出目录**:`vite build` 的产物输出到 `../hermes_cli/web_dist`——也就是**直接打进 Python 包内部**。这意味着 Web 控制台不是单独部署的前端站点,而是作为静态资源被 Python 后端打包并提供服务;装了 Hermes 的 Python 包就自带了这套前端。

---

## 二、TUI Gateway

**TUI** = Terminal User Interface,即终端里的富交互界面(不是简单的命令行问答,而是有面板、菜单、实时刷新的那种)。Hermes 的 TUI 前端用 **Ink**(一个用 React 写终端界面的 Node.js 框架)实现,跑在 Node.js 进程里。

问题来了:TUI 在 Node 里,而 Hermes 的大脑 `AIAgent` 在 Python 里。两者怎么对话?答案就是 **TUI Gateway**——一个**桥接进程**,它夹在"Node 写的终端 UI"和"Python 写的 Agent"之间,两边通过 **stdio 上的 JSON-RPC 2.0** 通信(JSON-RPC 是一种用 JSON 表示"调用某个方法、返回结果"的轻量协议;走 stdio 意味着不开网络端口,直接用进程的标准输入 / 输出管道传消息)。

### 组件关系:一个请求的完整流转

```
TUI(Ink/Node) → stdin → entry.py → server.dispatch → pool thread → transport.write → stdout → TUI
                                                                   ↳ WsPublisherTransport → dashboard
```

读这条链路:Node 端的 TUI 把一个 JSON-RPC 请求写进 Python 进程的 **stdin** → `entry.py` 读到它 → 交给 `server.dispatch` 分发 → 在线程池里挑一个工作线程(pool thread)处理 → 处理结果经 `transport.write` 写回 → 从 Python 进程的 **stdout** 出去 → TUI 收到并渲染。分叉的那一支(`↳`)表示:同一份输出还能**镜像(mirror)**一份给 `WsPublisherTransport`,经 WebSocket 推给一个网页仪表板(dashboard)——这样终端里发生的事,网页上也能实时看到。

下面把这条链上的几个文件逐个讲清。

**`entry.py` — 主入口**:
- 它就是上面那个"读 stdin、写 stdout"的循环:**按行读取 JSON-RPC 请求,处理后把响应写回 stdout**。
- **首次启动发 `gateway.ready` 事件**:进程起来后先告诉 TUI"我准备好了",TUI 才知道可以开始发请求。
- **信号处理**(进程收到操作系统信号时怎么反应):`SIGPIPE → SIG_IGN`(忽略管道破裂信号,避免下游 TUI 关掉时自己被信号杀死)、`SIGTERM → 记日志 + 优雅关闭`(收到终止请求时先清理再退)、`SIGINT → SIG_IGN`(忽略 Ctrl-C,因为中断应该由 TUI 那边处理,而不是把桥接进程打断)。这套信号处理的目的是让桥接进程足够"皮实",不会因为终端那头的动作而意外崩掉。
- **MCP 工具发现放后台 daemon 线程**:MCP(Model Context Protocol)的工具发现可能较慢,如果在主流程里同步做,会**阻塞 TUI 启动**。所以它被放进一个后台守护(daemon)线程里跑,TUI 能先起来,工具稍后到位。
- **Sidecar 发布者**:当设置了环境变量 `HERMES_TUI_SIDECAR_URL` 时,输出会**经 WebSocket 镜像**到那个地址(这就是上面链路图里 `WsPublisherTransport → dashboard` 那一支)。

**`transport.py` — 传输抽象层**:
"传输(transport)"在这里指"把一个对象写到某个出口"的统一接口。这个文件把"写到哪里"抽象掉,让上层不用关心目的地:
- **`Transport` 协议**:核心方法就一个,`write(obj) → bool`(把对象写出去,返回是否成功)。
- **`StdioTransport`**:写到 stdout 的实现,**线程安全**(多个池线程可能同时要写,得加锁保证不串行错乱),并且对 `BrokenPipeError`(管道破裂,通常意味着 TUI 那端关了)做了**精细分类**处理,而不是粗暴崩溃。
- **`TeeTransport`**:名字来自 Unix 的 `tee` 命令——它把同一份输出**同时写到主传输 + N 个"尽力而为(best-effort)"的辅助传输**。"尽力而为"意味着辅助传输失败不影响主传输,这正是"终端输出 + 网页镜像"能并存的原因。
- **`ContextVar` 绑定**:`_current_transport` 是一个 `ContextVar`(Python 的"上下文局部变量",类似线程局部变量但对异步友好)。它的作用是**让每个请求上下文里的代码,自动写到正确的传输上**——因为同一时刻可能有多个请求在不同线程 / 上下文里跑,不能让它们的输出串到一起。

**`ws.py` — WebSocket 传输**:
- **`WSTransport`**:把 JSON 帧(frame)发到一个 asyncio 的 WebSocket 连接上(asyncio 是 Python 的异步框架)。
- **线程安全的跨界调用**:难点在于"写"是从**池线程**(同步世界)发起的,而 WebSocket 跑在 **asyncio 事件循环**(异步世界)里。它用 `run_coroutine_threadsafe` 把写操作从池线程安全地投递进事件循环——这是 Python 里"同步线程调异步代码"的标准桥接手法。
- **断连回退**:WebSocket 一旦 disconnect,就**回退到 `_stdio_transport`**,保证输出不丢(网页连接断了,至少终端还在)。

**`render.py` — 渲染桥接**:
TUI 里有些富渲染(比如带颜色的 diff、流式输出)是 Python 侧的渲染器(`agent.rich_output`)生成的。这个文件负责**把对 `agent.rich_output` 函数的调用,路由到 Python 侧的渲染器**,暴露出 `render_message()`(渲染一条消息)、`render_diff()`(渲染代码 diff)、`make_stream_renderer()`(造一个流式渲染器,边收边渲)这几个函数。

**`slash_worker.py` — 持久斜杠命令工作器**:
"斜杠命令"指用户在 TUI 里敲的 `/xxx` 命令(比如 `/help`、`/model`)。这个 worker 的设计:
- **每个 TUI 会话维护一个 `HermesCLI` 实例**(`HermesCLI` 是命令行那套逻辑的封装)。"持久"指这个实例常驻,跨多次命令复用,不每次重建。
- **协议**:输入 `{id, command}` → 输出 `{id, ok, output|error}`(`id` 用来把请求和响应配对,`ok` 表示成败,成功给 `output`、失败给 `error`)。
- **捕获 Rich 输出的技巧**:`HermesCLI` 内部用 Rich 库(Python 的富文本终端库)打印,而这里要把打印结果**截获成字符串**返回给 TUI。做法是**替换 `Console.file` 的缓冲区**——把 Rich 的输出目标从真实终端换成一个内存缓冲,打印完再把缓冲里的内容读出来当 `output`。这是一种常见的"重定向输出以捕获"的技巧。

---

## 三、ACP 适配器(`acp_adapter/`)

**ACP** = Agent Client Protocol,一种让**编辑器**(Zed、VS Code、JetBrains)接入 AI agent 的标准协议。ACP 适配器的作用是:**把 Hermes 的 `AIAgent` 包装成一个 ACP stdio 服务器**,这样支持 ACP 的编辑器就能把 Hermes 当作内置 agent 来用,在编辑器里直接和 Hermes 对话、让它改文件。

### 核心组件

**`server.py` — `HermesACPAgent`**:这是 ACP 服务器主体,实现协议要求的各种操作:
- **协议操作**:`list_sessions`(列会话,**带游标分页**,即一次返回一批、靠游标取下一批)、`new_session`(新建会话)、`load_session`(加载会话)、`resume_session`(恢复会话)、`fork_session`(从某个会话**分叉**出新会话)、`set_session_model`(切换会话用的模型)、`set_session_mode`(切换会话模式)。这套操作让编辑器能完整管理 Hermes 的会话生命周期。
- **提示处理跑在线程池上**:`AIAgent` 是**同步**的(`run_conversation()` 会阻塞),而 ACP 服务器要保持响应。解法是把同步的 `AIAgent` 放到 `ThreadPoolExecutor`(线程池)上跑,**最多 4 个 worker**——这样同时最多并行处理 4 个 agent 调用,既并发又不至于把资源耗光。
- **编辑器编辑快照**:当 Hermes 调用 `write_file` / `patch`(写文件 / 打补丁)时,适配器会**捕获编辑前后的图像(快照)**,这样编辑器能给用户展示"改了什么"的对比。
- **计划 / 待办集成**:Hermes 有个 todo 工具(让 agent 管理自己的待办计划)。适配器**把 Hermes 的 todo 工具结果转换成 Zed 原生的 `AgentPlanUpdate` panel**——也就是说 agent 在 Hermes 里维护的待办,会在 Zed 的原生计划面板里直接显示出来,而不是当成普通文本。

**`session.py` — `SessionManager`**:
- **线程安全的会话映射**:维护"ACP 会话 ID → `AIAgent` 实例"的映射,且**线程安全**(因为多个 worker 线程会并发访问)。
- **WSL 路径自动转换**:在 Windows 的 WSL(Linux 子系统)环境里,编辑器给的是 Windows 路径(`C:\...`),而 Hermes 跑在 Linux 侧需要 Linux 路径(`/mnt/c/...`)。`SessionManager` **自动做这个转换**,让跨系统的路径不出错。

**`tools.py` — 工具映射**:
ACP 协议对工具有自己的分类(叫 `ToolKind`),而 Hermes 有自己的一套工具。这个文件负责对接:
- **把 50+ 个 Hermes 工具映射到 ACP 的 `ToolKind`**(`read` / `edit` / `search` / `execute` / `think` / `fetch` / `other`)。编辑器靠 `ToolKind` 决定怎么展示一个工具调用(比如 `edit` 类会显示成编辑动作),所以这个映射决定了 UI 呈现。
- **`_POLISHED_TOOLS` 白名单,共 60 个**:这是一份**精心打磨过呈现效果**的工具白名单——名单里的工具有专门设计的展示格式,在编辑器里看起来更规整。在 `acp_adapter/tools.py` 中,白名单的判定逻辑出现在工具格式化路径上(例如对错误态的特殊处理、对带专属 formatter 的工具走专门渲染)。注意"50+ 工具映射"与"60 个 polished 工具"是两个不同的集合:前者是"能映射到 ToolKind 的工具总量",后者是"被特别精修过展示的子集"。

**`permissions.py` — 审批桥接**:
Hermes 在执行危险操作前会调 `prompt_dangerous_approval()` 向用户征求同意。在编辑器里,这个征求要走 ACP 的 `request_permission()`。这个文件就是**把前者桥接到后者**,并提供这些选项:`"Allow once"`(只允许这一次)、`"Allow for session"`(本会话内都允许)、`"Allow always"`(永远允许)、`"Deny"`(拒绝)、`"Deny always"`(永远拒绝)。这套选项让用户对 agent 的危险动作有细粒度控制。

**`edit_approval.py` — 编辑审批**:
- **`ContextVar` 门控**:编辑审批用一个 `ContextVar` 来开关——**只有 ACP 入口会设置它**,而 CLI / 网关会话则绕过(不走这套编辑审批)。这保证编辑审批是 ACP 专属行为,不会误伤其他入口。
- **策略三档**:`ask`(总是提示用户确认)、`workspace_session`(同一工作区内自动批准,跨工作区才问)、`session`(整个会话内全局自动批准)。这三档让用户在"安全"和"省事"之间选平衡点。

### 注册表(`agent.json`)

ACP 编辑器靠一份注册表(`agent.json`)知道"有这么个 agent、怎么装它":

```json
{
  "id": "hermes-agent",
  "name": "Hermes Agent",
  "distribution": {
    "uvx": {"package": "hermes-agent[acp]==0.15.1", "args": ["hermes-acp"]}
  }
}
```

这里 `uvx` 是 uv 工具链里"临时拉起一个 Python 包并运行"的命令(类似 `npx` 之于 npm)。这份注册表告诉编辑器:用 `uvx` 拉 `hermes-agent[acp]==0.15.1` 这个包(`[acp]` 是可选依赖组,装上 ACP 所需的额外依赖;`==0.15.1` 锁定版本),并以 `hermes-acp` 参数启动。**于是用户在兼容 ACP 的编辑器里能无缝安装 Hermes**,不用手工 pip install。

---

## 四、批量轨迹生成(`batch_runner.py`)

前三节是"在线交互入口"。从这一节起换到**离线场景**:批量跑 agent 来生成训练数据(轨迹,trajectory——即一整段"用户提问 → agent 思考 → 调工具 → 回答"的完整记录)。这类数据用于训练 / 微调模型。

`batch_runner.py` 的关键设计:
- **多进程并行**:用 `multiprocessing.Pool` 把一批提示(prompt)**分配到多个 CPU 核心**上并行跑。注意是多进程(`Pool`)而非多线程——因为每个 agent 跑起来是 CPU + IO 混合负载,多进程能真正吃满多核,绕开 Python 的 GIL(全局解释器锁)限制。
- **检查点 / 恢复**:每跑完一条轨迹就**写进 JSONL**(每行一个 JSON 对象的文件格式)。这样支持 `--resume`——中途挂了或主动停了,重跑时**只补没完成的**,已完成的不重跑。代码里能看到这个语义:只有成功保存后才标记 prompt 为完成(失败的会在 resume 时重试)。
- **工具集分布**:每一批从 **16 个预定义分布**里采样工具集(分布名如 `default`、`image_gen`、`research`、`science`、`development` 等)。"分布"指"以多大概率给这条轨迹配哪些工具集"——这么做是为了让生成的训练数据覆盖不同的工具使用场景,而不是千篇一律。(分布定义见第八节与 `toolset_distributions.py`。)
- **工具统计提取**:`_extract_tool_stats()` 从消息历史里**解析出 `tool_calls`**,统计每个工具的成功 / 失败次数。这给数据质量分析提供了基础指标。
- **轨迹格式**:产出用 **`from/value` 对格式**(每条消息是一个 `{from: 角色, value: 内容}` 对,这是常见的对话数据集格式),内容里用 `<tool_call>` / `<tool_response>` 这样的 **XML 标记**把工具调用和工具返回包起来,方便训练时识别这些结构。

---

## 五、轨迹压缩(`trajectory_compressor.py`)

上一节生成的轨迹可能很长,直接拿去训练既贵又可能超过模型上下文窗口。`trajectory_compressor.py` 做的是**离线后处理**:把一条已完成的轨迹压缩到一个目标 token 预算之内,**同时尽量保住训练信号的质量**。

> 注意区分:这里的"轨迹压缩"是**离线、针对训练数据**的;它和报告 13–14、17 讲的"上下文压缩 / compaction"(在线、对话进行中为了不超窗口而压历史)是**两套不同的机制**,只是思路相近(都靠"保护关键部分 + 摘要中间部分")。本节讲的是离线这套。

### 压缩策略

**目标**:在保留训练信号质量的同时,把轨迹压缩到目标 token 预算。具体分四步:

1. **保护关键轮次**(这些绝不压):系统提示词(system prompt)、第一轮人类消息、第一轮 GPT 消息、第一轮工具响应、以及**最后 N 轮(N=4)**。为什么保护首尾?因为开头确立了任务背景、结尾是最终结论,这些对训练信号最关键;中间的来回试探才是冗余大头。代码里这个"保护最后几轮"的参数是 `protect_last_n_turns: int = 4`。
2. **只压中间轮次**:可压缩区域从**第 2 轮工具响应**开始——也就是除了上面被保护的首尾,中间那段才允许动。
3. **基于 token 的压缩**:在可压缩区域里,用**一条摘要消息**替换掉那些工具响应(工具响应往往是最长最啰嗦的部分,比如一大段文件内容或搜索结果)。
4. **配置参数**:
   - **目标最大 15250 tokens**(`target_max_tokens: int = 15250`):压完后整条轨迹要落在这个上限内。
   - **摘要 750 tokens**(`summary_target_tokens: int = 750`):那条替换用的摘要消息本身的目标长度。
   - **分词器用 `moonshotai/Kimi-K2-Thinking`**(`tokenizer_name`):用哪个 tokenizer 来数 token——这很关键,因为"多少 token"取决于用谁来分词,必须和下游训练用的 tokenizer 一致才准。

---

## 六、SQLite 状态存储(`hermes_state.py`,~3956 行)

前面所有入口(Web / TUI / ACP / 批处理)产生的会话和消息,最终都落到这个 SQLite 持久层。`hermes_state.py` 大约 3956 行,是会话状态的真值源。

### Schema(版本 14)

Hermes 的数据库结构有版本号,当前是 **版本 14**(代码里 `SCHEMA_VERSION = 14`)。版本号用于迁移:升级时按版本号决定要不要改表结构。主要的表:

- **`sessions`**(一行一个会话):`id`、`source`(从哪个入口来)、`user_id`、`model`、`system_prompt`、`parent_session_id`(**压缩链**——一个会话因压缩轮换出新会话时,新会话用这个字段指回旧会话,形成血缘链)、token 统计、billing(计费)字段、`title` 等。这里的 `parent_session_id` 正是报告 17 里 I12 信号"session 轮换 + parent 血缘"在数据库侧的落点。
- **`messages`**(一行一条消息):自增 `id`、`session_id`(外键,指回所属会话)、`role`(角色:user / assistant / tool 等)、`content`、`tool_call_id`、`tool_calls`(JSON,记这条消息发起的工具调用)、`token_count`、`reasoning` / `reasoning_content` / `reasoning_details`(推理 / 思考过程的几种存法)、codex 相关数据等。一条消息要存这么多字段,是因为不同 provider(OpenAI / Anthropic / codex)的响应结构不同,都要能落库还原。
- **`state_meta`**:任意 key/value 状态,放零散的全局状态。
- **`compression_locks`**:`session_id`(主键)、`holder`(持锁者标识,格式 `pid:tid:nonce`,即"进程号:线程号:随机数")、`expires_at`(过期时间)。这张表是**压缩并发控制**用的:同一个会话同时只能有一个 worker 在压缩,靠这把带过期时间的锁互斥,避免两个进程同时压同一会话把数据搞乱。

### FTS5 全文搜索 — 双表设计

**FTS5** 是 SQLite 的全文搜索扩展(Full-Text Search version 5),用来对消息内容做关键词检索。Hermes 这里有个值得讲的设计:**为什么要建两张 FTS 表**。

原因是中文 / 日文 / 韩文(统称 **CJK**)和拉丁文字的分词需求不同。SQLite 默认的分词器对 CJK 不友好——它会把每个 CJK 字符当成一个独立 token,导致"子串搜索"(比如搜"上下文"能匹配到"上下文管理")做不了。所以分两张表各管一摊:

1. **`messages_fts`**:用 **`unicode61` 分词器**,处理**拉丁文本**(英文这类有空格分词的语言)。
2. **`messages_fts_trigram`**:用 **trigram 分词器**(三元组分词,把文本切成每 3 个字符的滑动片段),专门处理 **CJK 子串搜索**——因为默认分词器会把 CJK 拆成单 token,搜不了子串,trigram 正好补上这个能力。

**触发条件**:`messages` 表的 INSERT / UPDATE / DELETE 都挂了**触发器**(trigger),自动维护这两张 FTS 索引(代码里能看到 `messages_fts_insert/delete/update` 和 `messages_fts_trigram_insert/delete/update` 六个触发器)。这样应用层不用手动同步索引,数据库自己保持一致。

**搜索逻辑**(决定一次搜索走哪条路):
- **CJK 检测**:用 `_contains_cjk()` 判断查询里有没有 CJK 字符。**3 个及以上 CJK 字符** → 路由到 trigram 表。
- **短 CJK(1–2 字符)** → 走 **LIKE 回退**(trigram 对太短的串不划算,直接用 SQL 的 LIKE 模糊匹配)。
- **非 CJK** → 走 **`unicode61` FTS5 + BM25 排名 + `snippet()`**(BM25 是经典的相关度排序算法;`snippet()` 生成带高亮的结果摘要片段)。
- **三种排序模式**:`None`(只按 FTS5 自身的相关度排名)、`newest`(最新优先)、`oldest`(最旧优先)。
- **上下文**:每个匹配结果会**带上它前后各 1 条消息**,方便看清匹配出现的语境。

---

## 七、Docker 容器构建

Hermes 提供一个 Docker 镜像把整套东西打包跑起来。这个 `Dockerfile` 用**多阶段构建**(multi-stage build,即用多个构建阶段,最终镜像只保留需要的产物,体积更小)。

**基础与工具链**:Debian 13.4 + s6-overlay 监督 + uv 包管理器 + Node 22 LTS。
这里 **Node 22 LTS** 是单独拉的:Debian 13(trixie)自带的 nodejs 被钉在 20.x,而 Hermes 要 22 这个长期支持版,所以 `Dockerfile` 专门用上游 `node:22` 镜像作为来源阶段(`node_source`),从中取 Node 22,以便留在受支持的 LTS 上。

**关键层 / 关键设计**:
- **s6-overlay v3.2.3.0**:s6-overlay 是一个**进程监督系统**。在容器里它当 **PID 1**(容器的第一个进程):`/init` 启动 → `s6-svscan` 作为 PID 1 运行,**非阻塞地回收僵尸进程**(容器里如果没有合格的 PID 1 回收僵尸进程,会积累僵尸),并**监督主 hermes 进程、仪表板、以及每个 profile 的网关**。换句话说,一个容器里其实跑着好几个被监督的服务,s6 负责它们的拉起、看护和清理。代码注释里能看到这是"s6-overlay 监督计划的第二阶段",用 s6-overlay 的 `/init` 替换了早先的 tini。
- **Playwright Chromium**:装了 Playwright 的 Chromium,用于**浏览器自动化**(让 agent 能驱动真实浏览器做网页操作)。
- **Python 依赖层缓存**:用 `uv sync --frozen` 装 Python 依赖。`--frozen` 表示严格按锁文件装、不改锁;把它单独成层是为了**利用 Docker 层缓存**——只要依赖没变,这一层就能复用,重建镜像快。
- **用户重映射**:容器内用户 ID 可由 `HERMES_UID` 覆盖,**默认 10000**。把默认 UID 设成一个较大的非特权值,是出于安全 / 与宿主用户映射的考虑。
- **s6 服务定义**:**每个 profile 的网关**通过 `cont-init`(容器初始化阶段)**动态注册**为 s6 服务。也就是说你配了几个 profile,容器启动时就动态生成几个网关服务交给 s6 监督——服务列表不是写死的,而是按配置生成的。
- **Docker exec 填充程序(shim)**:当你 `docker exec` 进容器执行命令时,有一个 shim 会**通过 `s6-setuidgid hermes` 透明地重新执行**你的命令——目的是让 exec 进来的命令也以正确的 `hermes` 用户身份跑,而不是 root,保持权限一致。

---

## 八、工具集与平台映射

最后这一节回到"工具集"这个贯穿全报告的概念,讲清它的组织方式。**工具集(toolset)** 是 Hermes 给工具分的组——不是单个工具,而是"一批相关工具的集合"(比如 web 工具集里有搜索、抓网页等)。每个入口 / 平台能用哪些工具,就是靠"启用哪些工具集"来决定的。

**工具集系统(`toolsets.py`)**:
- **叶子工具集 ~40 个**:最细粒度的、按能力分的工具集,如 `web`(网页)、`vision`(视觉)、`terminal`(终端)、`browser`(浏览器)、`skills`(技能)、`todo`(待办)、`memory`(记忆)等。
- **组合工具集**:由叶子工具集组合出的更大的集,如 `debugging`(调试)、`safe`(安全子集)、`hermes-gateway`(网关用)。
- **平台工具集 ~15 个**:和具体接入平台 / 入口绑定的工具集,如 `hermes-cli`、`hermes-cron`、`hermes-acp`,以及所有消息平台对应的工具集。
- **共享核心(`_HERMES_CORE_TOOLS`):55 个工具在所有平台工具集中共享**。这批核心工具是每个平台都会带上的"底座"(代码里 `_HERMES_CORE_TOOLS` 定义在 `toolsets.py:31`,被各平台工具集引用)。

> **关于"工具数量"的重要澄清(对应报告 17 §2.1 的勘误,务必理解)**:本节说的"叶子 ~40 个 / 平台 ~15 个 / 核心 55 个"是**对 `toolsets.py` 这份配置清单做的静态计数**,是"代码里列了多少"的结构事实。它和报告 13–15 里"实测某个请求里有 29 个工具"是**两个层面**,不要混淆:
> - **核心工具恒含**:任何平台工具集都会带上 `_HERMES_CORE_TOOLS` 这批核心工具——这是结构上的恒定事实。
> - **config 层的 `toolsets:[]` ≠ 内部参数 `enabled_toolsets=[]`**:在配置文件里写空的 `toolsets: []`,经 CLI 转换后**仍会带上核心工具**;而如果直接给 Hermes 内部函数传 `enabled_toolsets=[]`(空列表,且 `[]` 不等于 `None`),则会得到 **0 个工具**。这是报告 17 反复强调的一个坑:同样是"空",在两个层含义完全相反。
> - **实测的"29 个工具""16096 字符 system"是环境 / 配置相关的捕获值,不是常量**:它们随配置、工具集、上下文文件变化;采集时一律从真实出站请求(wire)里读真实值,**绝不能硬编码 29 或 16096**。本节的静态计数(40/15/55)同理是"清单计数",换版本会变。

**工具集分布(`toolset_distributions.py`)**:
- **16 个预定义分布**,用于第四节批量运行期间的**概率工具集选择**——决定"这条轨迹以多大概率配哪个工具集组合"。
- **`sample_toolsets_from_distribution()`**:具体的采样函数,它**按百分比独立掷骰子**决定每个工具集要不要进——也就是说每个工具集是否被选是独立的概率事件,而不是从一个固定列表里挑。这种独立掷骰让生成的数据在工具组合上有自然的多样性。

---

## 九、和其他报告的关系(交叉引用)

本报告是"外壳"层的结构梳理,几个点会在别处被深挖或验证,串起来看更完整:

- **会话与消息怎么持久化**(第六节)→ 报告 17 的 I12(session 轮换 + `parent_session_id` 血缘)、I16(usage 桶)直接落在 `sessions` / `messages` 表上;报告 17 提醒 usage 桶内部叫 `cache_write_tokens`(只有 Anthropic wire 字段才叫 `cache_creation_input_tokens`)。
- **工具集 / 工具数量的精确语义**(第八节)→ 详见报告 17 §2.1 勘误(config `toolsets:[]` ≠ 内部 `enabled_toolsets=[]`、核心工具恒含、29/16096 是环境相关捕获值)。
- **离线轨迹压缩 vs 在线上下文压缩**(第五节)→ 在线压缩(阈值 `max(ctx×0.5, 64000)`、SUMMARY_PREFIX、preflight + real-usage 两阶段)见报告 13–14 与报告 17 的 I7–I12;本节讲的是**离线、针对训练数据**的另一套。
- **多入口共用一个 `AIAgent`**(第零节)→ 报告 15 验证过:gateway 与 `--resume` 走的都是"新建 `AIAgent` + 从 SessionDB 还原历史"的同一条真实路径;ACP 的 session 管理(第三节)也是这套思路在编辑器入口的体现。

---

## 十、诚实边界

1. **本报告是架构调研,不是 wire 实测**:文中的工具集计数(40/15/55)、分布数(16)、schema 版本(14)、压缩参数(15250/750/4)、Docker 版本号等,都是**读源码 / 配置读出来的结构事实**,不是打 mock 打出来的 PASS/FAIL 断言。需要 wire 级实测(请求里真出现了什么)请看报告 13–15。
2. **静态计数会随版本漂移**:工具集数量、分布数量、`hermes_state.py` 行数(~3956)这类计数都是某一时刻的快照,Hermes 升级后须重核(项目已有 v0.13→v0.15 的漂移前例,详见记忆 `hermes-two-installs-version-skew`)。
3. **"工具数量"的层级陷阱已在第八节澄清**:不要把"清单里列了 N 个工具集 / 工具"等同于"某个请求里会带 N 个工具"——前者是结构,后者是环境相关的 wire 捕获值,采集一律从 wire 读。
