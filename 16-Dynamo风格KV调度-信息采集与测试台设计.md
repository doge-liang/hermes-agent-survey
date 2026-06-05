# 16 — Hermes-Agent → Dynamo 风格 KV/调度:信息采集规格 + 测试台设计

> 基准:Hermes-Agent **v0.15.1**(真实安装树 `~/.hermes/hermes-agent/`,非源码树)。
> 三目标定义:**G1** = KVCache 主动管理(preload/prefetch + 精准 evict);**G2** = Agent 状态感知调度(执行图 + prefetch/offload overlap + decode 长度先验 + DP 均衡);**G3** = SLO 感知调度(有/无 hint 下在 swap/pin/recompute 间启发式选择)。
> 三态采集方式:**HTTP可见**(框架在入站 header+body 即可读,零改 Hermes)/ **需 hook**(信号在 Hermes 内部、当前不下发)/ **框架自算**(Hermes 不发但框架可从已可见信号推导)。
> 方法:ultracode 动态 Workflow(4 路调研→3 路设计→综合→对抗性回源码核验)。本报告为**经对抗评审修正后的最终版**;评审改动汇总见 §7。

---

## 0. TL;DR

1. **能零改 Hermes 拿到的信息已经足够启动 G1/G2**:稳定共享前缀(16096-char system + 29 tools)、bit-perfect 前缀规范化、会话亲和键(`session_id`/`prompt_cache_key`/`x-grok-conv-id`,但**强 host/provider 门控**)、cache_control 断点(`system_and_3`,详 §2.1③)、压缩后前缀重写(SUMMARY_PREFIX = KV 中段失效点)、reasoning/thinking decode 先验(`budget_tokens=16000` 等)全是 HTTP 可见。
2. **G3 的核心是「有 hint vs 无 hint」两条基线对照**,而 Hermes 里这条线基本由 **provider/profile** 切换:custom/官方 OpenAI/localhost 几乎无会话键、无 cache_control、无 reasoning 先验;OpenRouter/Codex/xAI profile 才解锁全套 hint。
3. **Hermes v0.15.1 当前不下发** 8 类关键信号(全仓 grep `predicted/decode_length/expected_output` 零命中):显式 decode 长度预测、执行图/steps-to-execution、parent_session_id、压缩阈值/预警、重试计数、turn 阶段、custom 路径会话键、缓存命中回填消费通道——这些需新增 `x-hermes-*` 旁路 header 或 `build_extra_body` 注入。
4. **测试台分两段真值域,任何指标必须标注来源**:**MOCK 段**只能回答「信号是否齐全、能否驱动正确调度决策」;**REAL 段**(Dynamo+vLLM/SGLang)才能给真实 TTFT/ITL/吞吐/SLO 达成率。⚠️ 经评审:即便 MOCK「逻辑产物」类指标(块命中率/preload 命中/evict 误杀/DP 不均衡)也**条件依赖**自建路由假设、合成 hint、近似 tokenizer——只作 MOCK 内 A/B 相对比较,不可跨段绝对对照(详 §3.0、§5)。
5. **架构 = 5 层**:负载生成(并发多会话)→ 信号采集(扩展 mock `_record`/`/__mock/control`,补纳秒到达时戳+prefix-block hash+session/turn+事件标记)→ Trace→调度器回放(自建 KV 模拟器先行,真实后端后行)→ A/B 框架(同一 trace × {hint-ON, hint-OFF})→ 指标层(按真值域分栏)。
6. **并发是 KV 复用/抢占/排队唯一出现的条件**;单会话 trace 是 N=1 退化,无法验证 G1/G3。
7. **多 wire mock 是必需,非可选**(评审硬点):现有 `mock_anthropic.py` 只懂 Anthropic wire,而 G2/G3 的会话键(`prompt_cache_key`/codex header `session_id`/`x-grok-conv-id`)全在 **codex(Responses)/chat wire** → 「有 hint 基线」的关键信号在 Anthropic mock 上根本采不到。Responses/Chat wire mock 成熟度是 P1 阻塞项。
8. **分阶段路线**:P0 自建模拟器验决策正确性(全 MOCK,合成 hint 注入器代偿 8 类缺口)→ P1 补并发+纳秒时序+prefix-hash+可参数化 decode 时延模型+多 wire mock → P2 接真实后端取所有时延/吞吐/SLO 真值并激活 host 门控信号。
9. **诚实纪律**:任何来自 MOCK 段的 TTFT/ITL/吞吐数字必须带 `simulated=true` 标,不得对外当性能结论;MOCK 段对外结论仅限「信号齐全 + hint 能驱动出更优调度**决策**」。

---

## 1. 背景与映射:Dynamo 三目标 ↔ Hermes 已实测 agent hints

| Dynamo 目标 | 子能力 | Dynamo 承接字段/机制 | Hermes 已实测对应信号(信号组) | 采集方式 | 缺口 |
|---|---|---|---|---|---|
| **G1** KVCache 主动管理 | pin 持久前缀 | `cache_control.ttl` / KvIndexer prefix tree 顶层 | system(16096B)+ tools(29 个)稳定前缀;bit-perfect 规范化;cache_control{ttl:5m/1h}(①③) | HTTP可见 | 无 |
| | prefetch/preload | `agent_hints.speculative_prefill` | 「工具完成→下一次 prefill」确定边界;messages 单调累积(④) | 需 hook(边界)/ HTTP可见(累积) | 边界事件不下发 |
| | 精准 evict | steps-to-execution 驱逐(KVFlow 范式,Dynamo 无,需自建) | 压缩后前缀重写 SUMMARY_PREFIX(中段失效点);system_and_3 旧断点弃用(⑤③) | HTTP可见 | 执行图/parent 链不下发 |
| **G2** Agent 状态感知调度 | 会话亲和(DP rank) | KV-aware sticky routing key | `session_id`/`prompt_cache_key`/`x-grok-conv-id`/header session_id;gateway `session_key`+`config_signature`(②) | HTTP可见(门控)/ 需 hook(gateway) | custom 路径无键 |
| | 增量 prefill | 前缀超集复用 | messages N 轮 = N-1 超集;工具结果追加顺序确定(④) | HTTP可见 | 无 |
| | decode 长度先验 | `agent_hints.osl` / planner `next_osl` | thinking budget_tokens、output_config.effort、reasoning effort(⑥);**max_tokens 是上界非预测,且按 profile 解析无统一默认** | HTTP可见(上界,非预测) | 无显式预测 |
| | 执行图/overlap | steps-to-execution(自建) | 29 tools 静态封闭;Tool Search 三段式;Todo 软执行图;delegate 子图(④⑦) | HTTP可见(部分)/ 需 hook | 执行图字段缺失 |
| **G3** SLO 感知调度 | swap/pin/recompute 三选一 | Dynamo 仅有原语,**无统一在线决策器**(需自建) | cache_control TTL(pin 原语)+ 各层前缀命中长度(recompute length)(③⑤) | HTTP可见/框架自算 | 决策器需自建 |
| | SLO 闭环 | SLA planner + correction factor | stream_options.include_usage(流尾精确 usage)(⑧) | HTTP可见 | **命中回填无消费通道 = G3 闭环硬前置(评审 F3)** |
| | hint-on vs hint-off | `agent_hints` 全集 vs 纯 LRU | profile 门控:custom/localhost(无 hint)↔ OpenRouter/Codex/xAI(全 hint)(见 §2.3) | HTTP可见(门控) | 8 类缺口需合成 |

---

## 2. 信息采集规格

### 2.1 信号分组主表

列 = [信号 | Hermes 来源/字段 | 采集方式 | 跨轮稳定性 | 喂给 Dynamo 的决策 | G]

#### ① 共享前缀(可 pin)

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| System prompt(真实 CLI ~16096 字节) | `chat`:messages[0].role=system;`anthropic`:body.system;`codex`:body.**instructions**(`system_prompt.py`) | HTTP可见 | 恒定(整天;跨日或 `invalidate_system_prompt` 才变) | KvIndexer prefix tree 顶层 → pin 候选 | G1 |
| Tools 定义(29 个 schema) | `chat`/`anthropic`/`codex`:body.tools(`toolsets:[]` 仍 29) | HTTP可见 | 恒定(版本内) | 与 system 拼成可 pin 前缀第二段 | G1 |
| Bit-perfect 前缀规范化 | `conversation_loop.py:1047-1078`:`content.strip()`+`json.dumps(sort_keys=True,separators=(",",":"))`+surrogate 清理 | HTTP可见(结果体现在 body) | 恒定 | 逐字节稳定前缀 → 块哈希链稳定、命中率最大化 | G1 |
| System 断点字节恒定(实测 len=2126/16110 跨轮一致) | `anthropic` system block | HTTP可见 | 恒定 | 对 system 段长 TTL pin,无失效风险 | G1 |
| 隐式前缀句柄 = system+tools prefix-hash | 由 body 前缀派生 | 框架自算(tokenize+hash) | 恒定 | 无显式会话键时的被动会话键 | G1 |

#### ② 会话亲和(routing key)

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| body.session_id | body 顶层;`chat`;**仅 provider=openrouter** | HTTP可见 | 恒定(压缩后轮换) | KV-aware sticky routing key | G2 |
| header.session_id | header;`codex`;仅 is_codex_backend(`codex.py:234`) | HTTP可见 | 恒定 | header 级会话粘性 | G2 |
| header.x-client-request-id(=session_id) | `codex.py:235` | HTTP可见 | 恒定 | 冗余路由键 | G2 |
| body.prompt_cache_key(=session_id) | xAI 经 extra_body,`codex.py:264`;`codex` | HTTP可见 | 恒定 | 直接 KV cache 桶路由键 | G2 |
| header.x-grok-conv-id | openrouter+`x-ai/grok-*` 或 codex+xai(`codex.py:253`) | HTTP可见 | 恒定 | xAI 流量会话亲和 | G2 |
| body.metadata.sessionId | `chat`;qwen 且 host==portal.qwen.ai | HTTP可见(localhost 不触发) | 恒定 | Qwen 流量会话亲和 | G2 |
| gateway `session_key`+`config_signature` | 内部 LRU(`gateway/run.py:17648-17716`) | 需 hook | 恒定(signature 变即解亲和) | sticky routing;signature 变 → 允许重均衡 | G2/G1 |
| body.tags / 归属头(X-Title/UA/X-BILLING) | body/header,按 provider | HTTP可见 | 恒定(版本级) | 弱信号:识别 Hermes 流量、按 client 版本分桶 | G2 |

> ⚠️ **评审 F1**:上表 ② 组里除 `body.session_id`(openrouter chat wire)外,`prompt_cache_key`/codex header `session_id`/`x-client-request-id`/`x-grok-conv-id` 全在 **codex(Responses wire)**。现有 `mock_anthropic.py` 只解析 Anthropic wire(+后补 `/v1/responses`),要采全这些键**必须有 Responses/Chat wire mock**(否则「有 hint 基线」的会话键采不到)。

#### ③ 缓存断点(块级可缓存边界)

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| cache_control{type:ephemeral}(native) | `messages[i].content[j].cache_control`/`system[last]`;`anthropic`(provider=anthropic / `/anthropic` 结尾 / minimax) | HTTP可见(逐 block 解析) | marker 恒定,断点滑动 | agent 显式 KV 缓存断点 → 对齐 token 边界保留 KV | G1 |
| cache_control(envelope) | 消息顶层 `messages[i].cache_control`;`chat`;host==openrouter.ai / Nous Portal+Claude | HTTP可见(localhost 不触发) | 同上 | 消息级断点 | G1 |
| 断点布局 `system_and_3` | `prompt_caching.py:74-77`:`non_sys[-3:]`(最后 3 条非 system)+ system,总 ≤4 | HTTP可见(对比相邻 body) | system 锚点恒在,3 个尾部断点滑动 | 驱逐启发式:倒数第 4 条之前的旧断点被弃用 → 预测哪些 KV 段不再被引用 | G1 |
| TTL marker 5m / 1h | `{type:ephemeral}`=5m;`{...,ttl:"1h"}`=1h;`prompt_caching.py:41-46`,config `cache_ttl` | HTTP可见 | 配置级恒定 | KV 保留时长:1h→长会话拉长保留;5m→短交互省内存 | G1/G3 |
| anthropic-beta 头(context-1m/`drop_context_1m_beta`) | header;`anthropic`;host 门控追加 | HTTP可见 | 恒定(门控除外) | 标识 wire 能力;1M 剥离暗示长上下文边界变化 | G1 |

> **断点记法澄清(评审 H2)**:`system_and_3` = system + 最后 3 条非 system 消息。**wire 差异决定索引写法**:
> - **anthropic_messages wire**:`system` 是独立顶层字段(自带 1 个断点),`messages` 数组**只含非 system 消息**。数组随轮次增长 1→3→5→7,其断点索引相应为 `[0]→[0,1,2]→[2,3,4]→[4,5,6]`(恒为末尾 3 条——这是 `anthropic_platform` 的实测值,与 `non_sys[-3:]` 一致)。
> - **envelope(openrouter chat)wire**:system 在 `messages[0]` 的统一视图下,断点为 `{[0]} ∪ {[N-3,N-2,N-1]}`。
> 关键不变量(两 wire 共有):**system 锚点断点恒在场**,3 个滑动断点随尾部前移,**倒数第 4 条之前的非 system 旧断点被弃用** —— 这正是 evict 启发式要消费的信号。

#### ④ 会话结构与增量前缀

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| messages 累积长度(1→3→5) | body.messages 长度;`chat`/`anthropic`;`codex` 用 body.**input** | HTTP可见(长度+逐条比对) | 单调增长(压缩前) | 增量前缀:第 N 轮 = N-1 超集 → 只算尾部新 token | G2 |
| 工具结果追加顺序确定性 | `tool_executor.py:548,668`:严格按 parsed_calls 顺序回填(并行执行也确定) | HTTP可见 | 增量字节确定 | 增量前缀 `[旧]+[assistant(tool_calls)]+[N 个 tool 结果(序确定)]` → 前缀只增不改 | G2 |
| 「工具完成→下一次 prefill」边界 | 主循环 `conversation_loop.py:801`:tool batch 后必有恰好一次 prefill | 需 hook;框架可从响应含 tool_calls 推断 | 确定性触发 | 工具执行期保温该会话 KV;预测下次新增≈Σ(tool 结果)+assistant | G2 |
| codex `input`+`store=false`+`include:[reasoning.encrypted_content]` | body;`codex` | HTTP可见 | input 累积;encrypted 每轮回放 | 不能依赖服务端 response 存储;加密 reasoning 是跨轮 KV 连续性载体 | G2 |
| turn 数 / iteration_budget 剩余 | 内部(`iteration_budget.py`;`max_iterations` 默认 90) | 需 hook | — | 预测剩余请求数、是否临近压缩 | G2 |
| Todo/Plan 活跃项(软执行图) | `tools/todo_tool.py:22-90`;per-session 有序表;hydrate `conversation_loop.py:504` | 需 hook | 事件性 | 活跃项数≈剩余轮数下界;in_progress 暗示子任务类型 → prefetch/长保温 | G2 |
| delegate_task 子图边界 | `run_agent.py:4793`;子 agent 独立 max_iterations/toolsets | 需 hook | 事件性(突发扇出) | 进入子图父图阻塞;预留子 agent 容量,父会话 KV 长保温 | G2 |

#### ⑤ 压缩/失效事件(evict 触发)★

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| 压缩阈值 `max(ctx*0.5, MINIMUM_CONTEXT_LENGTH=64000)` | `context_compressor.py`;**ctx 被模型真实窗口元数据覆盖**(`model_metadata.py:133`):claude-sonnet-4→200000 → 阈值 **100000**(非 64000) | 需 hook;**框架自算**(知 ctx+usage) | 配置级恒定 | 预测会话何时压缩 → 预备驱逐 | G1/G3 |
| 触发抖动门控 `tolerated_growth=max(4096, threshold*0.05)` + 反抖动(近 2 次省<10% 跳过) | `context_compressor.py:721` 等 | 需 hook | — | 压缩不高频连发 → 失效偶发,亲和决策可放心;**也意味着 filler 必须真跨阈值+跨 growth tolerance 才触发**(评审 M1) | G3 |
| `current_tokens/(ctx×0.5)`(前缀剩余寿命) | `approx_request_tokens`(`conversation_loop.py:1089`) | 框架自算 | 单调逼近 1 | 接近 1→避免激进 KV 预约;远离→强亲和 | G2/G1 |
| Preflight/Real-time 两阶段 + `awaiting_real_usage` | `conversation_loop.py:639`(preflight)/`:3965`(real usage) | 需 hook;输入 usage HTTP可见 | 事件性 | 依赖框架流尾准确返回 prompt_tokens,否则空转延迟压缩 | G3 |
| Summary 辅助请求(独特签名) | 独立调用:非流式 + 单 user + 无 system + 无 tools + max_tokens=2000;内容以 `"You are a summarization agent…"` 开头 | HTTP可见(签名独特) | 事件性 | 识别为辅助请求。**⚠️ 评审 M2:它走同一 base_url/同 session client,真机若按 session_id 粘性路由会落同一 DP rank/占同一前缀树——「不占主会话 KV」是待验证假设,非既定事实**;native anthropic 还会逃逸真实 api | G1/G3 |
| 压缩后前缀重写(中段失效点) | `[system]+[头部 protect_first_n=3 条]+[assistant:摘要 `[CONTEXT COMPACTION — REFERENCE ONLY]`]+[尾部按 token 预算 `tail_token_budget≈threshold×0.20` **动态**保留若干条,非固定 20 条(评审 H3)]`;messages 长度突降+非单调 | HTTP可见(SUMMARY_PREFIX 标记) | 事件后前缀改变 | KV 大规模失效点:中段历史被摘要替换 → 旧 KV 作废,只 system+头尾有效;**回放器须按 token 预算重建有效尾部** | G1/G3 |
| 会话轮换 session_id + parent_session_id | `conversation_compression.py:507-538` | 新 session_id:HTTP可见;**parent_session_id:需 hook** | 轮换点 | 路由键变更点:新 key 经 parent 链映射回旧节点保 KV 局部性 | G1/G2 |

#### ⑥ 解码长度先验(decode 调度/DP 均衡)

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| body.thinking.budget_tokens(high=16000/xhigh=32000/medium=8000/low=4000) | body;`anthropic` manual | HTTP可见 | 配置级恒定(代际门控) | reasoning 段 token 预算 = decode 上界 → 预留 decode 显存/排期;喂 planner OSL | G3 |
| body.output_config.effort(high/xhigh/max) | body;`anthropic` adaptive(4.6+) | HTTP可见 | 恒定(版本门控) | decode 长度定性强度 | G3 |
| extra_body.reasoning(chat)/body.reasoning(codex) | host 门控 openrouter/nous/github/lmstudio;`{effort,max_tokens}` | HTTP可见(localhost custom 不下发) | 恒定 | 同上 | G3 |
| body.max_tokens / max_output_tokens(**按 profile/model 解析,无统一默认**) | `chat_completions.py:507-524`:`ephemeral > user > profile.get_max_tokens(model)`;默认 `None`=用模型默认(`agent_init.py:457`) | HTTP可见(codex backend 路径缺失) | per-profile 恒定 | decode 硬上界 → 排期/抢占输入。**⚠️ 评审 H1:不是默认 64000(那是压缩地板 MINIMUM_CONTEXT_LENGTH);采集时记录每请求实际值,不可假设固定上界作跨 provider decode 先验** | G3/G1 |
| 轮角色二分(工具轮短/终答轮长) | 由 Todo 活跃项 + finish_reason 推断 | 需 hook(Todo)/部分框架自算 | 事件性 | 工具轮紧凑 decode 预算,终答轮预留长 decode slot | G3 |
| stop_reason=length → 续写链(≤3 次,预算 `_boost_base*(retries+1)` 递增) | 响应映射 `length`;`conversation_loop.py:1628-1770`;`anthropic.py:175,178` | HTTP可见(续写=前 messages+续写注入);**续写计数需 hook** | 事件性;预算递增 | 预测「还有≤3 段续写,前缀几乎不变」→ 保 KV 不驱逐 | G3/G1 |
| 显式 decode 长度预测 | **Hermes 从不下发**(grep 零命中) | **需新增 instrumentation** | — | 直接喂 router 负载估计 / planner `next_osl` | G3/G2 |

#### ⑦ 执行图/下一步(prefetch/overlap)

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| 动作空间静态封闭(固定 29 tools) | 会话开始冻结的 tools 集 | HTTP可见 | 恒定 | 预编译「每个工具→预期资源画像」 | G2 |
| Tool Search 三段式(search→describe→call) | `tool_search`/`tool_describe`/`tool_call` | HTTP可见(出现 bridge 工具即知) | 事件性(可预测性升高) | 工具过多时下一步几乎必然三段式 → prefetch 序列 | G2 |
| Skill nudge 周期(每 N 轮 skill_manage) | `skill_preprocessing.py`;`_skill_nudge_interval`(`:858`) | 需 hook | 周期性可计数 | 预测特定轮的请求形态 | G2 |
| 执行图 / 下一步 + steps-to-execution | **Hermes 无字段;响应内才揭晓** | **需新增 instrumentation**(KVFlow Agent Step Graph 式) | — | steps-to-execution 驱动细粒度驱逐 + 后台 overlap 预取 | G2/G1 |

#### ⑧ 重试/流式/SLO 相关

| 信号 | Hermes 来源/字段 | 采集方式 | 稳定性 | 决策 | G |
|---|---|---|---|---|---|
| body.stream(默认 true) | body 顶层;全 mode | HTTP可见 | 恒定 | 流式需持 KV 直到流尾 | G3 |
| stream_options.include_usage | `chat`(`chat_completion_helpers.py:1707`) | HTTP可见 | 恒定 | **关键**:压缩依赖流尾 prompt_tokens,否则空转 | G3 |
| usage 三桶(input/cache_read/cache_creation) | 响应下行解析 | HTTP可见(下行) | 事件性 | Hermes 读后端命中数(代理 KV 占用);**framework 回填命中率无消费通道 = G3 闭环硬前置(评审 F3)** | G3 |
| long-context tier 429 反应式恢复 | 响应 429→降 ctx+压缩+retry | HTTP可见(429 后 messages 变化);**重试计数需 hook** | 事件性 | 重试=新请求,前缀被压缩重写 | G3 |
| thinking 签名 400 清空重试 | `conversation_loop.py:2544-2562`,`error_classifier.py:553` | HTTP可见(重试 body 差异);分支判定需 hook | 事件性 | 重试请求前缀略变(thinking 块剥离) | G3 |
| 重试计数/退避状态 | 内部 | **需 hook** | — | 识别「第 k 次重试」避免重复抢占 | G3 |
| SLA 目标 TTFT/ITL/E2E、实时 TTFT/ITL/请求率/队列深度 | **Hermes 不产出**(推理侧/网关侧指标) | 框架侧自测(Prometheus/FPM) | — | 直接喂 SLA planner correction factor + replica 计算 | G3 |

### 2.2 每目标 MUST / NICE 信号集

标注:**[现成]** = HTTP可见或框架自算,零改 Hermes;**[新增]** = 需 hook 或新 instrumentation。

**G1 — KVCache 主动管理**
MUST:**[现成]** system+tools 可 pin 前缀主体(①);**[现成]** bit-perfect 规范化前缀/prefix-hash 链(①);**[现成]** 会话路由键(有 hint profile)②,无 hint 时降级为 **[现成]** system+tools prefix-hash;**[现成/框架自算]** cache_control 断点+TTL(③);**[现成]** 压缩后前缀重写 SUMMARY_PREFIX = 中段 evict 触发点(⑤);**[框架自算]** `current_tokens/threshold` 压缩临界(⑤,阈值=`max(ctx*0.5,64000)`)。
NICE:**[新增]** parent_session_id(⑤);**[新增]** 压缩预警 `x-hermes-compaction-imminent`+比值(⑤);**[新增]** 执行图 steps-to-execution(⑦);**[新增]** Todo 活跃项/delegate 子图(④⑦);**[现成]** length 续写链(⑥)。

**G2 — Agent 状态感知调度**
MUST:**[现成]** 会话亲和键 `session_id`/`prompt_cache_key`/`x-grok-conv-id`(②);**[现成]** messages 单调累积+工具结果顺序确定(④);**[现成]** 动作空间静态封闭 29 tools(⑦);**[现成]** decode 上界 max_tokens/thinking budget(worker 占用代理,记实际值)(⑥)。
NICE:**[新增]** gateway `session_key`+`config_signature`(②);**[新增]** 「工具完成→下次 prefill」确定边界(④);**[新增]** Todo 活跃项数+delegate_task 扇出上界(④⑦);**[新增]** 执行图/下一步声明(⑦);**[现成]** Tool Search 三段式(⑦);**[新增]** 显式 decode 长度预测(⑥)。

**G3 — SLO 感知调度**
MUST:**[框架侧自测]** SLA 目标 + 实时 TTFT/ITL/请求率/队列深度/`sum_decode_kv_tokens`(planner 主输入,非 Hermes 产出)(⑧);**[现成]** stream_options.include_usage(流尾精确 usage)(⑧);**[现成]** max_tokens/thinking budget(recompute 成本+decode 预算,记实际值)(⑥);**[现成]** 压缩后前缀重写+summary 签名(KV 失效点)(⑤);**[现成/框架自算]** cache_control TTL(pin 原语)+各层前缀命中长度(recompute length)(③);**[新增·REAL MUST]** 缓存命中回填消费通道(评审 F3:无此下行消费点,correction-factor 闭环退化为开环前馈)。
NICE:**[新增]** 显式 decode 长度预测/OSL 分布(planner `next_osl`,减冷启动振荡)(⑥);**[新增]** 重试计数+原因 `x-hermes-retry-{kind,count}`(⑧);**[新增]** 压缩预警(⑤);**[新增]** priority hint → 映射 Dynamo `agent_hints.priority`。

### 2.3 「有 hint vs 无 hint」两条基线(G3 的核心对照)

Hermes 的 hint 下发**强 host/provider 门控**,「有无 hint」基本由 profile/provider 切换。

| 维度 | **无 hint 基线**(custom / 官方 OpenAI chat / localhost) | **有 hint 基线**(OpenRouter / Codex / xAI / 第三方 anthropic-wire) |
|---|---|---|
| 会话路由键 | 无任何会话标识,仅 `messages/model/stream/stream_options(/tools)` | `session_id`/`prompt_cache_key`/`x-grok-conv-id`/header session_id 全套 |
| 会话识别手段 | **框架自算** system+首消息 prefix-hash(被动键) | 显式键直接 sticky routing |
| 缓存断点 | **无** cache_control | cache_control(native/envelope)+ system_and_3 + 5m/1h TTL |
| decode 长度先验 | max_tokens(profile/model 解析,无统一默认);reasoning 仅门控 host 下发 | thinking budget / output_config.effort / reasoning effort 全套 |
| 共享前缀(两基线都有) | system + tools(29)、bit-perfect、messages 单调累积、include_usage、压缩签名、length 续写链 | 同左(①④⑤⑥⑧ 多与 profile 无关) |
| 压缩/失效 | 前缀重写+summary 签名仍 HTTP可见;**native anthropic summary 逃逸真实 api** | 第三方 anthropic-wire 可截获 summary;openrouter envelope 可见 |
| **MOCK 段可达性(评审 F2)** | ✅ 可在 localhost 完整复现(custom+`/anthropic` 留在 mock) | ⚠️ **codex/xai/openrouter 会话键 localhost 采不到** → MOCK 段只能靠合成注入或第三方 anthropic-wire 子集;全套真键须 REAL 段或多 wire mock |

**给 G3 的结论:** 无 hint 基线 = prefix-hash 被动识别 + 共享前缀 + max_tokens 硬上界 + 压缩失效点,足以做「保守 pin + 内容哈希复用 + 上界式 decode 预算」;有 hint 基线额外解锁显式会话键 + cache_control 断点/TTL + reasoning 定量先验,可做「主动 pin + 断点对齐 + 分档 decode 预算」。**两者差距即 G3 要量化的 hint 价值。** 但注意:有 hint 基线的全套键在 **MOCK 段不可直接采**(F2),P0/P1 只能靠合成注入器近似,真值留 REAL 段。

### 2.4 当前缺失、需新增的 hint 接口建议

Hermes 注入集中在 transport 层 `build_extra_body`(`agent/transports/codex.py:155-264`)与 chat helpers host 门控(`chat_completion_helpers.py:570-780`),是新增 hint 的天然落点。

| # | 缺失信号 | 喂目标 | 建议注入层 | 具体建议 |
|---|---|---|---|---|
| 1 | 显式 decode 长度预测 | G3/G2 | transport `build_extra_body` 或新 header | 由 `reasoning_effort`+历史 output 分布估算,附 `nvext.agent_hints.osl` 或 `x-hermes-expected-output-tokens` |
| 2 | 执行图/steps-to-execution | G2/G1 | 新 instrumentation(暴露 iteration_budget 剩余+Todo 活跃项+planner 意图)→ transport | KVFlow Agent Step Graph 式;`nvext.agent_hints` 扩 `next_step`/`reuse_time`(Dynamo 无此字段,需自建) |
| 3 | parent_session_id | G1/G2 | transport `build_extra_body`(轮换后) | `x-hermes-parent-session-id`,新 session_id 经 parent 链映射回旧节点 |
| 4 | 压缩阈值+即将压缩预警 | G1/G3 | conversation_loop 决策点 → header | `x-hermes-compaction-imminent` + `current_tokens/threshold` |
| 5 | 重试计数+原因 | G3 | error 重试路径 → header | `x-hermes-retry-{kind,count}`(length_continue / tier-429 / 签名-400) |
| 6 | turn 序号 / loop 阶段 | G2 | conversation_loop → header | `x-hermes-turn-index` + 阶段标(tool-result 回灌/thinking-only/终答) |
| 7 | custom/官方 OpenAI 会话键 | G2/G1 | transport `build_extra_body`(新 custom profile) | 给 custom profile 注入 `prompt_cache_key`/`session_id`;或框架侧 prefix-hash 被动识别 |
| 8 | **缓存命中率回填消费通道(G3 闭环硬前置)** | G3 | 下行 usage 解析处 | Hermes 现仅读 usage 三桶,**无消费框架回填的通道**;**评审 F3:这是 G3 REAL 段 MUST 而非 NICE**——没有它 SLA correction-factor 闭环跑不通,只能退化为开环前馈 |
| 9 | priority hint | G2/G3 | transport `build_extra_body` | 由会话类型(交互/背景/delegate)派生 → `nvext.agent_hints.priority` |

**注入层原则:** 键值型、provider 已有承接字段(session/cache/osl/priority)→ transport `build_extra_body`/chat helpers host 门控,与现有注入同址,改动最小;事件型、跨决策(压缩预警、重试计数、turn 阶段)→ 在 conversation_loop 决策点产出并以 `x-hermes-*` 自定义 header 旁路下发,避免污染 provider body schema。

---

## 3. 测试台架构

### 3.0 核心边界声明(贯穿全文)

| | **MOCK 段**(本地,复用现有 mock) | **REAL 段**(真实推理后端) |
|---|---|---|
| **能产生** | 请求内容、到达时序、prefix-block hash、cache_control 断点、session/turn 关联、压缩/续写/summary 事件标记、hint 字段 | 上面全部 **+** 真实 KV 块表/命中/驱逐/swap、真实 TTFT/ITL/吞吐 |
| **不能产生** | **真实 KV、真实时延、真实吞吐**(usage/SSE 节奏是脚本编造) | — |
| **回答** | 「信号是否齐全,且能驱动正确调度决策?」 | 「这些决策在真机换来多少 TTFT/ITL/吞吐/命中率?」 |
| **MOCK 段「逻辑」指标的条件性(评审 M3/B1/B2)** | 块命中率/复用%/swap·pin·recompute 次数/重算 token = MOCK 内自洽,**但**:① preload 命中/evict 误杀**条件依赖合成 hint 正确性**(B1,循环论证风险),② DP 不均衡度**仅在自建固定路由假设下成立**(M3),③ 块命中率因近似 tokenizer 与真机块边界不一致**不可与 REAL 绝对对照**(B2) | ITL/TTFT P50·P99/req·s/token·s/SLO 达成率(绝对值真值) |

**一句话边界:** MOCK 段用模拟时延驱动一个 KV 调度模拟器,验证「信号→决策」链路正确且 hint 能改变决策;**所有百分比/毫秒只作 MOCK 内 A/B 相对比较**,真实 ITL/TTFT/吞吐/命中绝对值必须等 REAL 段。MOCK 段一切时延数字强制带 `simulated=true`,**不得对外当性能结论**。

### 3.1 总体数据流(ASCII)

```
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │ LAYER 2  负载生成层 (NEW)                                                                │
 │  WorkloadGen: 多会话 / 泊松到达 / 可调并发度·RPS / 不同执行图模板                          │
 │   ├─ (driver A) 真实 hermes CLI 并发: N×{隔离 HERMES_HOME, --resume <SID>}  [复用 cli_platform]  │
 │   │   ⚠️ MOCK 段只能跑 anthropic-wire profile(custom+/anthropic);codex/openrouter 须 REAL/合成 │
 │   └─ (driver B) import-driver 并发:  N×asyncio AIAgent.run_conversation()  [复用 anthropic/validation] │
 └───────────────┬──────────────────────────────────────────────────────────────────────┘
                 │  真实 HTTP 请求 (system+tools+history, hint 字段)
                 ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │ LAYER 1  信号采集层 (EXTEND mock; 多 wire 必需: Anthropic + Responses + Chat)              │
 │  Mock 后端 (Anthropic/Responses/Chat/Gemini wire, 协议正确 SSE)                           │
 │   _record() 扩展 ──► 每请求侧车:                                                          │
 │     • t_arrival_ns (单调纳秒)        • session_id / parent_sid / turn_idx                 │
 │     • prefix blocks[] + block_hash[] • compaction / length-continue / summary 事件标记     │
 │     • cache_control 断点 (已有)       • hint 字段抽取 (osl/priority/ttl/exec-graph)         │
 │  控制端点 /__mock/control 扩展: decode_tokens / itl_ms / ttft_ms / kv_block_size / cap    │
 └───────────────┬──────────────────────────────────────────────────────────────────────┘
                 │  trace.jsonl  (+ blocks 侧车 + events 侧车)
                 ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │ LAYER 3  Trace→调度器回放层 (NEW)                                                         │
 │  TraceReplayer: trace → 带 KV-block 标注的时间轴请求流                                     │
 │    ┌─────────────────────────────┐        ┌──────────────────────────────────────────┐ │
 │    │ (a) KV 调度模拟器 (自建) ◄─ 推荐先行 │        │ (b) 真实后端 (Dynamo/vLLM/SGLang) ◄─ 后行 │ │
 │    │  prefix-cache 命中 / radix tree │        │  KV Router + KVBM + disagg + planner       │ │
 │    │  swap/pin/recompute/分层卸载    │        │  端到端真值 (真 KV/真时延/真吞吐)           │ │
 │    │  DP 路由 / 容量+LRU 驱逐        │        │                                            │ │
 │    └──────────────┬──────────────┘        └──────────────┬───────────────────────────┘ │
 └───────────────────┼──────────────────────────────────────┼─────────────────────────────┘
                     │                                       │
        ┌────────────┴───────────────────────────────────────┴────────────┐
        │ LAYER 4  A/B 实验框架 (NEW):  同一 trace × {hint-ON, hint-OFF}    │
        └────────────────────────────────┬─────────────────────────────────┘
                                         ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │ LAYER 5  指标层 (NEW)                                                                     │
 │  [MOCK-相对] 块命中率·KV复用%·swap/pin/recompute次数+重算token·preload命中·evict误杀·DP不均衡 │
 │             (条件于路由假设/合成hint/近似tokenizer; 仅 MOCK 内 A/B 比较)                    │
 │  [REAL-绝对] TTFT P50/P99 · ITL · 吞吐(req/s, token/s) · SLO达成率                         │
 │  对比器 ab_compare(hint_on, hint_off) → Δ指标 + 容差断言 (复用 fixtures 容差范式)          │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 分层设计

**Layer 1 — 信号采集层(EXTEND,多 wire 必需)**
复用 `mock_anthropic.py` 的 `/__mock/control` 可编程下发、`/__mock/snapshots` 回读、`_record()` 录制(headers 脱敏 + 最终 body + `enumerate_cache_control` 逐位断点)、协议正确 SSE。**评审 F1:必须把 Responses wire(`/v1/responses`)与 Chat-Completions wire(`scripts/mock_openai_server.py`)提升为一等采集端点**——否则 codex/openrouter 的会话键(`prompt_cache_key`/header `session_id`/`x-grok-conv-id`/`body.session_id`)采不到。补四类缺口(全在 mock 侧,不改 Hermes):
1. **到达时戳** — 把秒级 `ts`(`time.strftime`)换/补为 `t_arrival_ns = time.monotonic_ns()`(`do_POST` 入口首行),加 `t_first_byte_ns`/`t_done_ns`。这是 KV 命中「前一请求块是否还在缓存」与 SLO 判定的唯一时间基。
2. **prefix 分块 + prefix-hash** — 新增 `blockify(body)`:tokenize(MOCK 段可先用 tiktoken/简单 BPE 近似,**REAL 段换真 tokenizer**)→ 按 `kv_block_size`(默认 16,经控制端点可配,对齐后端 `--kv-cache-block-size`)切定长块 → 每块算前缀链 hash(`hash_i=H(hash_{i-1}‖block_tokens_i)`,即 Dynamo `sequence_hash` 语义)→ `segment` 标 `system|tools|history|tail`。⚠️ **评审 B2:近似 tokenizer 的块边界与真机不一致 → MOCK 块命中率仅作 MOCK 内 A/B 相对比较,不可与 REAL 绝对值对照。** 落侧车 `trace.blocks.jsonl`(`seq` 关联,不污染主 body)。
3. **session/turn 关联** — `session_id` 从各 provider 字段派生,**custom/官方 OpenAI 无键 → 回退 system+tools 前缀块 hash 作隐式会话键**;`turn_idx` = 同 session 下 `n_messages` 单调段计数;`parent_session_id` Hermes 不下发 → MOCK 段由 WorkloadGen 在驱动侧注入侧车,REAL 段需 `x-hermes-parent-session-id` hook。
4. **压缩/续写事件标记** — 复用 `classify()`(单 user+无 tools+非流式=summary)扩为事件枚举:`compaction`(n_messages 非单调回落 + SUMMARY_PREFIX `[CONTEXT COMPACTION — REFERENCE ONLY]`)/ `length_continue`(stop_reason=max_tokens 后超集+续写注入)/ `summary_aux`(独特签名)。均被动从相邻 body 推断,零改 Hermes。

> 采集层 mock 通过 `/__mock/control` 的 `decode_tokens`/`itl_ms`/`ttft_ms` 模拟 decode 节奏,仅为让 Hermes 端到端跑起来 + 给回放器默认服务时间分布;真实 decode 时延在 REAL 段取。

**Layer 2 — 负载生成层(NEW,基于现有 driver)**
从「单会话顺序」升级为「并发多会话」,两种 driver 并存:
- **Driver A — 真实 hermes CLI 并发(高保真,做主)**:复用 `cli_platform/driver_cli.py`(隔离 `HERMES_HOME`+`config.yaml` 模板 + turn1 抓 `session_id`+`--resume`)。并发化:N 会话 = N 个独立 `HERMES_HOME`,进程池/asyncio 并发;会话内串行 turn(Hermes 语义),会话间交错。拿到真实 16096 system + 29 tools + 真实 SessionDB 多轮前缀(**只有 CLI 路径才有全量 system/tools**)。⚠️ **评审 F2:CLI 并发 + 全套 hint profile + 不打真实 API 三者不可兼得**——指向 localhost mock 时,真实 CLI 在 codex/openrouter profile 产生的 wire 必须有对应 wire mock(F1),否则只能跑 anthropic-wire profile(custom+`/anthropic`);指向真实 host 则成本/限流/native summary 逃逸全部回归。
- **Driver B — import-driver 并发(轻量快迭代)**:复用 `validation_platform`/`anthropic_platform` 的 `AIAgent.run_conversation()` + `asyncio.gather`。代价:system 缩水(~1.7k),tools 可能为 0 → 前缀长度不真,只适合跑信号链路通断。

**WorkloadGen 核心**(新建):输入 `{并发度 C, 目标 RPS λ, 到达分布(泊松/trace重放/突发), 执行图模板集}`;执行图模板用实测可声明结构造:`interactive`(1 次调用)、`tool_chain`(3~10 轮工具,前缀单调增)、`delegate_fanout`(突发子图扇出)、`long_session_compaction`(累积过**按模型 ctx 参数化的阈值** `max(ctx*0.5,64000)` 并跨 `tolerated_growth`+反抖动 才触发压缩+session 轮换;评审 M1)、`length_continue`(≤3 段续写)。**并发是 KV 复用/抢占/排队唯一出现的条件,单会话 trace 是 N=1 退化。**

**Layer 3 — Trace→调度器回放层(NEW)**
把 `trace.jsonl`+blocks 侧车+events 侧车转成带 KV-block 标注的时间轴请求流(每请求 = `{t_arrival_ns, session_id, block_hash[], segment[], hint{}, decode_len}`):
- **(a) KV 调度模拟器(自建,先行)**:离散事件模拟 Dynamo 风格 KV 管理——prefix-cache/radix tree 命中(block_hash 前缀链查全局前缀树,算 `New prefill tokens = total − overlap×block_size`);swap/pin/recompute(`L_recompute = L − Σ层命中`,短序列 recompute、长序列 swap、pin 由 `cache_control.ttl`/hint 驱动,**计次数+重算 token**);分层卸载 G1-G4(GPU→CPU→SSD→远端,容量配额+块 priority);DP 路由(device-aware-weighted,session 亲和 sticky,算不均衡度——⚠️ M3:此值仅在本模拟器路由假设下成立);容量+LRU/优先级驱逐(算 evict 误杀率——⚠️ B1:条件于合成 hint 正确性)。时延用可参数化服务时间算**模拟** TTFT/ITL,仅内部一致性。
- **(b) 真实 Dynamo/vLLM/SGLang(后行)**:同一 trace 经 wire-adapter 重放,注入 `nvext.agent_hints{osl,priority,speculative_prefill}`+`cache_control{ttl}`,开/关 KV events,采真实 KV 命中(worker Stored/Removed)、真实 TTFT/ITL/吞吐(Prometheus `/metrics`+FPM)。

**Layer 4 — A/B 实验框架(NEW,G3 核心)**
同一 trace,两条回放,只差 hint:**hint-OFF** 剥离所有 agent hint(只靠内容哈希被动复用+LRU);**hint-ON** 喂全量(`osl`/`priority`/`speculative_prefill`/`cache_control.ttl`+自建 exec-graph/steps-to-execution/parent_session_id/compaction-imminent)。回放器加 `--hint {on,off}`+hint 投影函数。断言复用容差范式:`hit_rate(on)−hit_rate(off) ≥ Δmin`、`recompute_tokens(on) ≤ recompute_tokens(off)×(1−x)`,REAL 段再加 `TTFT_P99(on) ≤ TTFT_P99(off)`。

**Layer 5 — 指标层(NEW,按真值域分栏)**

| 指标 | 定义/算法 | 真值域 |
|---|---|---|
| prefix/KV 块命中率 | 命中块数/请求总块数(block_hash 前缀链匹配) | **MOCK-相对**(B2:不可与 REAL 绝对对照) |
| KV 复用 % | Σ复用token/Σ输入token = 1 − new_prefill/total | **MOCK-相对** |
| swap/pin/recompute 次数+重算 token | 模拟器计数器;recompute_tokens=ΣL_recompute | **MOCK-相对** |
| preload 命中率 | speculative_prefill 预取块在下次被真用比例 | **MOCK-相对**(B1:条件于合成 hint) |
| evict 误杀率 | 被驱逐后 W 窗内又被请求的块/总驱逐块 | **MOCK-相对**(B1:条件于合成 hint) |
| DP 副本不均衡度 | std(per-rank load)/mean 或 max/mean | **MOCK-相对**(M3:仅本模拟器路由假设下成立;真实分布须 REAL) |
| TTFT P50/P99 | t_first_byte − t_arrival | **仅 REAL-绝对** |
| ITL | 相邻 token 间隔 | **仅 REAL-绝对** |
| 吞吐 req/s, token/s | 完成数/墙钟 | **仅 REAL-绝对** |
| SLO 达成率 | 满足 TTFT/ITL 目标请求比例 | **仅 REAL-绝对** |

对比器 `ab_compare(on, off)` 输出每指标 Δ+容差判定;模拟器段额外输出「模拟 TTFT/ITL」并明确标 `simulated=true`。

### 3.3 复用 / 新建组件清单

| 组件 | 状态 | 来源/落点 | 职责 |
|---|---|---|---|
| Mock 后端(协议正确 SSE,Anthropic wire) | **复用** | `anthropic_platform/mock_anthropic.py` | 接 Hermes 请求,录制 |
| **Responses wire mock(`/v1/responses`)** | **复用+提级** | `mock_anthropic.py`(已补)→ 独立成熟化 | codex 会话键采集(F1 必需) |
| **Chat-Completions wire mock** | **复用+提级** | `scripts/mock_openai_server.py` | openrouter `body.session_id`/envelope cache_control 采集(F1 必需) |
| `/__mock/control` + `/__mock/snapshots` | **复用+扩展** | mock | 加 `decode_tokens/itl_ms/ttft_ms/kv_block_size/cache_capacity` |
| `_record()` + headers 脱敏 + `enumerate_cache_control` | **复用+扩展** | mock `:85,:152` | 加 `t_arrival_ns/first_byte/done`、session/turn/parent、event 标记 |
| `blockify()` tokenize + 前缀块 hash | **新建** | Layer 1 侧车 `trace.blocks.jsonl` | 块级复用率前提(B2:MOCK 近似) |
| 事件标注器(compaction/length/summary) | **新建**(复用 `classify()` 起点) | Layer 1 | KV 失效/续写/辅助请求标记 |
| CLI 子进程编排(隔离 HERMES_HOME, --resume, 抓 sid) | **复用** | `cli_platform/driver_cli.py` | 真实全量 system/tools/SessionDB 多轮 |
| import-driver(`AIAgent.run_conversation`) | **复用** | `validation_platform/driver.py`, `anthropic_platform/driver_anthropic.py` | 轻量并发,链路通断 |
| **WorkloadGen**(并发多会话+泊松/RPS+执行图模板) | **新建** | 包裹 Driver A/B | 负载生成核心 |
| 高精度时序采集(monotonic_ns) | **新建**(挂 `_record`) | Layer 1 | TTFT/排队/命中时间基 |
| **TraceReplayer**(trace→带块标注请求流) | **新建** | Layer 3 入口 | 回放闭环 |
| **KV 调度模拟器**(prefix-cache/swap/pin/recompute/分层/DP/驱逐) | **新建** | Layer 3 (a) | 信号有效性验证 |
| 真实后端 wire-adapter(Dynamo/vLLM/SGLang) | **新建** | Layer 3 (b) | 性能真值 + host 门控激活 |
| **A/B 框架**(`--hint on/off`+hint 投影) | **新建** | Layer 4 | G3 对照实验 |
| 指标层 + `ab_compare` + 容差断言 | **新建**(复用 `gen_fixtures.py` 容差 + `check_*.py` PASS/FAIL/INFO 范式) | Layer 5 | Δ指标门禁 |
| host 门控真实后端接入(openrouter/真实 Anthropic) | **新建** | Layer 3(b) | 采 localhost 测不到的会话键 |

**需在 Hermes 侧新增 instrumentation 才能进 REAL 段的信号**(列为依赖,不阻塞 MOCK 段——MOCK 段由 WorkloadGen 旁路已知注入侧车):`x-hermes-expected-output-tokens`、exec-graph/iteration_budget 剩余、`x-hermes-parent-session-id`、`x-hermes-compaction-imminent`、`x-hermes-retry-{kind,count}`、custom/官方 OpenAI 会话键、**缓存命中回填消费通道(G3 闭环 MUST)**。REAL 段拿真值则需上述 hook 或用 OpenRouter profile。

### 3.4 分阶段路线(mock → 真实后端)

- **P0(模拟器,全 MOCK,验决策正确性)**:复用三平台骨架,把 8 类缺失信号做成**合成 hint 注入器**喂自建 radix-cache 模拟器(块表/容量/LRU 驱逐建模),证明 block_hash/session/hint 足以驱动正确 prefix 命中、swap/pin/recompute、preload/evict。**此阶段一切时延/命中率百分比不可信,仅用「决策一致率」断言(且 preload/evict 一致率条件于合成 hint 正确性)。**
- **P1(并发+时序+prefix-hash+多 wire,半 mock)**:新建并发负载生成器(泊松/trace 重放)、纳秒级到达/首字节/完成戳、tokenizer+prefix-block hash 标注器、可参数化 decode 时延模型、**Responses/Chat wire mock 成熟化(F1)**。负载曲线/DP 方差形态可初步观测,但绝对毫秒/绝对命中率仍需真实后端校准。
- **P2(真实后端,验所有时延/吞吐/SLO)**:trace 不变换 (b),取真实 TTFT/ITL/吞吐/SLO,激活 host 门控信号(openrouter profile),并验证模拟器结论在真机成立。模拟器与真机用**同一 trace、同一指标 schema**,便于对照模拟器预测误差。补 §2.4 缺口 #8(命中回填通道)才能让 G3 闭环成立。

---

## 4. 实验矩阵

设计原则:每个实验 = 一个可证伪的因果论点,挂在 G1/G2/G3 某子目标,调一个自变量(通常 hint on/off),看一个主 KPI。
**判据:** KPI 是「决策是否正确/hint 是否发出/evict 选对/路由键稳定」→ MOCK 可验(注意 preload/evict 条件于合成 hint);KPI 含绝对时延(TTFT/ITL/TPOT)、吞吐、命中率绝对百分比、SLO 达成率、overlap 毫秒 → 必须 REAL 段。host 门控信号(openrouter/xai/codex)localhost 测不到,native Anthropic summary 逃逸真实 api——这两类只能 REAL 段采。

### G1 — KVCache 主动管理

| ID | 场景/映射 | 自变量(hint) | 主 KPI | 期望 | 风险/混淆 | 阶段 |
|---|---|---|---|---|---|---|
| **E-G1.1** 共享前缀 pin 命中收益 | 多会话并发,每会话首请求含 16096-char system+29 tools(**必须用 cli_platform**,import-driver 缩水会低估) | system+tools 是否 pin(`cache_control{ttl:1h}` vs LRU) | MOCK:断点落 system 段末尾且字节恒定;REAL:跨会话 prefill 节省 token / TTFT 降幅 | 16k-token 前缀被 pin → N 会话仅第 1 次付 prefill,省 ~16k token/会话 | import-driver system 缩水低估;1h pin 低复用会话占容量;openrouter envelope localhost 测不到 | 决策→MOCK / 时延→REAL |
| **E-G1.2** compaction 感知精准 evict vs LRU 误杀 | RC-08 压缩链(messages 非单调,**filler 须按模型 ctx 真跨阈值 `max(ctx*0.5,64000)` + `tolerated_growth` + 反抖动**)+前缀重写(SUMMARY_PREFIX) | evictor 是否消费 compaction 事件:on=`x-hermes-compaction-imminent`+轮换 parent_sid,仅作废中段;off=纯 LRU | MOCK:evict 命中精度=(正确作废中段块/总 evict)+误杀率=(误删仍有效 system+头尾块) | 感知压缩→只 evict 摘要替换段、保 system+头尾,误杀率→0 | parent_sid/阈值不下发→合成 hint(误杀率条件于其正确性);200K 模型阈值=100000 非 64000;native summary 逃逸 | **决策→MOCK(条件于合成 hint)** / 重算时延→REAL |
| **E-G1.3** 多轮 --resume preload 命中 | RC-02 多轮累积(`[1,3,5]`)经真实 `--resume`+SessionDB,前缀 bit-perfect 超集 | 工具完成→下次 prefill 间是否 `speculative_prefill` 预取保温 | MOCK:同 session_id 跨轮路由键恒定+增量纯尾部;REAL:第 N 轮 TTFT(命中则只算尾部≈Σtool 结果) | 增量前缀只增不改→preload 保温→TTFT 随轮数近似常数而非线性增 | custom/官方 OpenAI 不下发 session_id→走 openrouter profile 或 prefix-hash 被动识别 | 决策→MOCK / TTFT→REAL |

### G2 — 执行图感知调度

| ID | 场景/映射 | 自变量(hint) | 主 KPI | 期望 | 风险/混淆 | 阶段 |
|---|---|---|---|---|---|---|
| **E-G2.1** 工具循环 prefetch/offload 与 prefill overlap | 主循环 prefill→decode→tool_call→tool_result→再 prefill(「工具完成必有恰好一次后续 prefill」=确定性触发)+delegate 子图 | 工具执行期是否 KVFlow 式后台 onboard(CPU→GPU 与下次 prefill forward 重叠) | MOCK:每 tool batch 后正确预测「下次 prefill 即将到来」且增量≈Σtool 结果;REAL:下次 prefill TTFT 减少量/overlap 占比 | 工具执行=确定性未来复用窗口→后台 prefetch 完全 overlap,onboard 延迟被工具时间吸收 | overlap 全是时延量,MOCK 无真实计算/传输时延;exec-graph/iteration_budget 不下发→合成 hint;execute_code-only 轮 refund 预算 | 决策→MOCK / overlap 毫秒→REAL |
| **E-G2.2** decode 长度先验对 decode 调度/DP 均衡 | RC-04 reasoning_effort 映射:`xhigh→output_config{effort:xhigh}`、`claude-3-7→thinking{budget_tokens:16000}`、OpenRouter 默认 `{effort:medium}`;叠加工具轮短/终答轮长二分 | router/planner 是否消费 OSL:on=effort→thinking budget→`agent_hints.osl`+planner `next_osl`;off=默认/冷启动 correction | MOCK:osl 按 effort 档正确生成+按 Todo 活跃项区分工具/终答轮;REAL:DP 负载方差/decode slot 利用率/ITL 违约率 | 长 decode 路由 decode-heavy、短工具轮紧凑预算→DP 方差降、冷启动振荡减 | Hermes 只发 budget/effort(上界非预测)→osl 由 effort+历史分布估算;**max_tokens 无统一默认,不可当固定 osl**;DP 均衡需多副本真实后端 | 决策→MOCK / DP 方差·ITL→REAL |

### G3 — SLO 感知调度

| ID | 场景/映射 | 自变量(hint) | 主 KPI | 期望 | 风险/混淆 | 阶段 |
|---|---|---|---|---|---|---|
| **E-G3.1** 固定 SLO 下 swap/pin/recompute 启发式吞吐差 | 容量压力并发:稳定 system+tools(应 pin)+中段历史(可 swap)+压缩作废段(可丢弃重算);抢占时三选一(RC-08) | 三选一是否有 hint:off=纯 LRU+引擎默认(vLLM V1 默认 recompute);on=`L_recompute=L−Σ层命中`+sweet-spot 长度+SLA 紧迫度+复用时间在三者间选 | MOCK:三选一与 oracle 一致率;REAL:固定 TTFT/ITL SLO 下吞吐(req/s 或 token/s) | hint on→system pin、长历史 swap、短段 recompute→同 SLO 下吞吐高于无差别 LRU | 吞吐/SLO 必须真实后端;Dynamo 仅有原语**无统一在线决策器**→自建;sweet-spot 依赖 profiling | 决策一致率→MOCK / 吞吐→REAL |
| **E-G3.2** 负载升高时 SLO 达成率(hint vs 无) | 阶梯升高 RPS/并发(**并发负载生成器必须新建**)+compaction 突发重 prefill+summary 子请求+delegate 扇出 | 是否给 SLA planner 喂 hint:on=预告 OSL 分布+请求率+compaction-imminent+delegate 扇出容量;off=仅 FPM 在线回归+correction 反应式 | MOCK:仅 hint 是否按场景正确生成;REAL:SLO 达成率(TTFT/ITL 达标占比)随负载曲线/扩缩提前量/correction 振荡 | hint 让 planner 前瞻扩容→高负载段 SLO 达成率显著高于反应式 | 达成率/扩缩纯真实后端+真实到达时序;Hermes 不预告请求率/扇出/compaction→合成 hint;planner adjustment_interval 默认 180s,负载阶梯需够长;Kalman/ARIMA 已部分吸收突发→需隔离 hint 增量;**G3 闭环依赖 §2.4 #8 命中回填通道(F3)** | 几乎全 REAL(MOCK 仅验 hint 生成) |
| **E-G3.3** summary 辅助请求 KV 隔离性(新增,评审 M2) | RC-08 压缩触发的 summary 子请求(非流式单 user)与主会话并发 | summary 是否带独立路由键/标 non-session,避免占主会话前缀树 | REAL:summary 是否落同一 DP rank、是否驱逐主会话 KV;MOCK 无法验(无真实路由) | 理想:summary 隔离不污染主会话 KV——**但这是假设,须 REAL 验证** | 现 Hermes summary 走同 base_url/同 session client,真机粘性路由可能落同 rank | **纯 REAL** |

### 速查:实验 → 论点 → 可验阶段

| 实验 | 证明什么 | MOCK 验 | REAL 验 |
|---|---|---|---|
| E-G1.1 | 共享前缀 pin 的命中收益 | 断点位置/字节恒定 | TTFT 降幅、prefill token 节省 |
| E-G1.2 | 感知 compaction 的精准 evict vs LRU 误杀 | 决策(evict 选对哪些块,条件于合成 hint) | 省下的重算时延 |
| E-G1.3 | 多轮 --resume preload 命中 | 路由键恒定、增量纯尾部 | 跨轮 TTFT 曲线 |
| E-G2.1 | 工具循环 prefetch/offload 与 prefill overlap | 触发边界+新增 token 预测 | overlap 毫秒、TTFT |
| E-G2.2 | decode 长度先验对 decode 调度/DP 均衡 | osl 按 effort 生成、轮型分类 | DP 负载方差、ITL |
| E-G3.1 | 固定 SLO 下 swap/pin/recompute 吞吐差 | 三选一与 oracle 一致率 | 固定 SLO 吞吐 |
| E-G3.2 | 负载升高时 SLO 达成率(hint vs 无) | 仅 hint 生成正确性 | **达成率曲线(主)** |
| E-G3.3 | summary 辅助请求 KV 隔离性 | — | **隔离性(纯 REAL)** |

---

## 5. 风险与诚实边界

1. **MOCK 无真实时延/KV/吞吐**:三套现有平台 usage 全脚本编造、SSE 无逐 token 节奏、单会话串行无到达时序、无 prefix-block hash;mock `cache_turns` 只是「首轮 creation 后续 read」的假命中(无容量/无驱逐)。因此 E-G1.1/E-G1.3/E-G2.1/E-G2.2 的时延、E-G3.1/E-G3.2 的吞吐与 SLO、E-G3.3 的隔离性**必须留 REAL 段**;E-G1.2/E-G3.1 的「决策选对」可在自建 radix 模拟器验。
2. **MOCK「逻辑」指标的条件性(评审 M3/B1/B2)**:① **preload 命中率/evict 误杀率条件于「合成 hint 正确」**——MOCK 段只验证决策逻辑自洽,**不能验证 hint 本身的预测准确率**(后者需 REAL + 真实 instrumentation);② **DP 不均衡度仅在自建固定路由策略假设下成立**,换 Dynamo 真实 KvIndexer router 会不同 → 与 TTFT 同级降为「须 REAL」;③ **块命中率因近似 tokenizer 与真机块边界偏移,不可与 REAL 绝对值对照**,只作 MOCK 内 A/B 相对比较。
3. **host 门控 + 多 wire(评审 F1/F2)**:openrouter/codex/xai profile 的会话键在 **localhost 测不到**;且 `mock_anthropic.py` 只懂 Anthropic wire → 这些键须靠 **Responses/Chat wire mock**(F1 必需)或 REAL 段采。**CLI 并发 + 全套 hint profile + 不打真实 API 三者不可兼得**(F2):MOCK 段 Driver A 只能跑 anthropic-wire profile,codex/xai/openrouter 的 hint 在 MOCK 段只能合成注入。**native Anthropic 压缩 summary 子请求会逃逸真实 api**,localhost 采不到,只第三方 anthropic-wire(custom+`/anthropic`)或 openrouter envelope 可截获。
4. **需新增 instrumentation 的部分**:Hermes v0.15.1 不下发 8 类信号(显式 decode 预测、执行图/steps-to-execution、parent_session_id、压缩阈值/预警、重试计数、turn 阶段、custom 会话键、**命中回填消费通道**)。P0/P1 一律用**合成 hint 注入器**代偿——这本身也是「加 instrumentation 后预期收益的上界估计」实验;REAL 段拿真值须按 §2.4 加 `x-hermes-*` header 或 `build_extra_body` 注入,或改走 OpenRouter profile。**其中 #8 命中回填通道是 G3 闭环硬前置(F3),非 NICE——缺它 SLA correction-factor 退化为开环前馈。**
5. **Dynamo 侧自建项**:Dynamo 仅提供 pin/swap/recompute **原语,无统一在线决策器**;执行图驱逐(steps-to-execution / KVFlow 范式)、ephemeral/session 标签 Dynamo 当前无对应字段,均需自建。
6. **压缩触发的可复现性(评审 M1)**:阈值 `max(ctx*0.5, 64000)` 中 ctx 被模型真实窗口覆盖(200K 模型→100000),且触发还要跨 `tolerated_growth=max(4096, threshold*0.05)` + 反抖动。压缩场景模板**必须按模型 ctx 参数化阈值并显式建模 growth tolerance**,否则 filler 在真实 CLI 上不会按预期频率触发,E-G1.2 trace 不可复现。
7. **诚实纪律(对外口径)**:任何 MOCK 段产出的 TTFT/ITL/吞吐/SLO 数字必须带 `simulated=true` 标,不得对外当性能结论;MOCK 段对外结论仅限「信号齐全 + hint 能驱动出更优调度**决策**(块命中↑、重算↓、误杀↓)」。

---

## 6. 落地下一步(具体到改哪个文件/平台)

**采集层(扩展现有 mock,零改 Hermes,先做)**
1. `anthropic_platform/mock_anthropic.py` `_record()`(`:152-192`):入口首行加 `t_arrival_ns=time.monotonic_ns()`(现为秒级 `time.strftime`,精度不足),补 `t_first_byte_ns`/`t_done_ns`;落 session/turn/parent/event 字段。
2. 同文件 `/__mock/control`(`:215-255`):加 `decode_tokens/itl_ms/ttft_ms/kv_block_size/cache_capacity` 可编程字段。
3. **多 wire mock 提级(F1)**:`/v1/responses`(已补)+ `scripts/mock_openai_server.py`(Chat wire)成熟化为一等采集端点,采 codex/openrouter 会话键。
4. 新建 `blockify()`(tokenize→定长块→前缀链 hash→segment 标注)→ 侧车 `trace.blocks.jsonl`(`seq` 关联;MOCK 用近似 tokenizer,标 B2 不可跨段对照)。
5. 复用 `enumerate_cache_control`(`:85-109`)+扩 `classify()` 为事件标注器(compaction/length_continue/summary_aux)。

**负载层(并发化现有 driver)**
6. 复用 `cli_platform/driver_cli.py`(`:292-357,:360-383`)并发化:N 个隔离 `HERMES_HOME`,进程池/asyncio 并发,会话内串行 turn(Driver A,主,MOCK 段限 anthropic-wire profile);`validation_platform/driver.py` + `asyncio.gather`(Driver B,轻量)。
7. 新建 WorkloadGen 包裹 Driver A/B:输入 `{C, λ, 到达分布, 执行图模板集}`,模板 = `interactive/tool_chain/delegate_fanout/long_session_compaction(按 ctx 参数化阈值)/length_continue`。

**回放+实验层(新建,P0 先行)**
8. 新建 TraceReplayer + KV 调度模拟器(prefix-cache/radix tree + swap/pin/recompute + 分层卸载 + DP 路由 + 容量/LRU 驱逐)。
9. 新建 A/B 框架(`--hint {on,off}` + hint 投影函数)+ 合成 hint 注入器(代偿 §2.4 的 8 类缺口;输出标注「条件于合成 hint」)。
10. 新建指标层 + `ab_compare` + 容差断言(复用 `cli_platform/gen_fixtures.py` 容差范式 + `check_assertions.py`/`check_cli.py` 的 PASS/FAIL/INFO 门禁范式),指标按真值域分栏,模拟 TTFT/ITL 强制标 `simulated=true`,MOCK「逻辑」指标标条件性。

**Hermes 侧 instrumentation(REAL 段依赖,按 §2.4 优先级,不阻塞 P0)**
11. transport `build_extra_body`(`agent/transports/codex.py:155-264`):加缺口 #1/#3/#7/#9(osl/parent_sid/custom 会话键/priority)。
12. `conversation_loop` 决策点 → `x-hermes-*` header:加缺口 #4/#5/#6(压缩预警/重试计数/turn 阶段)。
13. **下行 usage 解析处加缺口 #8 命中回填消费通道(G3 闭环 MUST,F3)。**
14. REAL 段接入:wire-adapter(Dynamo/vLLM/SGLang)+ OpenRouter/真实 Anthropic profile 激活 host 门控会话键。

**关键源码坐标(绝对路径,便于落地)**
- 会话/缓存键注入:`/home/niaowuuu/.hermes/hermes-agent/agent/transports/codex.py:155-264`
- host 门控 + stream_options:`/home/niaowuuu/.hermes/hermes-agent/agent/chat_completion_helpers.py:570-780,1707`
- max_tokens 解析优先级:`/home/niaowuuu/.hermes/hermes-agent/agent/transports/chat_completions.py:507-524`;默认 None `agent_init.py:457`
- 压缩阈值 `max(ctx*0.5,64000)`+ctx 覆盖:`/home/niaowuuu/.hermes/hermes-agent/agent/context_compressor.py`;`agent/model_metadata.py:133`(`MINIMUM_CONTEXT_LENGTH`);两阶段 `conversation_loop.py:639,3965`
- cache_control 断点+TTL:`/home/niaowuuu/.hermes/hermes-agent/agent/prompt_caching.py:41-77`(`non_sys[-3:]`)
- bit-perfect 规范化:`/home/niaowuuu/.hermes/hermes-agent/agent/conversation_loop.py:1047-1078`;system 冻结 `:1000-1029`
- 工具结果追加顺序:`/home/niaowuuu/.hermes/hermes-agent/agent/tool_executor.py:548,668`
- length 续写链:`/home/niaowuuu/.hermes/hermes-agent/agent/conversation_loop.py:1628-1770`
- session 轮换+parent 链:`/home/niaowuuu/.hermes/hermes-agent/agent/conversation_compression.py:507-538`
- gateway 亲和键:`/home/niaowuuu/.hermes/hermes-agent/gateway/run.py:17648-17716`
- Todo 软执行图:`/home/niaowuuu/.hermes/hermes-agent/tools/todo_tool.py:22-90`;delegate 子图 `run_agent.py:4793`(max_iterations=90 默认 `:353`)
- system_prompt 稳定前缀:`/home/niaowuuu/.hermes/hermes-agent/agent/system_prompt.py`

**复用平台文件(绝对路径)**
- `/mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/mock_anthropic.py`(控制端点 `:215-255`、`_record` `:152-192`、`enumerate_cache_control` `:85-109`)
- `/mnt/d/Workspace/Survey/hermes/DeepseekOutput/cli_platform/driver_cli.py`(并发编排骨架 `:292-357,:360-383`)、`gen_fixtures.py`(容差范式)、`check_cli.py`(门禁范式)
- `/mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/check_assertions.py`(PASS/FAIL/INFO 范式)
- `/mnt/d/Workspace/Survey/hermes/DeepseekOutput/validation_platform/driver.py`(import-driver)
- `/mnt/d/Workspace/project/hermes-agent/scripts/mock_openai_server.py`(Chat-Completions/Responses wire mock,F1 提级为一等端点)

---

## 7. 对抗评审修正记录(透明留痕)

本报告经一轮回源码(`~/.hermes/hermes-agent/` v0.15.1)对抗评审,以下为采纳/驳回明细:

| # | 评审项 | 判定 | 处理 |
|---|---|---|---|
| H1 | 「max_tokens 默认 64000」错(默认 None,按 profile/model 解析;64000 是压缩地板 MINIMUM_CONTEXT_LENGTH) | **采纳** | §0/§1/§2.1⑥/§2.3 全部改为「按 profile 解析,无统一默认,采集记实际值」 |
| H2 | 断点 `[0]→[0,1,2]→[2,3,4]→[4,5,6]` 与 `non_sys[-3:]` 矛盾 | **部分驳回**(wire 误读) | §2.1③ 澄清:anthropic wire 下 system 独立、messages 只含非 system,该序列正确;补 envelope wire 统一视图 `[0,N-3..N-1]` 与「system 锚点恒在+旧断点弃用」不变量 |
| H3 | 「protect_last_n=20 固定 20 条」夸大 | **采纳** | §2.1⑤ 改为「尾部按 token 预算 `≈threshold×0.20` 动态保留」,回放器按预算重建 |
| M1 | 压缩阈值=`max(ctx*0.5,64000)`(200K→100000)+growth tolerance+反抖动,filler 须真跨 | **采纳** | §2.1⑤/§3.2/§5.6/E-G1.2 全部参数化阈值并建模 growth tolerance |
| M2 | summary 辅助请求 KV 隔离是假设非事实 | **采纳** | §2.1⑤ 改措辞;新增实验 **E-G3.3**(纯 REAL 验隔离性) |
| M3 | DP 不均衡度非 MOCK-可信(依赖真实 router) | **采纳** | §3.0/§3.2/§5.2 降级为「MOCK 仅固定路由假设下成立,真值须 REAL」 |
| B1 | preload/evict 指标条件于合成 hint(循环论证) | **采纳** | §3.0/§5.2/§4 标注「条件于合成 hint 正确性,仅验决策逻辑自洽」 |
| B2 | 近似 tokenizer 块命中率不可跨段对照 | **采纳** | §3.0/§3.2/§5.2 标注「MOCK-相对,仅 MOCK 内 A/B」 |
| F1 | mock 只懂 Anthropic wire,codex/chat 会话键采不到 → 多 wire mock 必需 | **采纳** | §0.7/§2.1②/§3.2/§3.3/§5.3 提级多 wire mock 为 P1 阻塞项 |
| F2 | CLI 并发+全 hint profile+不打真实 API 三者不可兼得 | **采纳** | §2.3/§3.2/§5.3 标注 MOCK 段 Driver A 限 anthropic-wire profile |
| F3 | 命中回填通道(#8)是 G3 闭环硬前置,非 NICE | **采纳** | §1/§2.2/§2.4/§5.4/§6 提为 G3 REAL MUST |

**评审核验为真、无需改动**(供采信):压缩阈值公式、SUMMARY_PREFIX 文本 `[CONTEXT COMPACTION — REFERENCE ONLY]`、host 门控经 `base_url_host_matches`、codex header session_id(`codex.py:234-235`)、qwen `sessionId`、mock `_usage=body_len//4`(编造无容量无驱逐)、`_record` 用秒级 `time.strftime`(需换 `monotonic_ns`)——均与源码一致。
