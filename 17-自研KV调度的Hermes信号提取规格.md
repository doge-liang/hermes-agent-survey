# 17 — 自研 NeMo+Dynamo 的 Hermes 信号提取规格(实现输入)

> 基准:Hermes-Agent **v0.15.1**,真值源 = 已安装树 `/home/niaowuuu/.hermes/hermes-agent/`(version 0.15.1)。本报告所有 `file:line` 均经两轮回源码核验(见 §2 勘误)。
> **本文档定位**:不是"调研发现",是**实现输入规格**。前提是——我们的整个推理后端与 NVIDIA **不兼容**,NeMo Agent Toolkit + Dynamo **不能复用、要从零自研全栈**,这块成本不可省,且性能目标要比 NVIDIA 更极致。因此 NeMo/Dynamo 只作**契约参考**(哪些 hint 值得收、各驱动什么决策),真正可交付的是下面这张"**每个 Hermes 信号在哪、怎么取、什么保真度、什么成本**"的提取配方表——它直接决定我们自研采集层(NeMo-AT 等价物)与调度后端(Dynamo 等价物)怎么实现。
> 配套:报告 16(信号→G1/G2/G3 映射 + 测试台)、记忆 `dynamo-nvext-agent-hints-schema`(注入目标字段参考)。

---

## TL;DR(一段话读懂本报告)

我们要从零自研一套推理后端(NeMo Agent Toolkit + Dynamo 的等价物),agent 端跑的是 Hermes。要让后端做出好的 KV cache 管理与调度决策,就得先知道:**Hermes 在每次推理请求里到底带了哪些可供调度用的信息(agent hint)、这些信息在 Hermes 源码的哪一行、怎么取、取到的值有多准、取它要付多大改造成本。** 本报告把 29 个这样的信号逐一列成一张提取配方表,并按"怎么拿到"分成三类:**W**(信号就在出站 HTTP 请求上,代理被动读即可,零改 Hermes)、**S**(信号在 Hermes 内部对象属性上,要在进程内读 `agent.X`)、**H**(信号是稍纵即逝的局部变量/事件,必须在源码决策点埋一行 emit 才拿得到)。结论是约 2/3 的信号是 W,一个被动 egress 代理零改就能拿到主体;真正需要动 Hermes 的只是执行图、parent 血缘、重试计数等一小撮 H 信号。报告后半部分(§2)逐条勘误了报告 12–16 里的坐标/语义错误,(§3–§4)说明"自研全栈"相比 NVIDIA 通用方案多解锁了哪些能力、消解了哪些 localhost 测不到的门控限制。

> 名词速记(下文首次出现时还会再解释一遍):
> - **agent hint** = agent 随推理请求下发给后端、辅助后端做 KV/调度决策的元数据。
> - **import-driver** = 在 Python 里直接调 `AIAgent.run_conversation()` 来驱动 Hermes;**真实 CLI** = 起子进程跑 `hermes chat`。两者是本系列报告的两条验证路径。
> - **host 门控** = Hermes 会按请求 `base_url` 的主机名(函数 `base_url_host_matches`)决定某些 hint 发不发——主机名对不上就不注入。

---

## 0. 提取机制三分法(实现决策的核心轴)

每个信号按"怎么拿到"分三类,直接决定采集层的工程形态:

| 类 | 含义 | 工程形态 | 改 Hermes? |
|---|---|---|---|
| **W** wire-tap | 信号就在出站 HTTP 请求/响应上(wire = 上线的请求/响应字节) | egress 代理被动读 | **零改** |
| **S** 内部状态读取 | 信号在内部对象属性上(不上 wire、但持久可读) | in-process 读 `agent.X` | 零改源码,但须在 Hermes 进程内 / 可读其状态 |
| **H** emit hook | 信号是事件 / 局部变量,只在某决策点存在(事后取不到) | 在决策点插一行 emit | 最小改(一处埋点) |

这里 **egress 代理**指架在 Hermes 与后端之间、被动旁路出站流量的代理;**stateful** 表示它要跨请求记住会话状态(如前缀、会话键)才能产出有用信号。**in-process 读**指采集代码与 Hermes 跑在同一进程、能直接访问 `agent` 对象的属性。**emit hook** 指在 Hermes 源码的某个决策点手工插一行"把当前值发出去"的埋点代码。

**总览结论**:29 个信号里,**W 占多数(约 2/3)→ 一个 stateful egress 代理零改就能拿到主体**;真正需要 S/H 的,是执行图 / 委派、parent 血缘、重试计数、压缩判定的内部量、iteration 预算、Todo、gateway 签名这一小撮——它们恰好是报告 16 §2.4 "需新增 instrumentation" 的那半。**自研全栈下,这半也由你决定埋点,不受 host 门控约束(见 §4)。**

---

## 1. 信号提取配方总表

下面两张表是本报告的主体。每行一个信号,列含义如下:**确切 file:line** = 该信号在 v0.15.1 源码里的精确坐标;**持有它的代码/字段** = 运行时这个值落在哪个变量/请求字段上;**类** = W/S/H 三分;**保真度** = 取到的值有多准(精确 / 估计 / 配置常量);**关键路径** = 是否每次推理都用得上(影响优先级);**更新频率** = 这个值多久变一次(决定采集层要不要每请求重算);**最小提取接缝** = 实际动手时从哪儿读最省事。读表前建议先看 §1.3 的分布小结,知道整体哪些零改可采、哪些要埋点。

### 1.1 Wire 层信号(W 为主,egress 代理零改可采)

这一组信号全部直接出现在出站 HTTP 请求上,所以一个被动代理读 `body.*` / 请求头就能拿到,无需改 Hermes 源码。

| # | 信号 | 确切 file:line | 持有它的代码/字段 | 类 | 保真度 | 关键路径 | 更新频率 | 最小提取接缝 |
|---|------|---------------|------------------|---|--------|---------|---------|------------|
| W1 | System prompt 全量 | 构建 `agent/system_prompt.py:347-363`(`build_system_prompt`,三段拼接 341-343);放置 chat=`messages[0]` / anthropic=`body.system`(`agent/anthropic_adapter.py:2041-2080`)/ codex=`body.instructions`(`agent/transports/codex.py:96-100`) | `agent._cached_system_prompt`(stable/context/volatile 三段) | W | 精确 | 是 | 会话级恒定(仅 `invalidate_system_prompt` 重建) | chat 读 `messages[0].content` / anthropic 读 `system` / codex 读 `instructions` |
| W2 | Tools 定义 | 组装 `agent/agent_init.py:907`(`get_tool_definitions`);实现 `model_tools.py:264,337`;核心名单 `toolsets.py:31`(`_HERMES_CORE_TOOLS`) | `agent.tools`(OpenAI-format list) | W | **估计(随环境/配置)** | 是 | 会话级恒定 | 读 `body.tools` 长度。**见 §2.1:数量环境相关,不可硬编码** |
| W3 | bit-perfect 前缀规范化 | `agent/conversation_loop.py:1053-1078`(函数 `run_conversation@:351`);surrogate 清理 `:1084` | `content.strip()`+`json.dumps(sort_keys=True,separators=(",",":"))`,作用于 `api_messages` 副本 | W | 精确 | 是 | 每请求 | 出站 `messages` 即已规范化形;`:1084` 后读 `api_messages` |
| W4 | body.session_id(openrouter) | `plugins/model-providers/openrouter/__init__.py:42-47`(`build_extra_body`);合并 `chat_completions.py:544-553→573-574` | `extra_body.session_id`(provider=openrouter 且非空) | W | 精确 | 否(缓存路由) | 会话级恒定 | 读 `body.session_id` |
| W5 | codex 会话键 | header `session_id`=`codex.py:234`;`x-client-request-id`=`codex.py:235`;body `prompt_cache_key`=`codex.py:159`;header `x-grok-conv-id`=`codex.py:253`;xAI body `prompt_cache_key`=`codex.py:264` | `extra_headers` / `prompt_cache_key` / `extra_body`;门控 `is_codex_backend@220`、`is_xai_responses@242` | W | 精确 | 否 | 会话级恒定 | 读对应 header/body 键 |
| W6 | qwen body.metadata.sessionId | dict 构建 `chat_completion_helpers.py:690-695`;顶层注入 `plugins/model-providers/qwen-oauth/__init__.py:62-72` | `api_kwargs["metadata"]`(**顶层非 extra_body**);门控 host==portal.qwen.ai | W | sessionId 精确 / promptId 每请求随机 | 否 | sessionId 会话级 | 读 `body.metadata.sessionId` |
| W7 | cache_control 断点+TTL | 布局 `agent/prompt_caching.py:70-77`(system@70-72,`remaining=4-used`@74,`non_sys[-remaining:]`@76-77);marker/TTL `:41-46`(`ttl=="1h"` 才加 `ttl`);native/envelope 由 `_apply_cache_marker(native_anthropic=…)`@15-38 | system_and_3 = system + 最后 3 条非 system,共 ≤4 断点同 TTL;cfg `cache_ttl` 默认 "5m" | W | 精确 | 是(Claude/Anthropic 协议路径) | 每请求(断点位置随 messages 重算) | 读 `system`/`messages[*].content[-1].cache_control` |
| W8 | anthropic-beta 头 | `anthropic_adapter.py:539-568`(`_common_betas_for_base_url`);常量 `_COMMON_BETAS@261-264`、`_CONTEXT_1M_BETA@271`;落头 `:639/724/735`;另 `agent_init.py:849-854` | interleaved-thinking + fine-grained-tool-streaming 恒含;context-1m / MiniMax 剥离 / `drop_context_1m_beta` 门控 | W | 配置常量 | 是 | 会话级恒定 | 读请求头 `anthropic-beta` |
| W9 | thinking/reasoning | anthropic `anthropic_adapter.py:2248-2269`(`thinking.budget_tokens@2266`、`output_config.effort@2262-2264`);预算表 `THINKING_BUDGET@:58`(high=16000);chat `extra_body.reasoning`(profile 注入如 `openrouter/__init__.py:86-90`→合并 `chat_completions.py:556-557`);codex `body.reasoning`=`codex.py:177/184`;门控 `_supports_reasoning_extra_body`=`run_agent.py:4442-4482` | 源自 `agent.reasoning_config`(effort) | W | budget 配置常量 / effort 精确 | 是 | 会话级恒定 | anthropic 读 `thinking`/`output_config`;chat 读 `extra_body.reasoning`;codex 读 `reasoning` |
| W10 | max_tokens 解析 | profile 路径 `agent/transports/chat_completions.py:507-524`(ephemeral>user>`profile.get_max_tokens(model)`);默认 `agent_init.py:457`(`= None`) | `api_kwargs["max_tokens"]`;**无统一默认,None 时整键缺省** | W | 精确 | 是 | 会话级恒定 | 读 `body.max_tokens`(可能缺省)。**见 §2.1:非 64000** |
| W11 | stream + stream_options | `chat_completion_helpers.py:1706`(`stream:True`)+`:1707`(`stream_options.include_usage:True`) | API kwargs 字面量 | W | 配置常量 | 是 | 每请求恒定 | 读 `body.stream`/`body.stream_options` |
| W12 | host 门控判定 | `utils.py:358-376`(`base_url_host_matches`:`hostname==domain or endswith("."+domain)`);调用点 openrouter `agent_init.py:715`、qwen `:731`、nous/github `run_agent.py:4449-4453`、bedrock `:316` | 纯判定函数(本身不上 wire,决定 W4/5/6/8/9 是否注入) | W(可由 base_url 复算) | 精确 | 是 | 会话级恒定 | 对出站 base_url 复算同一函数即知门控分支 |

### 1.2 内部状态/事件层信号(S/H,需 in-process 读或 emit 埋点)

这一组要么是 Hermes 内部对象上的状态(S,代理看不见但持久可读),要么是只在某个决策瞬间存在的局部变量/事件(H,事后取不到、必须在源码里埋点)。注意有几行标了"W / S"或"W / H":意思是这个信号在 wire 上有可观测的"侧影"(比如压缩发生时 messages 会突然变短),但要拿到精确内部量仍需 S 读对象或 H 埋点。

| # | 信号 | 确切 file:line | 持有它的代码/字段 | 类 | 保真度 | 关键路径 | 更新频率 | 最小提取接缝 |
|---|------|---------------|------------------|---|--------|---------|---------|------------|
| I1 | messages 累积/增量前缀 | `conversation_loop.py:499`(`messages=list(conversation_history)`)+`:566`(append user)+投影 `api_messages@945-1045` | 局部 `messages`(累积超集) | W(长度/内容上 wire)/ S | 精确 | 是 | 每轮 append / 每请求重建 | wire 读 `body.messages` 长度;或 `:801` 顶读 `len(messages)` |
| I2 | 工具结果确定追加顺序 | `tool_executor.py:548`(收集)+`:668`(`messages.append(tool_result)`) | `parsed_calls`(保原序@201-341)+ `results[index]`(@379/442/452) | W / S | 精确 | 是 | 事件性(每 tool batch) | wire 上顺序确定;或读 `results` 数组 |
| I3 | tool 完成→下一 prefill 边界 | 主循环 `while@:801`;tool 执行 `_execute_tool_calls@:3965`;dispatcher `run_agent.py:4770` | 局部 `api_call_count`(@729 init,@813++);tool 路径无 `break` 回落 `:801` | H(或由响应含 tool_calls 推断) | 精确 | 是 | 每请求(每循环=1 call) | 响应含 tool_calls ⇒ 下次 prefill 必来;或 hook `:801` 循环顶 |
| I4 | turn/iteration 预算剩余 | 默认 `max_iterations=90`@`run_agent.py:353`;`IterationBudget@agent/iteration_budget.py:17`(`consume/refund/used/remaining@37-59`) | `agent.iteration_budget`;`agent._api_call_count`(@814) | S | 精确 | 是 | 每请求 | 读 `agent.iteration_budget.remaining`+`_api_call_count` |
| I5 | Todo/Plan 活跃项 | `TodoStore@tools/todo_tool.py:25`(`_items@:35`);hydrate `conversation_loop.py:504-505`;实例化 `agent_init.py:1042` | `agent._todo_store._items: List[Dict]`(有序) | S(或 hook write) | 精确 | 否(辅助) | 事件性/会话级 | 读 `agent._todo_store.read()`;或 hook `TodoStore.write` |
| I6 | delegate_task 子图边界 | 入口 `run_agent.py:4793`(`_dispatch_delegate_task`);child 构建 `tools/delegate_tool.py:870`;child 跑 `:1523` | child `AIAgent(max_iterations,parent_session_id)`@1114/1129;子默认 `DEFAULT_MAX_ITERATIONS=50`@512 | H(进/出) | 精确 | 是(委派时) | 事件性 | `_build_child_agent` 返回处埋"进";`_run_single_child` 返回处埋"出" |
| I7 | 压缩阈值公式 | `context_compressor.py:626-628`(init)+`:572-574`(update);`MINIMUM_CONTEXT_LENGTH=64_000@model_metadata.py:133` | `threshold_tokens=max(int(ctx*0.50),64000)`;ctx 来自 `get_model_context_length()@:616`(被模型元数据覆盖) | S | 精确 | 是 | 会话级(模型切换 update) | 读 `agent.context_compressor.{threshold_tokens,context_length}` |
| I8 | 抖动门控+反抖动 | `tolerated_growth=max(4096,int(threshold*0.05))@:721`;反抖动 "saved<10%×2"@`:732-742`(`_ineffective_compression_count>=2`) | `should_defer_preflight_to_real_usage()@:698`;`should_compress()@:728` | H | 精确 | 是 | 事件性(每次压缩判定) | hook `should_compress` / `should_defer_preflight_to_real_usage` 返回 |
| I9 | 压缩两阶段 preflight+real-usage | 阶段1 `conversation_loop.py:604-690`;阶段2 `update_from_response()@context_compressor.py:684-696`(`awaiting_real_usage_after_compression=False@696`);post-response 压缩 `conversation_loop.py:4011-4050` | `compressor.awaiting_real_usage_after_compression`;`last_real_prompt_tokens` | H | 精确 | 是 | 事件性 | hook `update_from_response`;读 `awaiting_real_usage_after_compression` |
| I10 | summary 辅助请求 | 提示 "You are a summarization agent…"@`context_compressor.py:1254`;kwargs@`:1380-1391`;发送 `call_llm(task="compression")@:1394` | `max_tokens=int(summary_budget*1.3)@:1390`,非流式/单 user/无 tools | W(出站独特签名)/ H(参数) | 精确 | 否(辅助模型) | 事件性 | 代理被动认签名;或 hook `_generate_summary@:1217` |
| I11 | 压缩后前缀重写 | `SUMMARY_PREFIX="[CONTEXT COMPACTION — REFERENCE ONLY]…"@:37-38`;`protect_first_n=3@:589/606`;`tail_token_budget=int(threshold*0.20)@:645-646`;`protected_count=max(budget_protect_count,min_protect)@:827` | `compressor.{protect_first_n,tail_token_budget}`;`_with_summary_prefix()@:1534` | W(SUMMARY_PREFIX 上 wire)/ S | 精确 | 是 | 事件性 | wire 见 SUMMARY_PREFIX+messages 突降;或读 `compressor.*` |
| I12 | session 轮换+parent_session_id | `conversation_compression.py:507-523`;new id `:509`(`{datetime}_{uuid.hex[:6]}`);`create_session(parent_session_id=old)@:517-523` | `agent.session_id`(旋转);DB `parent_session_id` 字段 | W(新 sid 上 wire)/ H(parent 链) | 精确 | 是 | 事件性(压缩时) | wire 见 session_id 变;parent 链须在 `:508-509` 埋 emit |
| I13 | stop_reason=length 续写链 | 块起 `conversation_loop.py:1628`;cap `length_continue_retries<3@:1732`(init@734,++@1726);boost `:3521-3522`(文本)/`:1811-1817`(截断工具调用);Anthropic 映射 `transports/anthropic.py:175,178` | 局部 `length_continue_retries`、`truncated_response_parts`;`agent._ephemeral_max_output_tokens` | W(续写请求上 wire)/ S(计数) | 精确 | 是 | 事件性(截断时) | wire 见续写;计数 hook `:1726` |
| I14 | tier 429 + thinking-sig 400 重试 | thinking-sig `conversation_loop.py:2544-2562`(one-shot);分类 `error_classifier.py:549-562`(返回@559);tier 429 `error_classifier.py:564-571`+`conversation_loop.py:2730` | `FailoverReason.{thinking_signature,long_context_tier}`(enum@24) | W(429+重试 body)/ H(分支) | 精确 | 是 | 事件性 | hook `classify_error` 返回 `.reason`;或 `:2544`/`:2730` |
| I15 | 重试计数/退避状态 | 局部 `retry_count`(init@1139,++@1464);N 个 `*_retry_attempted` bool@1140-1163;backoff `jittered_backoff(retry_count,5,120)@:1561`;header 解析 `rate_limit_tracker.py:92` | 全是 `run_conversation` **局部变量(非持久)** | H | 精确 | 是 | 事件性 | **必须在 `:1464`/`:1561` 决策点 emit,事后取不到** |
| I16 | usage 三桶 | 下行 `conversation_loop.py:1881`(`normalize_usage`);桶@`:1897-1900`;会话累加@`:1920-1924` | `canonical_usage.{input_tokens,cache_read_tokens,cache_write_tokens}`(**叫 cache_write 非 cache_creation**) | W(响应上)/ S(规范化后) | 精确 | 是 | 每请求 | 响应 usage;或 hook `normalize_usage` / 读 `agent.session_*_tokens` |
| I17 | gateway session_key+config_signature | `_agent_cache:OrderedDict@gateway/run.py:1917-1925`;reuse 判定 `:17651`+`:17665-17666`;写 `:17713`;sig 函数 `:16055`;`build_session_key@gateway/session.py:600` | `self._agent_cache`(LRU);`config_signature_str`(变即解亲和) | S | 精确 | 是(gateway) | 每消息 | hook `:17651-17666` 命中/未命中,读 `session_key`+`_sig` |

### 1.3 提取机制分布(给采集层选型)

- **纯 W(零改代理可采)**:W1–W12 全部 + I1/I2/I10/I11/I12(新 sid)/I13(续写)/I14(429)/I16 的 wire 侧。→ **一个 stateful egress 代理就能拿到主体信号**(共享前缀、会话键、cache_control、reasoning、max_tokens、stream、压缩发生/续写/429 的可观测面、usage)。
- **S(需 in-process 读内部对象)**:I4(iteration 预算)、I5(Todo)、I7(压缩阈值/context_length)、I11(精确 protect/tail 参数)、I17(gateway 签名)。→ 代理看不到,需在 Hermes 进程内读 `agent.X`(零改源码,但要能触达对象)。
- **H(需埋 emit hook,事后取不到)**:I3(prefill 边界,可由响应推断弱化为 W)、I6(delegate 进出)、I8(压缩判定内部量)、I9(两阶段切换)、I12(parent 链)、I14(失败分类分支)、**I15(重试计数——局部变量,最硬)**。→ 这些是报告 16 §2.4 "需新增 instrumentation" 的精确落点。

---

## 2. 关键坐标/事实勘误(对报告 12–16 的透明修正)

回源码两轮核验,以下与报告 12–16(及部分记忆)声明不符,**以本报告为准**:

### 2.1 重要语义修正

1. **`toolsets:[]` 与工具数 — 需精确区分两层**:
   - **import 级** `get_tool_definitions(enabled_toolsets=[])` → **0 个工具**(`model_tools.py:347` 门控 `if enabled_toolsets is not None`,空列表 `[]` ≠ None,进入对空列表循环 → 空);默认 `None`(全开)→ ~33 个。
   - **真实 CLI 级**(报告 15 cli_platform,config `toolsets: []`)→ HTTP 出站**实测 29 tools**(authoritative 实测,成立)。
   - **结论**:config 层的 `toolsets: []` **≠** 内部 `enabled_toolsets=[]`——CLI 把空配置转换后仍带核心工具(`_HERMES_CORE_TOOLS@toolsets.py:31`)。报告 15"29 tools"作为**真实 wire 实测**成立;但"`toolsets:[]` 仍 29"这句**因果表述不严谨**(空配置 ≠ 内部空列表)。**提取规格唯一安全做法:tools 数环境/配置相关,一律从 wire 读 `body.tools` 长度,绝不硬编码 29/33。** (config→enabled_toolsets 的确切转换点待单独定位,不影响提取结论。)
   - 同理"即便 toolsets:[] 也发 16096 字符 system":~16096 是**默认全开 + 含 AGENTS.md 等上下文文件**的量级;显式内部空工具集会让大量 `if agent.valid_tool_names:` 门控段落跳过,system 显著变短。**system 长度同样从 wire 读,不假设固定 16096。**

2. **codex `prompt_cache_key` 行号修正**:报告 16 称 `codex.py:253`。实际 `prompt_cache_key=session_id` 在 **`codex.py:159`**;`:253` 是 **`x-grok-conv-id` 头**;`:264` 是 **xAI body 级 `prompt_cache_key`**(`extra_body.prompt_cache_key`)。header `session_id`(:234)/`x-client-request-id`(:235)正确。

3. **`run_agent.py` 路径修正**:在**仓库根 `run_agent.py`**,不是 `agent/run_agent.py`。行号(`:353` max_iterations=90、`:4793` delegate、`:4442` `_supports_reasoning_extra_body`、`:4770` dispatcher)本身正确,仅目录名错。

4. **summary 请求 `max_tokens` 修正**:不是固定 2000。实际 `max_tokens=int(summary_budget*1.3)@context_compressor.py:1390`,其中 `summary_budget=max(2000, min(content*ratio, max_summary_tokens))`;`2000` 是下限常量 `_MIN_SUMMARY_TOKENS@:85`。

5. **usage 桶命名**:Hermes 内部叫 **`cache_write_tokens`**,不是 `cache_creation`;解析入口 `normalize_usage()→canonical_usage@conversation_loop.py:1881`,非裸读响应 `usage` dict。

### 2.2 行号微调(逻辑无误,坐标偏移)

6. bit-perfect 规范化:实际 `conversation_loop.py:1053-1078`(报告 16 写 1047 是注释起点),surrogate 清理 `:1084`,函数名 `run_conversation@:351`。
7. cache_control 布局:`remaining = 4 - breakpoints_used`,仅当 system 断点被用才是 `non_sys[-3:]`,无 system 时 `non_sys[-4:]`;native/envelope 是 `_apply_cache_marker(native_anthropic=…)` 参数(非独立行号分支)。
8. 压缩两阶段:`:3965` 是 `_execute_tool_calls`(非 awaiting_real_usage);real-usage 阶段在 `update_from_response()@context_compressor.py:684-696`;`:639` 仅 preflight log。
9. 续写 boost:`:3521-3522`(文本)/`:1811-1817`(截断工具调用),不在续写循环主体 `1628-1770` 内。
10. qwen `sessionId` 进**顶层 `body.metadata`**(非 extra_body),`metadata` 另含每请求随机 `promptId`(uuid4)。
11. openrouter `x-grok-conv-id` 有**两条独立注入路径**:Responses 走 `codex.py:253`;chat 走 openrouter profile `__init__.py:93-94`。
12. gateway:reuse 决策块 `17648-17716` 正确,但 LRU 字典在 `:1917-1925`、sig 函数 `:16055`、`build_session_key` 在 `gateway/session.py:600`;`gateway/run.py` 共 19738 行。
13. Todo:`TodoStore` 类起 `:25`(报告写 22 偏 3 行),hydrate `:504` 正确;`_hydrate_todo_store@run_agent.py:2945`。

### 2.3 核验为真(无需改)

压缩阈值 `max(ctx*0.5, 64000)` 公式与 `MINIMUM_CONTEXT_LENGTH=64000@model_metadata.py:133`、`tolerated_growth=max(4096,threshold*0.05)`、`protect_first_n=3`、`tail_token_budget=threshold*0.20`、SUMMARY_PREFIX 文本、`THINKING_BUDGET` high=16000、stream_options 行号 1707、`base_url_host_matches` 语义、max_tokens **无统一默认(非 64000)**、`_supports_reasoning_extra_body@run_agent.py:4442` —— 均与源码一致。

---

## 3. 自有全栈相对 NVIDIA 解锁了什么

NVIDIA 的 agent hint 是 **advisory/best-effort**(仅供参考、尽力而为):后端可以采纳也可以忽略,因为这套 hint 必须对任意 harness(任意上层 agent 框架)+ 任意后端都通用,谁也不能假设对方一定按约定行事。我们自研全栈、agent 端就是 Hermes(及其同类),约束完全不同——下面这几条 NVIDIA 做不了而我们能做,且全部锚定在 §1 已核验的信号上:

| 解锁项 | NVIDIA 受限于 | 我们能做(锚定信号) |
|---|---|---|
| **hint 从「建议」升「权威」** | 通用 harness,hint 只能当估计 | agent 与调度器协同设计:W7 cache_control、I12 parent 链、I9 压缩两阶段作为**确定指令**而非提示 |
| **不绑 nvext-on-OpenAI-wire** | 必须塞进 OpenAI body 的 nvext | 传输自定义(side-channel / gRPC metadata / 共享内存);W/S/H 信号经我们自己的通道下发 |
| **prefix 块按真实 tokenizer 对齐** | 后端各异,只能近似 | W3 bit-perfect 规范化 → 我们用**自己后端的 tokenizer 算真块哈希**,消掉报告 16 的 B2 局限 |
| **Hermes 确定性当硬保证** | 无法假设任意 agent 行为 | W3 bit-perfect → 精确 prefix key;I2 工具结果确定序 → **精确**增量 prefill(非估计);I11 SUMMARY_PREFIX → **硬 KV 失效指令**;I12 session 轮换+parent → 显式 **KV 交接** |
| **decode 长度近硬上界** | 只能 learned osl 估计 | W9 thinking `budget_tokens` + W10 max_tokens(实际值)→ 近硬上界,配协同预测逼近精确 |
| **G3 闭环真能建** | 命中回填通道是第三方缺口 | I16 usage 三桶下行 + 我们自有后端 → **命中回填消费通道自己建**,SLA correction-factor 闭环成立(报告16 §2.4 #8 由"缺口"变"可建") |
| **执行图先验** | 无 agent 内部可见性 | I3 prefill 边界 + I4 iteration 预算 + I5 Todo + I6 delegate → 自埋 emit 得**真执行图**,不靠推断 |

**一句话:NVIDIA 的 agent hint 是我们的地板,不是天花板。** 地板的字段集(priority/osl/speculative_prefill/cache_control)照抄即可立即拿 4×TTFT 量级收益;天花板在"把 Hermes 确定性信号当权威指令 + 自有后端闭环"。

---

## 4. host 门控重评估(自有后端后哪些限制消解)

报告 14–16 里多处出现"localhost 测不到某信号",根因是**当时不控制后端**:host 门控(W12 的 `base_url_host_matches`)要求请求的 `base_url` 主机名命中 openrouter.ai / anthropic.com 这类真实域名,Hermes 才注入对应 hint;指向 localhost 就被门控拦下、信号根本不上 wire。**自研全栈后,门控的性质变了**(因为"真实后端"现在就是我们自己的后端):

| 门控信号 | 旧限制(不控后端) | 自有后端后 |
|---|---|---|
| W4 body.session_id(openrouter profile) | 需 host==openrouter.ai,localhost 触发不了 | **消解**:我们的后端就是"openrouter 等价物",可让 Hermes 走自定义 profile 注入,或后端直接接受 |
| W5 codex 会话键 | 需 is_codex_backend / chatgpt.com | **消解**:后端声明为 codex-compatible 或自定义,键照收 |
| W8 anthropic-beta / W9 reasoning extra_body | 需 openrouter/nous/anthropic.com 子串 | **部分消解**:门控在 Hermes 侧(`run_agent.py:4442` 等),仍需让 base_url 命中或改 profile;但我们可在自有 profile 里放开 |
| I10 native summary 逃逸真实 api | native anthropic 压缩 summary 打真实 api.anthropic.com | **消解**:base_url 指我们后端,summary 不再逃逸 |

**含义**:报告 16 把 RC-06/07 等标 INFO "留真实后端阶段"——对我们,**"真实后端"就是自研后端**,这些门控项在自有 profile + 自有后端下都可验、可采。门控真正剩下的硬约束只是"Hermes 内部 `_supports_*` 判定函数是否放行",而那是我们可以在自有 profile 配置里满足的。

---

## 5. 实现路线建议(采集层 → hint 通道 → 调度后端)

1. **采集层分两件,按 W/S/H 落地**:
   - **W → stateful egress 代理(先做,零改 Hermes)**:拿全 §1.3 的 W 主体。代理还能像 NVIDIA 说的那样**按 tool-call type 在线学习 osl**(它看得见响应 token 数,见 I16)。
   - **S → in-process 读**:I4/I5/I7/I11/I17,在 Hermes 进程内读 `agent.iteration_budget`/`_todo_store`/`context_compressor`/gateway `_agent_cache`。
   - **H → emit 埋点(最小改,按 §1.2 给的 file:line)**:I6/I8/I9/I12/I14/**I15**。重试计数 I15 是局部变量,**必须在 `:1464`/`:1561` 决策点 emit**,这是唯一拿得到的时机。
2. **每目标最小采集集**(对齐报告 16 的 MUST):
   - **G1(KV 主动管理)**:W1/W2/W3(可 pin 前缀)+ W7(断点/TTL)+ I11(SUMMARY_PREFIX 失效点)+ I7(压缩临界)→ 全是 W/S,可早交付。
   - **G2(状态感知调度)**:W4/W5(会话键)+ I1/I2(增量前缀)+ I3(prefill 边界)+ W9/W10(decode 上界)+ I4/I5/I6(执行图)→ 执行图部分需 H。
   - **G3(SLO 感知)**:W11(include_usage)+ I16(usage 桶/命中回填)+ I8/I9(压缩判定)+ I13/I14/I15(续写/重试)+ W7 TTL → 闭环依赖 I16 自建回填(§3)。
3. **hint 通道**:不绑 nvext;字段语义对齐 Dynamo 参考(priority/osl/speculative_prefill/cache_control)便于迁移,但传输与 schema 自定义。
4. **闭环验证**:用报告 16 的测试台(hint-ON vs hint-OFF A/B)量化每个信号的 TTFT/吞吐增量,**先证明值得,再决定哪些 H 埋点要进生产**。

---

## 6. 诚实边界 / 待实测

1. **tools/system 量级一律 wire 读**:§2.1 已说明 config `toolsets:[]` ≠ 内部空集;config→enabled_toolsets 的确切转换点(CLI 如何把空配置变成核心工具集)**待单独定位**,但不影响"从 wire 读"的提取结论。
2. **S 类需触达 Hermes 对象**:I4/I5/I7/I11/I17 代理拿不到,需 in-process。若采集层不在 Hermes 进程内,这些要么降级为 wire 推断(更粗),要么也走 emit hook。
3. **H 类局部变量事后不可取**:I15 重试计数、I3 prefill 边界等是 `run_conversation` 局部态,**只能在决策点 emit**;选了纯代理路线就拿不到精确值,只能从 wire 弱推断。
4. **保真度分级**:W2(tools 数)、W9 budget(配置常量非真实 decode 长度)是"估计/常量"——当 osl 先验用要标"上界非预测"。
5. **gateway 路径**:I17 仅在经 gateway(多平台入站)时存在;纯 CLI/import 路径无 `_agent_cache` 复用语义。
6. 本报告坐标基于 v0.15.1;Hermes 升级后须重核(已有 v0.13→v0.15 漂移前例)。
