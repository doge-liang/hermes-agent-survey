# 15 — 真实 hermes CLI + mock 探测测试台

> **一句话**:前面报告 12–14 是"在 Python 里直接调 Hermes 的函数"来观察它发什么请求;本报告换成"像真实用户那样从命令行启动 `hermes`",对着一个本地假后端(mock)跑,看真实部署下到底往推理后端发了哪些 agent hint,并把结论沉淀成一份**后端无关的 fixtures**,作为以后接真实推理后端时的对照基准。
> **规模**:12 个场景,断言结果 **25 PASS / 0 FAIL / 3 INFO**,全程 **0 次请求逃逸到外部真实 API**。
> **平台代码**:`cli_platform/`(`driver_cli.py` 编排、`check_cli.py` 断言、`gen_fixtures.py` 生成 fixtures、`cli_requests.jsonl` 捕获、`fixtures.json` 产物)。

---

## 0. 先讲清楚:两种"驱动 Hermes"的方式,差别在哪

要观察"Hermes 给推理后端发了什么",得先让 Hermes 跑起来发请求。有两条路:

**(A) import-driver(报告 12–14 用的)** —— 在一个 Python 脚本里 `import` Hermes,直接 `new` 一个 `AIAgent` 对象,手工填好它的内部状态,然后调 `AIAgent.run_conversation()`。好处是快、可控、能用 monkeypatch 强行打开某个开关;坏处是**它从对话循环的"半路"切进去,绕过了真实部署必经的前半段**——配置文件解析、工具集组装、完整 system prompt 拼装、CLI 参数到 agent 状态的映射全被跳过了。结果就是:import-driver 看到的请求是个"缩水版",tools 经常是 0 个、system prompt 只有一两千字符。

**(B) 真实 CLI(本报告用的)** —— 不 import Hermes,而是像真实用户一样在命令行敲 `hermes chat -q "..." --provider ...`,用子进程把它启动起来。它会**走完整的真实入口**:读 `config.yaml`、组装工具、拼完整 system prompt、把 CLI 参数映射成 agent 内部状态,最后才发请求。这样捕获到的请求,才是"推理框架在生产里真正会收到的样子"。

本报告做的就是 (B):用真实 CLI 打一个本地 mock,把每个请求录下来,验证各种 agent hint 是否按预期出现。这补上了 import-driver 的盲区。

---

## 1. import-driver 与真实 CLI 的逐项差异

| 维度 | import-driver(报告 12–14) | 真实 CLI(本报告) | 为什么差这么多 |
|------|---------------------------|--------------------|----------------|
| **入口** | 直接 `AIAgent.run_conversation()` | `hermes chat` 子命令 | CLI 多走了 config 解析 + 状态映射这一整段 |
| **tools** | 内部参数 `enabled_toolsets=[]` → **0 个工具** | 配置 `toolsets:[]` → **实测 29 个工具** | 见下方 ⚠️:配置层的空列表 ≠ 内部参数的空列表 |
| **system prompt** | ~1700 字符(缩水) | **~16096 字符**(含 AGENTS/规则/工具说明) | CLI 不加 `--ignore-user-config`,会带上完整上下文文件 |
| **触发推理 hint 的方式** | monkeypatch 强行返回 True | 纯 `config.yaml` + 真实模型名**自然触发** | CLI 走真实门控逻辑,更忠实于线上 |
| **多轮对话** | 手工把上一轮 `result['messages']` 传回去 | `--resume <SID>` 经 SessionDB 自动还原 | CLI 的多轮路径 = gateway 线上路径 |

> ⚠️ **关于"29 个工具"的精确含义(报告 17 §2.1 勘误,重要)**:这里左右两列看似矛盾(`[]` 一边 0 一边 29),其实是**两个不同的层**:
> - import-driver 调的是 Hermes 内部函数 `get_tool_definitions(enabled_toolsets=[])`。这个函数里有个判断 `if enabled_toolsets is not None`,而**空列表 `[]` 不等于 `None`**,于是它进入"对一个空列表做循环"→ 结果是 **0 个工具**。
> - 真实 CLI 读的是 `config.yaml` 里的 `toolsets: []`。CLI 这一层并不会把它原样当成内部的 `enabled_toolsets=[]`——它转换后**仍然带上一批核心工具**(`_HERMES_CORE_TOOLS`),实测出站请求里有 **29 个**。
>
> 所以:**配置层的 `toolsets:[]` ≠ 内部参数的 `enabled_toolsets=[]`**。报告 12–14 的"0 工具"是 import 级现象,本报告的"29 工具"是真实 wire 实测——两者都对,只是测的不是同一层。
> 同理"16096 字符 system":这是"默认带上 AGENTS.md 等上下文文件"时的量级,**不是固定常量**;换配置、换工具集都会变。
> **给后续实现的唯一安全结论:工具数量和 system 长度都是环境/配置相关的捕获值,采集时一律从实际请求(wire)里读真实值,绝不要硬编码 29 或 16096。**

---

## 2. 这个 CLI 测试台是怎么搭的(每条"踩坑契约"讲清来龙去脉)

用子进程驱动真实 CLI 看似简单,实际有一串坑。每条都是实测撞出来的,记录如下——既是本平台的设计依据,也是"如果你要自己搭一个,这些会咬你"的清单。

**契约 1:受控多轮必须走 `hermes chat`,不能用 `-z` 一次性模式。**
我们需要"多轮对话"来观察缓存断点滑动、历史累积这些跨轮现象。最初试了 `hermes ... -z`(one-shot 一次性回答),配合 `--continue/--resume` 想串起多轮,结果发现 mock 看到的消息数恒为 2(scout 现象),根本没累积。原因:`-z` 走的是一个 oneshot 分支(`main.py:15516`),这个分支在处理 resume 的代码(`:15528`)**之前**就短路返回了,等于完全忽略了 `--continue/--resume`。
**解法**:改用 `hermes chat` 子命令。第一轮正常建一个新会话,从它的 stderr 里用正则 `\d{8}_\d{6}_[0-9a-f]{6}` 抓出 `session_id`;后续每一轮带 `--resume <抓到的SID>`,Hermes 就会从本地的 SessionDB(SQLite)里把历史还原回来。这样 mock 才看到消息数真实累积(实测 1 → 3 → 5)。

**契约 2:`chat` 路径不能加 `--ignore-user-config`。**
这个开关本意是"忽略用户配置"。但在我们的测试里,场景配置就放在 `{HERMES_HOME}/config.yaml`,而 Hermes 把它当成"用户配置";加了这个开关,场景配置直接被丢掉,测试就跑空了。**解法**:不加它,靠隔离的 `HERMES_HOME` 来保证每个场景的配置干净。

**契约 3:`provider` 名字必须等于 `custom_providers` 里那个条目的 `name`。**
直觉上会想写 `provider: custom`,但 Hermes 的 provider 解析器是按名字去 `custom_providers` 列表里找对应条目的;名字对不上就找不到 API key,报"Provider resolver returned an empty API key"。**解法**:`provider` 字段填的值,要和 `custom_providers[].name` 一模一样(比如都叫 `mockcustom`)。

**契约 4:关掉懒安装 + 清空可选工具,避免子进程卡死。**
某些可选工具集(如 edge-tts 语音)在首次用到时会触发"懒安装"(lazy install)去下载依赖,把子进程卡住。**解法**:配置里设 `security.allow_lazy_installs: false`,命令行加 `-t ''`(清空可选 toolset),让它别去装东西。

**契约 5:压缩 / tier 这类场景必须用"第三方 anthropic-wire",不能用原生 `provider=anthropic`。**
这是个隐蔽的逃逸坑。当用原生 `provider=anthropic` 触发上下文压缩时,Hermes 生成摘要用的那个**辅助 LLM client 不继承我们 override 的 mock base_url**,于是它会直接打到真实的 `api.anthropic.com`(用无效 token 拿到 401)。这既污染测试,又是真外部调用。**解法**:走"第三方 anthropic-wire"——即 `provider=custom` + `base_url` 以 `/anthropic` 结尾 + 模型名含 `claude`。这条路径下,摘要辅助请求会乖乖留在我们配置的 base_url(mock)上,不逃逸。

**契约 6:单个 `-q` 命令行参数有约 128KB 的长度上限。**
压缩场景需要"很长的内容"把上下文撑过阈值。最初想用一个超大的 `-q "巨长 filler"`,结果报 "Argument list too long"——这是操作系统对单个参数的限制(`MAX_ARG_STRLEN`,约 128KB)。**解法**:把单条 filler 控制在 128KB 以内,靠**多轮累积**(历史经 SessionDB 攒起来,不占命令行参数)把总量推过压缩阈值。

> 平台文件:`cli_platform/driver_cli.py` 负责子进程编排 + 从 stderr 抓 SID + 通过 `/__mock/control` 端点给 mock 下发场景行为——它**完全不 import Hermes,只是 shell out 启动真实 CLI**;`check_cli.py` 跑断言;`gen_fixtures.py` 把捕获提炼成后端无关 fixtures;`cli_requests.jsonl` 是原始捕获。

---

## 3. 12 个场景与实测结果

场景编号 `RC-01 … RC-10`(RC = Real-CLI),每个都映射到前面报告里 import-driver 验过的某个用例(字母 A–J 来自报告 13、`S1–S13` 来自报告 14),目的是"用真实 CLI 把同一件事再忠实地验一遍"。结果三种:**PASS**(断言通过)、**FAIL**(失败,本批为 0)、**INFO**(因 host 门控等真实约束,localhost 测不了,留到真实后端阶段,详 §4)。

| 场景 | 映射用例 | 触发方式(config/CLI) | 实测结果 |
|------|---------|------------------------|----------|
| **RC-01** | A | custom chat_completions 基线 | **29 tools + system 16096 字符** + `stream_options`;无 `body.session_id` ✅ |
| **RC-02** | E,S3,B,S5 | 第三方 `/anthropic` + 3 轮 `--resume` | native `cache_control` 断点滑动 **`[0]→[0,1,2]→[2,3,4]`**,封顶 4 个;beta 头 2 个;system 字节跨轮稳定(16110×3);带 29 tools ✅ |
| **RC-03** | S2 | `prompt_caching.cache_ttl: 1h` | 缓存 marker 变成 `{type:ephemeral, ttl:1h}` ✅ |
| **RC-04a** | D,S7 | `claude-opus-4-7` + `reasoning_effort:xhigh` | `thinking={adaptive}` + `output_config={effort:xhigh}` ✅ |
| **RC-04b** | S7 | `claude-opus-4-6` + xhigh | `output_config={effort:max}`(老一代模型自动降级)✅ |
| **RC-04c** | S7 | `claude-3-7-sonnet` + high | `thinking={enabled, budget_tokens:16000}`(更老模型走手动预算)✅ |
| **RC-05** | H | custom `api_mode:codex_responses` | 打 `/v1/responses`、`store=false`、带 `prompt_cache_key`、用 `instructions`(不是 messages)、无 codex 身份 header ✅ |
| **RC-06** | G | `provider=openrouter` | **INFO**:OpenRouter 那套 hint 要 host 命中 `openrouter.ai`,localhost 激活不了(真跑会打真实 openrouter.ai 拿 401)→ 留真实后端阶段 |
| **RC-07** | J,S4 | openrouter + reasoning | **INFO**:双重 host 门控(reasoning extra_body + envelope 布局)→ 留真实后端阶段 |
| **RC-08** | F,S9 | 第三方 + `compression` + 大 filler | 压缩触发(预检日志 `📦 Preflight ~111005 ≥ 100000` 阈值;主请求消息数非单调 = 发生了 compaction);摘要辅助请求(非流式、单 user)留在 mock ✅ |
| **RC-09** | S13 | `inject_tier_429_once` | 收到 429 → 自动降 `context_length` + 压缩 + 重试(≥2 个请求)✅ |
| **RC-10** | S10 | `stop_reason_once:max_tokens` | 输出被截断后自动续写(≥2 个请求);system 跨续写稳定 ✅ |

> **RC-04 这组尤其能说明"真实 CLI 比 import-driver 忠实"**:import-driver 要靠 monkeypatch 把 `_supports_reasoning_extra_body` 强行改成 `lambda: True` 才能触发 reasoning 相关字段;而真实 CLI 只用 `agent.reasoning_effort` 这个配置项 + 真实模型名,就让 thinking 的五个分支(不同模型代际走 adaptive / 降级 / 手动预算等)**自然地触发**——不需要任何 hack,因此更接近线上真实行为。

---

## 4. 关键发现(逐条展开)

### 4.1 真实 CLI 的请求比 import-driver 丰富得多

import-driver 看到的是缩水版(0 工具、~1700 字符 system),真实 CLI 看到的是 **29 个工具 + ~16096 字符 system**(在 `toolsets:[]` 这个配置下的捕获值;含义见 §1 的 ⚠️——这是环境相关的量级,不是常量)。

**为什么这点对 KV 调度很重要**:推理框架在生产里收到的每个请求,前面都顶着这么一大段稳定的前缀(system + tools)。这正是 KV cache 最该复用的部分(所有同部署的请求都共享它)。如果你拿 import-driver 的缩水版去估算"前缀有多长、能省多少 prefill",会严重低估真实收益。所以做前缀命中/亲和性实验,**必须用真实 CLI 这条路拿到真实量级的前缀**。

### 4.2 上下文压缩的触发阈值有个"硬地板",而且配置会被模型元数据覆盖

Hermes 决定"什么时候该压缩历史"用的阈值是:
```
threshold_tokens = max(context_length × 0.5, 64000)
```
这里有两个容易被坑的点:
1. **64000 是硬地板**(`MINIMUM_CONTEXT_LENGTH=64000`):哪怕你把 `context_length` 配得很小,阈值也不会低于 64000。
2. **你配的 `context_length` 会被模型的真实窗口元数据覆盖**:比如配了个小值,但模型是 `claude-sonnet-4`(真实窗口 200000),Hermes 会用 200000 来算,于是阈值实际是 `200000 × 0.5 = 100000`,而不是你以为的值。

**对测试的含义**:import-driver 可以直接 monkeypatch 把阈值改小来"轻松触发压缩";但真实 CLI 没有这个后门,**只能老老实实让消息内容真的累积过 10 万 token 这个阈值**才会触发(这就是为什么 RC-08 要用大 filler + 多轮累积,见契约 6)。RC-08 的预检日志 `📦 Preflight ~111005 ≥ 100000` 正是越过阈值的实证。

### 4.3 host 门控是"真实约束"——这句到底在说什么(从零讲清)

这是报告里最容易让人一头雾水的一条,展开讲。

**第一步:什么是"host 门控"?**
Hermes 在往请求里塞某些 provider 专属的 agent hint **之前**,会先做一个检查:**这次请求要发往的 `base_url`,它的主机名(hostname)是不是某个特定域名?** 只有命中,才注入那个 hint;不命中,就当没这回事。
做这个判断的函数是 `base_url_host_matches(base_url, domain)`(`utils.py:358-376`),逻辑很简单:把 base_url 的 hostname 取出来,看它是不是 `== domain` 或者以 `.domain` 结尾。
所以"host 门控"= **用"请求发往哪个域名"当开关,来决定某些字段发不发。**

**第二步:它门控了哪些 hint?**(举具体例子)

| 被门控的 hint | 要求 base_url 主机名命中 |
|---|---|
| OpenRouter 的 `body.session_id`(会话亲和键) | `openrouter.ai` |
| OpenRouter 的 envelope 式 `cache_control` 布局 | `openrouter.ai` |
| grok 模型的 `x-grok-conv-id` 头(chat 路径) | `openrouter.ai` |
| reasoning 的 `extra_body`(`_supports_reasoning_extra_body@run_agent.py:4442`) | base_url 含 `openrouter` / `nousresearch.com` 等 |
| OAuth 身份头(Anthropic 路径相关) | base_url 含 `anthropic.com` 子串 |
| qwen 的 `metadata.sessionId` | `portal.qwen.ai` |

**第三步:为什么"localhost 测不到"?**
我们的 mock 跑在 `localhost`。`localhost` 这个主机名,既不是 `openrouter.ai`,也不含 `anthropic.com`。于是上面那些门控判断**全部不通过 → Hermes 干脆不注入这些 hint → 请求里压根没有它们 → mock 自然也就抓不到**。
这就是 RC-06 / RC-07(OpenRouter 场景)被标成 **INFO** 而不是 PASS 的原因:不是测失败了,而是**这些 hint 在 localhost 这个域名下根本不会被生成**,无从断言。

**第四步:为什么叫"真实约束",约束的是什么?**
"真实约束"是相对"能 mock 掉的假限制"说的。很多东西我们能在 mock 里随便伪造(usage 数字、SSE 节奏、错误注入);但 host 门控不行——它取决于"请求实际发往哪个域名",这是 Hermes 写死的真实行为,**不是改改 mock 就能绕过的**(你没法让 localhost 在 Hermes 眼里变成 openrouter.ai,除非真的去动 DNS / 主机名)。
所以它**约束的是"测试能力"**:这些 host-gated 的 hint,**你在本地 mock 上永远验证不了,只能在请求真的发往正确域名(如真实 openrouter.ai,或一个你让它叫这个名字的后端)时才能验证。** 这正是我们把它们做成 fixtures(§5)、留到"真实后端阶段"再回归的原因。

**补充一个相关细节**:为什么不能用 `custom_providers` 给 openrouter 套个本地 base_url 蒙混过关?因为 `provider=openrouter` 是**内置 provider**,它**不采用** `custom_providers` 里同名条目的 base_url override——它认死了真实的 openrouter.ai。所以真去跑 RC-06,请求会打到真实 openrouter.ai 拿 401(我们因此直接跳过执行,避免外部调用)。

**和 Anthropic 路径的关系**(回答你"Anthropic SDK 那块"):报告这句举的是 OpenRouter,但**同一套 host 门控机制也卡 Anthropic 路径**——(1)OAuth 身份头要 base_url 含 `anthropic.com` 才发;(2)更麻烦的是 native Anthropic 压缩的摘要辅助请求会**逃逸到真实 `api.anthropic.com`**(契约 5),localhost 也截不住。所以"Anthropic 路径在 localhost 测不全"是同一类约束的另一种表现。

**和你们自研全栈的关系**:报告 17 §4 专门重评估过——**你们自研整套后端后,这道约束会部分消解**:因为后端是自己的,可以让它的主机名/profile 满足门控条件,或者干脆在自有 profile 里把这道门放开,这些原本 localhost 测不到的 hint 就能正常采集和验证了。

### 4.4 `--pass-session-id` 是个"假"会话键

直觉上 `--pass-session-id` 像是"把 session_id 作为会话亲和键传给后端"。实测不是:它**只把 session_id 当文本拼进 system prompt**(`system_prompt.py:332`),**不进任何 header 或 body 的业务字段**。也就是说它对 KV-aware 路由没用——真正的会话键是 §4.3 里那些被 host 门控的字段。

### 4.5 多轮 `--resume` 走的就是 gateway 的真实路径

`--resume` 每一轮都会**新建一个全新的 `AIAgent`,再从 SessionDB 把历史还原回去**。这一点很重要:它和线上 gateway 的行为**一模一样**——gateway 对每条入站消息也是新建 agent + 还原历史。所以我们多轮测的不是某个 CLI 特例,而是生产路径本身。

---

## 5. 产物:真实后端测试台的输入(fixtures)

把上面捕获到的东西提炼成 `cli_platform/fixtures.json`,共 12 个 **后端无关(backend-agnostic)** 的 fixture。"后端无关"的意思是:它不锁死某个具体后端的实现细节,而是描述"一个正确的请求应该长什么样",带容差。每个 fixture 三部分:

- **触发**:指向 `driver_cli.py` 里的 config + 对应的 `hermes chat` 命令(怎么把这个请求复现出来)。
- **期望的 agent-hint 签名**:从捕获里提取的"应该出现哪些字段、什么形状"。
- **容差(tolerance)**:哪些地方不能逐字比、要按模式比。例如:
  - `session_id` / 时间戳:用正则 `^\d{8}_\d{6}_[0-9a-f]{6}$` 匹配(每次值都不同,但格式固定),并校验"跨轮恒定"。
  - system prompt:只校验**长度量级**和**跨轮字节稳定**,不逐字比对(里面含日期等会变的内容)。
  - tools:只校验**数量**和**关键工具子集在不在**,不锁死完整的 28/29 个(版本会变)。

**怎么用(到了真实后端阶段)**:把同一条 `hermes chat` 命令的 base_url 指向**真实推理后端**,再拿 fixture 的签名 + 容差去对照实际发出的请求——就能验证"真实后端是不是按预期收到了这些 agent hint",并且把 RC-06/RC-07 这些之前因 host 门控只能标 INFO 的项**补回归**(因为这时域名对了,门控通了)。

---

## 6. 局限与诚实声明

1. **RC-06/07 这两个 OpenRouter 场景在 localhost 测不了**(host 门控,§4.3),已标 INFO 并**跳过执行**(避免真去打 openrouter.ai)。它们不是"没验过",而是"本地验不了,等真实后端"。
2. **RC-08 的摘要辅助请求在批量跑里偶发抓不到**:compaction 一定会触发(这点稳定),但那个摘要辅助请求在批量跑中偶尔没被捕获;单独开 verbose 跑能稳定抓到它(特征:`/v1/v1/messages`、kind=summary、`stream=false`)。
3. **mock 的 usage/cache 数字是脚本编造的,不是真实缓存语义**:我们断言的是缓存断点的**位置**(真实),不对缓存**命中率**做断言(那是编的)。真实命中率要等接真实后端。
4. **复现命令**:`<venv-python> cli_platform/driver_cli.py 8920`(会自动起停 mock)→ 再跑 `check_cli.py`(断言)→ 再跑 `gen_fixtures.py`(生成 fixtures)。

> 配套阅读:报告 16(把这些信号映射到 Dynamo 风格 KV/调度的 G1/G2/G3 + 测试台架构)、报告 17(每个信号的逐条提取配方 + host 门控在自有全栈下的重评估)。
