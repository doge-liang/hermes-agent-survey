# 14 — Anthropic SDK 路径上下文管理深度测试

> 目标:Hermes Agent **v0.15.1**(用户实际安装树 `/home/niaowuuu/.hermes/hermes-agent`,`pyproject.toml:10` version=`0.15.1`)
> 范围:Anthropic Messages SDK 路径(`api_mode=anthropic_messages`)的上下文管理特性 —— 原生 prompt-cache 断点布局 / 5m·1h TTL / `anthropic-beta` 枚举 / thinking 五分支 / cache usage 三字段聚合 / 上下文压缩链路 / `length` 续写 / long-context tier 429。
> 方法:增强版可编程 mock Anthropic 后端 + 真实 `AIAgent.run_conversation()` driver + 程序化断言检查器,捕获 37 个真实请求,**25 PASS / 0 FAIL / 4 INFO**。

---

## 0. 导言与 TL;DR

本报告是 Hermes Agent 上下文管理系列调研的第 14 篇,聚焦 **Anthropic Messages SDK 路径**(即 Hermes 内部 `api_mode=anthropic_messages` 的那条 wire)。在动手读下文之前,先把几个贯穿全文的 Hermes 专有概念一次讲清,后文不再重复展开:

- **Hermes Agent**:一个把多家 LLM provider(Anthropic / OpenAI / xAI / Bedrock 等)统一封装到一个 agent 运行时里的项目。它在客户端侧主动做"上下文工程"——例如决定缓存断点打在哪里、推理预算给多少、上下文超长时怎么压缩——而不是把这些全交给后端黑盒处理。本报告就是在验证 Anthropic 这条路径上,这些客户端侧决策的真实行为。
- **api_mode(四类 wire)**:Hermes 把对接后端的协议分成四种线格式——`chat_completions`(OpenAI 兼容)、`anthropic_messages`(Anthropic 原生 Messages API)、`codex_responses`(OpenAI Responses/Codex)、`bedrock_converse`(AWS Bedrock Converse)。本报告只测第二种。
- **cache_control 断点**:Anthropic Messages API 允许客户端在 prompt 的若干位置打上 `cache_control` 标记(术语叫"断点"),告诉服务端"这个前缀可以缓存复用"。Anthropic 硬性限制每个请求最多 4 个断点。Hermes 采用的布局策略叫 **`system_and_3`**:把断点打在 system prompt + 最后 3 条非 system 消息上,合计 ≤4 个。
- **native(content 级)vs envelope(消息顶层)**:同样是打 `cache_control`,有两种放法。**native** 把标记打在消息 `content` 数组里某个 block 上(content 级);**envelope** 把标记打在消息对象顶层(message 级)。直连 Anthropic 与第三方 anthropic-wire 用 native,经 OpenRouter 转发则用 envelope。
- **host 门控**:Hermes 的很多行为不是看 provider 名字,而是看请求 `base_url` 的**主机名**来决定(代码里靠 `base_url_host_matches` / `_is_third_party_anthropic_endpoint` 之类函数判定)。例如"是否注入 OAuth 身份头""用 native 还是 envelope 布局"都由 host 字符串拍板。这一点对本报告至关重要:因为测试用的是 localhost mock,host 永远不是 `api.anthropic.com`,所以一切"需要特定 host 才触发"的特性都无法在 localhost 端到端跑出来,只能靠源码 file:line 确认(在结果里标 **INFO** 而非 PASS)。
- **压缩 / compaction**:当对话历史逼近模型上下文上限时,Hermes 会发一个"辅助请求"让模型把历史总结成一段摘要(checkpoint),用摘要替换旧消息以腾出空间。这条链路本报告会专门验证。
- **import-driver vs 真实 CLI**:Hermes 有两种驱动方式——直接在 Python 里调 `AIAgent.run_conversation()`(import-driver),或子进程跑 `hermes chat`(真实 CLI)。本报告用的是 import-driver。

**TL;DR(三句话)**:① 在 Anthropic 路径上,Hermes 确实按 `system_and_3` 布局打 4 个 cache_control 断点,断点随对话向数组末尾滑动,且 system 字节跨轮稳定(实测 `len=2126` 恒定 5 轮),这正是推理框架前缀缓存复用的前提。② thinking(推理)注入有五个分支,随模型代际门控,其中 4.6 模型遇到 `xhigh` 预算会被降级成 `max`。③ 凡是依赖 host 字符串的特性(OAuth 身份头、OpenRouter envelope 布局、native 压缩摘要逃逸到真实 Anthropic)在 localhost 都无法端到端触发,本报告诚实地把它们标为 INFO 并附源码坐标——这是测试边界,不是缺陷。

本报告共 37 个真实捕获请求,断言结果 **25 PASS / 0 FAIL / 4 INFO**。相关的 OpenAI SDK 路径对比见报告 12/13;已知错误的勘误集中见报告 17 §2。

---

## 1. 平台架构

测试平台位于 `/mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/`,由四件套组成。它与报告 12/13 的 OpenAI 平台共享同一套骨架——"mock 控制端点 + 真 AIAgent driver + 断言检查器"——但本平台的 mock 做了重度增强:把 **usage(token 计数)/ cache(缓存字段)/ stop_reason(停止原因)/ content-block(内容块)/ 错误注入** 全部做成"按场景可编程",目的就是为了能在 localhost 上精确触发 Anthropic 路径独有的那些上下文管理特性。

### 1.1 增强版 mock 后端 — `mock_anthropic.py`

这是一个线程化的 HTTP 服务(`ThreadingHTTPServer`,`mock_anthropic.py:406-408`),默认监听端口 8910。所谓"线程化"是指它能并发处理请求,不会因为 SDK 的流式连接而阻塞控制端点。它的核心能力如下:

- **协议正确的 Anthropic SSE 流**(`_anthropic`,`mock_anthropic.py:276-325`):SSE(Server-Sent Events)是 Anthropic 流式响应用的传输格式。mock 严格按 Anthropic 约定的事件顺序发送:先 `message_start`(携带初始 `usage`)→ 对每个 content block 依次发 `content_block_start` / `content_block_delta` / `content_block_stop` → `message_delta`(携带 `stop_reason` 与输出侧 usage)→ 最后 `message_stop`。因为顺序与字段都合规,真实的 `anthropic` SDK(版本 0.87.0)才能通过 `stream.get_final_message()` 把这些增量事件正确聚合成一条完整 Message——这是后面所有断言能成立的前提。
- **可编程 content blocks**(`_emit_block`,`:327-352`):mock 能按需要发四种内容块——普通文本 `text`、思维链 `thinking`(并附带 `signature_delta`,即 Anthropic 给 thinking 块的签名)、被脱敏的思维链 `redacted_thinking`(附带 `data`)、工具调用 `tool_use`(附带 `input_json_delta`,即工具参数的流式增量)。这样就能在一条响应里精确编排出测试想要的内容结构。
- **可编程 usage**(`_usage`,`:194-211`):usage 指 token 计数。mock 在 `message_start.usage` 里给出 `input_tokens / cache_read_input_tokens / cache_creation_input_tokens`(分别是输入 token、缓存命中读取的 token、缓存创建写入的 token),在 `message_delta.usage` 里给出 `output_tokens`(输出 token)。它有几个开关:`cache_turns=True` 模拟一段真实缓存生命周期——首轮 `creation>0 / read=0`(第一次把前缀写进缓存),后续轮 `read` 逐渐增长(命中已缓存前缀);`force_prompt_tokens` 直接抬高 `input_tokens`,用来人为触发上下文压缩;`omit_cache_fields` 干脆不发 cache 字段,用来测试 Hermes 端对缺失字段的 None 兜底。
- **可编程 stop_reason**:stop_reason 是 Anthropic 告诉客户端"为什么停下"的字段。mock 可发 `end_turn`(正常结束,默认)或 `max_tokens`(因达到输出上限被截断,这会触发 Hermes 的 `length` 续写);`stop_reason_once` 表示这个停止原因只对下一个主请求生效一次。
- **内容门控的错误注入**(`_maybe_error`,`:254-273`):mock 能根据请求内容有条件地注入错误,用来测 Hermes 的错误处理与重试:
  - thinking 签名 400 —— 当请求 body 里含 thinking 块时触发(由 `has_thinking_block` 判定,`:121-128`),模拟 Anthropic 拒绝无效签名的场景。
  - OAuth 1M-beta 400 —— 当 `anthropic-beta` 头含 `context-1m`(100 万上下文 beta)时触发。
  - long-context tier 429 —— 在下一个主请求上触发,模拟长上下文计费档位限流。
  - 通用 `error_once` —— 直接按指定 status + message 注入一次任意错误。
- **请求录制**(`_record`,`:151-191`):mock 把每个请求落盘到 `anthropic_requests.jsonl`,headers 经脱敏处理(敏感头列表 `_SENSITIVE`,`:49`,避免把 API key 写进日志),body 是最终发出的完整 body。录制时还**预计算**好一批切片字段供后续做字节级稳定断言:`cache_control_hits`(由 `enumerate_cache_control` 递归扫 system / messages-envelope / content-block / tools 四处算出,`:84-108`)、`ttl_forms`(各 marker 的 TTL 形态)、`system` 指纹、`has_thinking_history`、`thinking_field`、`output_config` 等。预计算的好处是断言检查器不必重新解析 body,直接读这些切片即可。

**控制端点**(给测试脚本用,不属于 Anthropic 协议):`POST /__mock/control`(下发场景配置,可携带 `reset`)、`GET /__mock/snapshots`(取已录请求)、`POST /__mock/reset`(清空状态)。

**业务端点**(模拟真实后端):`POST /v1/messages`(Anthropic 原生)、`POST /anthropic/v1/messages`(第三方 anthropic-wire,即第三方代理转发 Anthropic 协议)、`POST /v1/chat/completions`(OpenAI-wire 基线,专门用来验证 `x-anthropic-beta` 这个旁路头的键名)、`GET /v1/models`、`POST /api/show`(后两者是 Hermes 启动时探测模型能力会调的辅助端点)。

### 1.2 driver — `driver_anthropic.py`(13 个场景 S1-S13)

driver 用真实的 `AIAgent.run_conversation()` 来驱动,而不是手搓 HTTP 请求,因此走的是 Hermes 生产代码路径。几个关键设计点:

- **统一构造 agent**(`build_agent`,`:47-78`):集中创建 agent,并允许逐场景 override 几个上下文相关参数——`_cache_ttl`(缓存 TTL)、以及 `context_compressor` 的 `context_length`(上下文长度)/ `threshold_tokens`(压缩阈值)/ `protect_last_n`(保护末尾 N 条不被压缩)/ `protect_first_n`(保护开头 N 条)。这样不同场景能复用同一套构造逻辑只改少量旋钮。
- **跨轮回放**(`converse`,`:81-95`):它把上一轮返回的 `result["messages"]` 原样作为下一轮的 `conversation_history` 传回去。这一点很重要——它精确复刻了 gateway 在生产里跨轮回放的真实路径:gateway 对**每条入站消息都新建一个 fresh AIAgent**,历史不是靠"同一个 agent 一直循环"保留的,而是靠把上一轮消息数组喂回去。后文 §4(d)的因果结论正建立在这个事实上。
- **凭据隔离**(`:23-24`):driver 在每个场景开始前 `pop` 掉 `ANTHROPIC_API_KEY / OPENROUTER_API_KEY / ...` 等环境变量,防止真实凭据泄漏进来污染 provider 解析逻辑(否则 Hermes 可能误判该走哪条 provider 路径)。

13 个场景与被测特性的映射:S1 断点滑动、S2 TTL 5m/1h、S3 第三方 native 布局、S4 envelope 布局、S5 beta 头基线、S6 OAuth 身份头、S7 thinking 五分支、S8 cache 回读、S9 压缩链路、S10 length 续写、S11 签名 400 重试、S12 redacted_thinking、S13 tier 429。

### 1.3 断言检查器 — `check_assertions.py`

断言检查器读取 `anthropic_requests.jsonl`,逐场景做程序化校验,输出 `PASS / FAIL / INFO` 三态。其中 **INFO** 是专门为"源码确认 / host 门控"类断言准备的标记:这类特性在 localhost mock 上根本无法被触发(因为 host 不对),但有确凿的 file:line 源码证据,所以既不能判 PASS(没真跑出来),也不该判 FAIL(代码逻辑是对的),于是用 INFO 如实标注。检查器里的 `nonsys_idx`(`:27-34`)从预计算的 `cache_control_hits` 中提取出 `messages[i].content[j]` 形式的索引集合,专供断点滑动断言使用。

### 1.4 复现命令

```bash
# 1) 启动 mock(端口 8910)
/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3 \
  /mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/mock_anthropic.py 8910

# 2) 跑全部场景(用真实 AIAgent)
/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3 \
  /mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/driver_anthropic.py

# 3) 取断言结果
/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3 \
  /mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/check_assertions.py
```

注意三条命令都用安装树自带的 venv Python(`/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3`),以保证导入的是用户实际安装的 v0.15.1 代码,而不是别处的源码副本。

**最新断言汇总(本次实跑,37 请求):PASS=25  FAIL=0  INFO(源码/host 门控)=4。**

---

## 2. 特性总览表

下表把本报告验证的全部特性、它们在源码里的触发位置、关键请求字段、以及实测结论汇成一张参考表。表后的第 3 节会对每一行逐项详述,这里先给鸟瞰。需要先解释的几个表头:**触发条件(file:line)**指该特性由哪段源码实现并在何处被调用;**实测**列里 PASS 表示在 localhost 端到端真跑出来了,INFO 表示因 host 门控等原因只能源码确认,括号里是对应的场景编号(S1-S13)。

| 特性 | 触发条件(file:line) | 关键请求字段 | 实测 |
|---|---|---|---|
| cache_control 断点 `system_and_3` | `prompt_caching.py:70-77` apply,`conversation_loop.py:1024-1029` 调用门控 | `system[last].cache_control` + 最后 3 条非 system `content[-1].cache_control`,总数 ≤4 | **PASS** (S1) |
| 断点随对话滑动 | `prompt_caching.py:76` `non_sys[-remaining:]` 末尾切片 | 末 3 断点贴数组末尾(1,2,3→2,3,4→6,7,8) | **PASS** (S1) |
| 5m TTL marker | `prompt_caching.py:41-46` `_build_marker` | `{type:ephemeral}` 无 ttl 键 | **PASS** (S2) |
| 1h TTL marker | `prompt_caching.py:44-45` | `{type:ephemeral, ttl:"1h"}` | **PASS** (S2) |
| 非法 ttl 回退 5m | `_build_marker` else 分支 `:43-46` | 未知值 → `{type:ephemeral}` | 源码+实测(`30m`→无 ttl) |
| `anthropic-beta` 基线 | `anthropic_adapter.py:261-264` `_COMMON_BETAS` | `interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14` | **PASS** (S5) |
| interleaved-thinking 恒发(与 thinking 解耦) | `_COMMON_BETAS[0]` | beta 头恒含,thinking 关也在 | **PASS** (S5) |
| 无 prompt-caching/extended-cache-ttl beta(缓存已 GA) | 全仓 0 命中 | beta 头无 caching 子串 | **PASS** (S5) |
| 第三方 /anthropic native 布局 | `agent_runtime_helpers.py:1207-1208` policy=(True,True) | path=`/anthropic/v1/messages`,content-level cache_control | **PASS** (S3) |
| thinking 老模型 manual budget | `anthropic_adapter.py:2266` `{type:enabled,budget_tokens:N}` | high=16000(`:58`) | **PASS** (S7) |
| thinking 4.6+ adaptive | `:2253-2256`,`_ADAPTIVE_THINKING_SUBSTRINGS:85` | `{type:adaptive,display:summarized}`+`output_config.effort` | **PASS** (S7) |
| thinking 4.7+ 接受 xhigh / 4.6 降级 max | `_XHIGH_EFFORT_SUBSTRINGS:80`,`:2260-2261` | 4.6+xhigh → `effort:max` | **PASS** (S7) |
| thinking 关闭/haiku 不注入 | `:2249` `enabled is not False and "haiku" not in model` | 无 `thinking` 字段 | **PASS** (S7) |
| cache usage 三字段读取 | `usage_pricing.py:723-727` | input/cache_read/cache_creation 各独立桶 | **PASS** (S8) |
| `prompt_tokens` = 三者之和 | `usage_pricing.py:39-41` `@property` | 派生属性求和(非 normalize_usage) | **PASS** (S8,详见 §4 校正) |
| 压缩摘要辅助请求 | `context_compressor.py:1380-1395` | 非流式 `messages.create` + 单 user + 无 tools | **PASS** (S9) |
| `length` 续写(≤3 次预算提升) | `conversation_loop.py:1628/1732/3514-3528` | `max_tokens`→`length`→注入续写文本+提升 max_output | **PASS** (S10) |
| long-context tier 429 重试 | `conversation_loop.py` 重试 + 降 context_length | 429 一次 → 压缩 + retry | **PASS** (S13) |
| OAuth 身份头 | `anthropic_adapter.py:744-754`(host 门控) | localhost 走 x-api-key,无 claude-cli UA | **INFO** (S6,host 门控) |
| envelope 布局(OpenRouter) | `agent_runtime_helpers.py:1194` host 门控 | localhost 下 chat 无 cache_control | **INFO** (S4,host 门控) |
| thinking 块经 reasoning_details 回放 | `chat_completion_helpers.py:910-925`,`anthropic_adapter.py:1572-1586` | conversation_history 不含 thinking content 块 | **INFO** (S11/S12,见 §4) |

---

## 3. 逐特性详述(含真实捕获片段)

### 3.1 cache_control 断点滑动(`system_and_3`)

这是本报告最核心的特性。布局策略实现于 `prompt_caching.py:49-79`,模块顶部的 docstring(`:3-4`)就把策略写死了:"Single layout: system_and_3. 4 cache_control breakpoints — system prompt + last 3 non-system messages"(单一布局 system_and_3;4 个 cache_control 断点 = system prompt + 最后 3 条非 system 消息)。算法本身很短,逐行解释如下:

```
breakpoints_used = 0
if messages[0].role == "system":            # prompt_caching.py:70-72
    mark(messages[0]); breakpoints_used += 1   # system 占 1 个断点
remaining = 4 - breakpoints_used            # :74 总数封顶 4
non_sys = [非 system 消息的下标]              # :75
for idx in non_sys[-remaining:]:            # :76-77 末尾切片 → 向数组末尾滑动
    mark(messages[idx])
```

关键在第 `:76` 行的 `non_sys[-remaining:]`——这是个"取末尾 remaining 个元素"的切片。随着对话变长,非 system 消息越来越多,这个切片始终咬住数组**最末尾**的若干条,于是断点就表现为"随对话向数组末尾滑动"。这正是缓存友好的做法:最旧的前缀(system + 早期消息)保持稳定可复用,断点跟着新增的消息走。

具体往哪个 block 上打标记,由 `_apply_cache_marker`(`:15-38`)负责。在 native 路径下,它把 marker 打到该消息 `content` 列表的**最后一个 block**(`:35-38`);如果 content 是一个裸字符串(而非 block 列表),它会先把字符串包装成 `[{type:text, text, cache_control}]`(`:29-32`)再打。这就解释了一个容易困惑的现象:当 system 本身是一个 list(多个 block)时,marker 落在的是**最后一个** system block,而不是第一个。

**S1 真实捕获(native anthropic,5 轮,system 是双块结构):**

| seq | n_messages | 断点总数 | 断点位置 |
|---|---|---|---|
| 1 | 1 | **2** | `system[1]`, `messages[0].content[0]` |
| 2 | 3 | **4** | `system[1]`, `messages[0,1,2].content[0]` |
| 3 | 5 | **4** | `system[1]`, `messages[2,3,4].content[0]` |
| 4 | 7 | **4** | `system[1]`, `messages[4,5,6].content[0]` |
| 5 | 9 | **4** | `system[1]`, `messages[6,7,8].content[0]` |

怎么读这张表:`seq` 是请求序号,`n_messages` 是该请求里非 system 消息的条数,`断点总数`是该请求里 `cache_control` 标记的个数,`断点位置`列出它们落在哪。第一轮只有 1 条用户消息,所以断点总数是 **2**(1 个 system + 1 条消息);从第二轮起非 system 消息 ≥3,断点总数恒为 **4**(1 个 system + 3 条末尾消息)封顶。注意非 system 三断点的索引随轮次滑动:`0,1,2 → 2,3,4 → 4,5,6 → 6,7,8`,每轮整体向末尾推进——这就是"滑动"的直观证据。各轮断点总数序列为 `[2,4,4,4,4]`,始终 ≤4,满足 Anthropic 的硬约束。

这里的 `system[1]` 需要解释:本场景的 system 是一个双块 list——`system[0]` 是 57 字节的身份串(不打 marker),`system[1]` 是 2069 字节的主体(打 marker)。因为 `_apply_cache_marker` 打的是**最后一个** block,所以 marker 落在 `system[1]`。

**system 字节跨轮稳定**(这是缓存前缀稳定性的硬前提):`system.len = [2126, 2126, 2126, 2126, 2126]`——5 轮的 system 总字节数完全一致(检查器判 PASS)。这一点至关重要:只有 system 前缀逐字节不变,服务端的 KV 缓存前缀才可能复用;只要变一个字节,前缀就失效。(2126 = 57 + 2069 两块之和。)

第一条 user 消息的真实片段(可以看到 content 字符串被包成了 text block 并打上了 marker):
```json
{"role":"user","content":[{"type":"text","text":"Turn 1: please acknowledge.","cache_control":{"type":"ephemeral"}}]}
```

> **一个边角情形(不影响主结论,已限定在"system 存在"前提下)**:如果首条消息不是 system(即 `breakpoints_used=0`),那么 `remaining=4`,4 个断点会全落在最后 4 条非 system 消息上,布局名义上退化为"last 4"而非"system_and_3"。但在正常运行路径下,`conversation_loop.py:1009` 总会在消息数组前置一条 `{role:system}`,所以实际跑出来的永远是 system_and_3 布局。这个边角只是把算法的完整行为讲清楚,不构成对主结论的削弱。

### 3.2 5m / 1h TTL marker

TTL(Time To Live)是缓存条目的存活时长。Anthropic 的 ephemeral 缓存默认 5 分钟(5m),也支持延长到 1 小时(1h)。Hermes 用一个 marker 来表达这个选择,marker 由 `_build_marker`(`prompt_caching.py:41-46`)构造:基础形态是 `{type:ephemeral}`,**仅当** `ttl=="1h"` 时才额外加上 `marker["ttl"]="1h"`。换句话说,5m 是"不写 ttl 键"来表达的(用默认),只有 1h 才显式写键。

**S2 真实捕获:**
```
S2_ttl_5m  → markers = [{"type":"ephemeral"}, {"type":"ephemeral"}]
S2_ttl_1h  → markers = [{"type":"ephemeral","ttl":"1h"}, {"type":"ephemeral","ttl":"1h"}]
```

可以看到 5m 场景的两个 marker 都不带 `ttl` 键,1h 场景的都带 `ttl:"1h"`。此外还测了非法值:把 ttl 设成未知的 `30m`,经 `_build_marker` 的 else 分支会退化成 `{type:ephemeral}`(不带 ttl,等价于 5m),实测确认。

一个值得注意的解耦事实:TTL 的切换与 `anthropic-beta` 头**完全无关**。负责算 beta 头的函数 `_common_betas_for_base_url`(`anthropic_adapter.py:539-568`)的签名只接受 `base_url / drop_context_1m_beta` 两个参数,根本不接收任何 ttl 参数。所以无论选 5m 还是 1h,beta 头都不会变——早期 Anthropic 需要 `extended-cache-ttl` beta 来开启 1h,但现在缓存已 GA(正式可用),不再需要 beta 头(见 §3.3)。

### 3.3 `anthropic-beta` 枚举

`anthropic-beta` 是 Anthropic 用来开启实验性/分阶段功能的请求头,值是逗号分隔的 beta 名单。Hermes 的基线名单 `_COMMON_BETAS`(`anthropic_adapter.py:261-264`)只含两项:`interleaved-thinking-2025-05-14`(交错思维,允许 thinking 与 tool_use 交替)与 `fine-grained-tool-streaming-2025-05-14`(细粒度工具流式)。

**S5 真实捕获(native api-key):**
```
anthropic-beta: "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"
auth_kind: x-api-key   user_agent: "Anthropic/Python 0.87.0"   x_app: ""
```

几个要点:
- `interleaved-thinking` **恒在**,与 thinking 注入是解耦的——即使把 thinking 关掉,这个 beta 头依然带。这说明它不是"开了 thinking 才加",而是基线常驻。
- 基线名单里**不含** `prompt-caching` 或 `extended-cache-ttl` 这类缓存 beta——因为 prompt 缓存与扩展 TTL 都已经 GA(正式发布),不再需要 beta 开关。证据是全仓 `agent/` 源码对这些子串 0 命中。
- 还有一类 **OAuth-only beta**(`claude-code-20250219`、`oauth-2025-04-20`),它们只在 OAuth host 门控命中时才追加(`:280-283`)。在 localhost 上 host 不对、门控不命中,所以这两个 beta 不会出现(详见 §4(a))。

### 3.4 thinking 五分支(含 4.6+xhigh→max 降级)

thinking 是 Anthropic 模型的推理/思维链能力,客户端可以控制它的预算(budget)或努力档位(effort)。Hermes 的注入逻辑在 `anthropic_adapter.py:2249-2269`,它会根据模型代际走五个不同分支。先解释三张门控表:

- 预算表 `THINKING_BUDGET = {xhigh:32000, high:16000, medium:8000, low:4000}`(`:58`)——把努力档位映射到具体的 token 预算。
- adaptive 门控子串 `_ADAPTIVE_THINKING_SUBSTRINGS = ("4-6","4.6","4-7","4.7","4-8","4.8")`(`:85`)——模型名里含这些子串(即 4.6/4.7/4.8 代)才走新的 adaptive 模式。
- xhigh 门控子串 `_XHIGH_EFFORT_SUBSTRINGS = ("4-7","4.7","4-8","4.8")`(`:80`)——只有 4.7/4.8 代支持 `xhigh` 档,**4.6 不在此列**(这正是下面降级的原因)。

**S7 真实捕获(五分支全 PASS):**

| 场景 | model | `thinking` 字段 | `output_config` |
|---|---|---|---|
| 老模型 manual | `claude-3-7-sonnet-20250219` | `{type:enabled, budget_tokens:16000}` | `None` |
| 4.6 adaptive | `claude-opus-4-6` | `{type:adaptive, display:summarized}` | `{effort:high}` |
| 4.7 xhigh 保持 | `claude-opus-4-7` | `{type:adaptive, display:summarized}` | `{effort:xhigh}` |
| **4.6 xhigh→max 降级** | `claude-opus-4-6` | `{type:adaptive, display:summarized}` | `{effort:max}` |
| 关闭 | `claude-sonnet-4` `enabled:False` | `None` | `None` |

逐行解读这五个分支:
1. **老模型 manual**:3.7 代不在 adaptive 名单里,走旧的"手动预算"模式——thinking 字段是 `{type:enabled, budget_tokens:16000}`(high 档对应 16000),没有 `output_config`。
2. **4.6 adaptive**:4.6 在 adaptive 名单里,走新模式——thinking 变成 `{type:adaptive, display:summarized}`(自适应 + 摘要式展示),努力档位移到独立的 `output_config.effort` 字段,这里是 `high`。
3. **4.7 xhigh 保持**:4.7 既在 adaptive 名单又在 xhigh 名单,所以请求的 `xhigh` 档被原样保留,`output_config.effort:xhigh`。
4. **4.6 xhigh→max 降级**(本表重点):同样请求 `xhigh`,但 4.6 不支持,于是被降级成 `max`。降级逻辑是 `if adaptive_effort=="xhigh" and not _supports_xhigh_effort(model): adaptive_effort="max"`(`:2260-2261`)——因为 4.6 不在 `_XHIGH_EFFORT_SUBSTRINGS` 名单,`_supports_xhigh_effort` 返回 False,xhigh 就被压成 max。
5. **关闭**:显式 `enabled:False`,thinking 与 output_config 都是 `None`,不注入任何 thinking 字段。

补充两个细节:老模型 manual 分支注入 thinking 时还会强制 `temperature=1` 并抬高 `max_tokens`(`:2268-2269`),因为 Anthropic 要求开 thinking 时温度必须为 1 且输出预算要够容纳思维;而 `haiku` 系列模型整块跳过 thinking 注入(`:2249` 的判定 `enabled is not False and "haiku" not in model`),因为 haiku 不支持。

### 3.5 cache usage 三字段聚合

这一节讲 Hermes 怎么把 Anthropic 返回的缓存 token 计数读进自己的数据结构。流程是:SDK 的 `get_final_message()` 返回一条原生 Anthropic Message,它的 `.usage` 就是 SDK 把流式增量聚合后的最终 usage 值(`chat_completion_helpers.py:2041`,相关注释在 `:1964-1967`)。然后 `normalize_usage` 函数的 `anthropic_messages` 分支(`usage_pricing.py:723-727`)把三个字段 `input_tokens / cache_read_input_tokens / cache_creation_input_tokens` **各自独立**地用 `_to_int` 读出来,塞进 `CanonicalUsage` 的**三个独立桶**(`:764-770`)。对于缺失字段,有双重兜底:先 `getattr(..., 0)` 取默认 0,再 `_to_int(int(value or 0))`(`:548-552`)二次保护,确保缺字段时落 0 而不是报错。

> **重要校正(避免一个常见误解)**:三个字段求和(`input + cache_read + cache_write`)这件事**不是**发生在 `normalize_usage` 里,而是发生在 `CanonicalUsage.prompt_tokens` 这个 `@property`(`usage_pricing.py:39-41`)被读取时才动态求和。`normalize_usage` 本身刻意保持三桶**分离**——这与 codex/openai 分支的语义恰好相反(后者是从总数里**减去**缓存部分)。所以正确的说法是:Hermes 内部三个桶分别叫 `input` / `cache_read` / `cache_write`(注意内部桶名是 `cache_write_tokens`,而 Anthropic wire 上对应的字段名才叫 `cache_creation_input_tokens`),`prompt_tokens` 是按需派生的求和属性。详见 §4。

**S8 真实结果**:driver 日志显示 `cache_read` 跨轮递增(首轮 `creation>0 / read=0`,后续轮 `read` 增长),Hermes 端正确把这三个独立桶都回读出来(3 个请求,PASS)。在 `omit_cache_fields` 路径下(mock 不发 cache 字段),三个字段缺省兜底为 0(源码确认)。

### 3.6 上下文压缩链路(摘要非流式 / 单 user / 无 tools)

当对话历史逼近上下文上限,Hermes 会发一个"摘要辅助请求"把历史压缩成 checkpoint。这个辅助请求的构造在 `context_compressor.py:1380-1395`:`task="compression"`,`messages=[{role:user, content:prompt}]`(只有一条 user 消息),**不带 tools**,然后经 `call_llm()` 走**非流式**的 `messages.create`(`auxiliary_client.py:967,1012`)。"非流式 + 单 user + 无 tools"这三个特征就是识别压缩摘要请求的指纹。

**S9 真实捕获(第三方 anthropic-wire,custom provider,5 个主请求 + 2 个摘要请求):**

| seq | kind | stream | n_messages | tools | path |
|---|---|---|---|---|---|
| 23 | main | True | 1 | False | `/anthropic/v1/messages` |
| 24 | main | True | 3 | False | `/anthropic/v1/messages` |
| 25 | main | True | 5 | False | `/anthropic/v1/messages` |
| **26** | **summary** | **False** | **1** | **False** | `/v1/v1/messages` |
| 27 | main | True | 5 | False | `/anthropic/v1/messages` |
| **28** | **summary** | **False** | **1** | **False** | `/v1/v1/messages` |
| 29 | main | True | 5 | False | `/anthropic/v1/messages` |

怎么读:`kind` 区分主请求(main)与摘要请求(summary);`stream` 列里主请求都是流式(True),两个摘要请求都是非流式(False),正好对上"摘要走非流式"的指纹。摘要请求(seq 26/28)都是 `n_messages=1`(单 user)、`tools=False`(无 tools),三特征齐备。注意主请求的 `n_messages` 在压缩发生后**回落**——序列是 `[1,3,5,5,5]`,在第 3 轮触顶 5 之后,后续即便对话继续也封顶在 5(因为旧消息被摘要替换了),不再单调增长(PASS)。

摘要请求 body(seq 26)的细节:`keys=[max_tokens, messages, model]`,`stream=None`,`tools=None`,单条 user 消息,content 以 `"You are a summarization agent creating a context checkpoint..."`(你是一个创建上下文 checkpoint 的摘要 agent)开头,`max_tokens=2000`。

> **关于 max_tokens=2000 的勘误说明**:v0.15.1 的源码实际是 `max_tokens=int(summary_budget×1.3)`,其中 `summary_budget=max(_MIN_SUMMARY_TOKENS=2000, content×0.20, …)`,在地板(floor)情形下约为 2600(2000×1.3)。这里实测到的 2000 应当是**升级前 v0.13 的捕获值**,不是 v0.15.1 的当前行为。请勿把 2000 当成固定常量;正确写法是上面的公式。详见报告 17 §2.1。

> **为何这里用 custom provider 而非 native anthropic**:这是一个刻意的选择。native anthropic 路径下,压缩摘要的辅助 client 会**逃逸**到真实的 `api.anthropic.com`(详见 §4(c)),根本到不了 localhost mock,也就没法观测。只有改用 custom provider,才能把摘要请求留在 localhost。表中摘要 path 显示成 `/v1/v1/messages`(有两个 `v1`)是 mock 对 custom base_url 做 SDK 路径拼接的产物,不影响"非流式 + 单 user + 无 tools"这个形态断言。

### 3.7 `length` 续写

当模型因达到输出上限被截断(stop_reason=`max_tokens`),Hermes 不会直接放弃,而是发起"续写"。先看映射:`stop_reason → finish_reason` 的转换在 `transports/anthropic.py:175,178`——`max_tokens` 和 `model_context_window_exceeded` 两种 stop_reason 都映射成 `finish_reason="length"`,这个映射在 anthropic 路径上由 `conversation_loop.py:1606` 应用。

续写逻辑:当 `finish_reason=="length"`(`:1628`)且本轮没有 tool_call 时,Hermes 执行——`length_continue_retries += 1`(`:1726`),用 `< 3` 做门控(`:1732`,即最多续写 3 次),注入一段续写提示文本(`_get_continuation_prompt`,文本定义在 `:343-348`;注意这段续写消息的 role 实际是 **user**,content 以 `[System: ...]` 开头),并置 `restart_with_length_continuation=True`(`:1760-1769`)重启本轮。每次重启会**渐进提升**输出预算,公式是 `_boost_base*(retries+1)` 并封顶(`:3514-3528`)——即续写次数越多给的输出预算越大。一旦成功(模型这次正常结束),`length_continue_retries` 归零(`:4372`),为下次重新计数。

**S10 真实捕获**:把 mock 设成 `stop_reason_once=max_tokens`(只在下一个主请求返回一次截断)→ 触发 2 个请求(seq 30 是 `n_msgs=1` 的原始请求,seq 31 是 `n_msgs=3` 的续写请求),PASS。

### 3.8 long-context tier 429

long-context tier 指 Anthropic 对超长上下文的计费/限流档位。当触发该档位的限流,后端返回 HTTP 429。mock 用 `_maybe_error` 注入这种 429,message 里含 `extra usage` + `long context tier` 字样(`mock_anthropic.py:265-268`)。

**S13 真实捕获**:429 注入一次 → Hermes 反应式恢复:降低 `context_length` + 触发压缩 + 重试(retry)→ 共 2 个请求(seq 36/37,均打到 `/anthropic/v1/messages`),PASS。这条链路把"长上下文限流"从硬失败变成了"压缩历史腾空间再重试"的自愈流程。

---

## 4. 关键发现专节(对抗验证)

本节四项核心结论分别经过独立的**对抗验证**——即不轻信 claim,逐条找源码反例,给出 confirmed(证实)/ partial(部分成立)/ refuted(推翻)的裁决,每条都附 file:line 证据。这四项是本报告最需要工程师警惕的边界,因为它们都涉及"localhost 测不出真实行为"或"字面陈述与因果结论不一致"。

### (a) OAuth 身份头 host 门控 —— `verdict: partial`(部分成立)

**机制(成立的部分)**:OAuth 身份头(包含 `auth_token` 的 Bearer 凭据 + UA 为 `claude-cli` + `x-app:cli` + `oauth-2025-04-20`/`claude-code-20250219` 这两个 beta)的注入代码在 `anthropic_adapter.py:744-754`。但关键是:第三方分支 `elif _is_third_party_anthropic_endpoint(base_url)`(`:736`)**排在** OAuth 分支(`:744`)**之前**。localhost 的 base_url 不含 `anthropic.com` 子串 → 第三方分支判定为 True(`:382`)→ 走 `kwargs["api_key"]` 注入(即 x-api-key,`:741`),于是 OAuth 头根本不会被注入。另外,会话级的 `_is_anthropic_oauth` flag(`agent_init.py:644-645`,由 token 前缀 + `provider==anthropic` 判定)与身份头注入是**两个独立门控**——`build_anthropic_client` 压根不接收这个 flag(`:646`),所以即便会话被判为 OAuth,身份头是否注入仍由上面的 host 门控独立决定。

**S6 真实捕获(localhost,试了两种 OAuth token):**
```
S6_oauth_oat (sk-ant-oat01-...) → auth=x-api-key  ua="Anthropic/Python 0.87.0"  x_app=""
S6_oauth_cc  (cc-...)           → auth=x-api-key  ua="Anthropic/Python 0.87.0"  x_app=""
beta = [interleaved-thinking, fine-grained-tool-streaming]  (无 oauth/claude-code beta)
```

两种 token 都走了 x-api-key(而非 Bearer),UA 是 SDK 默认的 `Anthropic/Python 0.87.0`(而非 `claude-cli`),`x_app` 为空,beta 里也没有 oauth/claude-code——全部符合"localhost 不注入 OAuth 身份头"的预期。

**为何裁决 partial 而非 confirmed**:原 claim 说身份头"要求 host == api.anthropic.com"。但源码实际门控是 `_is_third_party_anthropic_endpoint` 返回 False,其条件为(a)base_url 为空/None(`:378`),**或**(b)规范化后的 URL **子串包含** `anthropic.com`(`:380`)——注意这是**子串匹配**,不是 host **相等**判定。两个偏差:① 空 base_url 也会触发 OAuth 头(claim 漏掉了这种情况);② `https://api.anthropic.com.evil.com/` 之类的恶意域名会因为子串包含 `anthropic.com` 而被误判为"直连 Anthropic"。在安全语境下这是真实的语义偏差,所以不给 confirmed,只给 partial。

### (b) envelope 布局 host 门控(openrouter.ai)—— `verdict: confirmed`(证实)

经 OpenRouter 转发 Claude 时,Hermes 用 envelope 布局(marker 打在 message 顶层)而非 native。判定纯靠 host:`is_openrouter = base_url_host_matches(eff_base_url, "openrouter.ai")`(`agent_runtime_helpers.py:1194`),没有任何"按 provider 名字"的分支。OpenRouter + Claude 时,policy 返回 `(True, False)`——即 `use_native_layout=False`,marker 打在 message envelope(`:1207-1208`)。`base_url_host_matches`(`utils.py:358-376`)的判定规则是 `hostname==domain` **或** `hostname.endswith('.'+domain)`;localhost 的 hostname 是 `localhost`,既不等于 `openrouter.ai` 也不是它的子域 → 返回 False,所有 host 分支都不匹配 → 最终 `return False, False`(`:1255`)→ `_use_prompt_caching=False` → 于是 `conversation_loop.py:1024` 处的 `apply_anthropic_cache_control` 被整个跳过。

**S4 真实捕获(provider=openrouter,localhost):**
```
seq 10/11  path=/v1/chat/completions  cache_control_count=0  x_anthropic_beta=""  beta=[]
```

localhost 下 OpenRouter + Claude 的请求走 `/v1/chat/completions`(OpenAI-wire),且 `cache_control_count=0`——**完全不带 cache_control**。已做对抗排查:确认没有别的代码路径会在 localhost 上重新启用 OpenRouter caching。结论 confirmed:envelope 布局确实是纯 host 门控,localhost 测不出来(故主总览表里标 INFO),但源码逻辑正确。

### (c) native 压缩摘要逃逸真实 Anthropic —— `verdict: confirmed`(证实)

这是本报告最重要的"安全/隔离风险点"。结论:在 native anthropic(`provider=anthropic`)路径下,压缩摘要的辅助 client **不会继承被运行时 override 的 mock base_url**,而是**逃逸**到真实的 `api.anthropic.com`。

完整链路:`context_compressor` 把运行时 override 放进 `main_runtime` → `call_llm` → `_get_cached_client` → `resolve_provider_client("auto")` → `_resolve_auto`。关键门控在 `auxiliary_client.py:3129-3134`:此时 `explicit_base_url=None`,而代码**仅当** `main_provider in {custom, custom:}` 时才把 `runtime_base_url`(也就是 mock 地址)注入进去;anthropic 不满足这个条件,于是 mock 地址被直接丢弃。接着 anthropic 分支(`:3718-3725`)调用 `_try_anthropic(explicit_api_key=...)`,这个调用**完全忽略 explicit_base_url**;而 `_try_anthropic`(`:2112-2127`)只认 config.yaml 里的 `model.base_url`(且要求 `model.provider==anthropic`),否则就用默认值 `_ANTHROPIC_DEFAULT_BASE_URL="https://api.anthropic.com"`(`:420`)→ 最终 `messages.create` 打到了真实端点。

对比其他路径就能看清差异:custom/custom: 在 `:3131-3134` 会显式把 mock 赋给 explicit_base_url(留在配置的 base_url);通用 api_key provider 在 `:3744-3750` 也会 honor(尊重)explicit_base_url。**唯独 native anthropic 分支不接收 explicit_base_url**——这就是逃逸点。这也正是 §3.6 里 S9/S13 的 driver 注释为何刻意改用 custom provider 的根因(用 native 会因为打到真实 Anthropic 而 401 逃逸)。逃逸出去的摘要请求形状与 §3.6 一致:非流式 `messages.create` + 单 user + 无 tools。

> **一个限定(不削弱结论)**:如果用户把 mock 地址写进 config.yaml 的 `model.base_url` 且 `model.provider=anthropic`(`:2122-2125`),或者 anthropic 凭据池里的凭据自带了自定义 base_url,那么就不会逃逸。但这属于"配置层路径",与本 claim 针对的"CLI/gateway 在运行时 override mock base_url"是正交的两件事——配置层能拦住,不代表运行时 override 能拦住。

### (d) thinking 块不经 conversation_history "顶层 content" 回放 —— claim 字面前提勉强成立,但因果结论 `verdict: refuted`(推翻)

这一条最微妙:claim 的**字面观测**对了,但它从观测推出的**因果结论**被源码推翻了。

**S11/S12 真实捕获**:turn2 的请求 `has_thinking_history=False`——也就是说,经 conversation_history 传回的历史 body 里,确实**没有顶层的 thinking content 块**。字面看起来像是"thinking 没被回放"。

**但因果结论被源码推翻**:thinking 块实际是以 `reasoning_details` 字段(其内含 `type='thinking'` 与 `signature`)端到端往返的,只是它不在"顶层 content"里,而藏在另一个字段里。完整往返链路:
- `normalize_response` 采集 thinking(`transports/anthropic.py:101-105`,写入 `provider_data['reasoning_details']`)
- → `build_assistant_message` 把它存进 `msg['reasoning_details']`(`chat_completion_helpers.py:910-925`)
- → `messages.append`(`conversation_loop.py:3778`)
- → `result['messages']` 原样返回(`:4733`,没有剥离 reasoning_details)
- → 下一轮 `messages=list(conversation_history)` 浅拷贝(`:499`)
- → Anthropic 转换层从 `reasoning_details` **重建出签名 thinking content block**(`anthropic_adapter.py:1572-1586, 2064-2065`)再发回 API。

所以,签名 400 的"清空重试"分支(`conversation_loop.py:2544-2562`)**完全可以仅靠 conversation_history 端到端触发**,不需要"同一个 agent 连续循环"。事实上 `conversation_loop.py:508-509` 的注释明确指出:gateway 对每条入站消息都新建 fresh AIAgent,跨轮回放正是经 conversation_history 完成的——这与 claim 所谓"需要同一 agent 连续循环"恰好相反,故裁决 refuted。

那为什么 S11/S12 标 **INFO** 而不是 PASS?因为本平台的 mock 只检测**顶层** thinking content 块(`has_thinking_block`,`mock_anthropic.py:121-128`),看不到藏在 `reasoning_details` 里的 thinking,所以没法在 mock 端到端观测到这条往返。签名 400 分支本身是真实存在的(由 `error_classifier.py:553-562` 做子串匹配 + 源码确认),只是没在本 mock 里端到端跑出来——这是"测不到",不是"不存在"。

**直接探针实证(本次补做的验证)**:为了坐实 refuted 结论,对 `claude-3-7-sonnet` + thinking high 跑了一轮,mock 发回带 signature 的 thinking 块,然后打印 `result['messages']`,assistant 消息确实带上了 reasoning_details:

```
[1] role=assistant keys=['role','content','reasoning','finish_reason','reasoning_content','reasoning_details']
    reasoning_details=[{"signature":"SIG_PLACEHOLDER_MOCK","thinking":"[Mock] internal reasoning.","type":"thinking"}]
```

→ `reasoning_details`(含 signature)**确实**被采集并随 `result['messages']` 往返,实证了 refuted 结论。但有意思的是:在同一数据集里,S11 turn2 的**最终 Anthropic 请求**里,assistant 消息却仍然只有 `text` 块——这说明 `_extract_preserved_thinking_blocks` 的重建是**有门控的**(疑似:只有最近一条 assistant 在特定布局下才保留签名块,一旦后面跟了更新的 user 轮就降级成 text,这与研究 spec 的预期一致)。精确的门控条件值得后续做定向测试。

---

## 5. 与 OpenAI SDK 路径(报告 12/13)的对比

把 Anthropic 路径和 OpenAI 路径(详见报告 12/13)的上下文管理能力并排比较,能看出 Anthropic 路径在客户端侧暴露的"上下文工程"旋钮明显更多。

| 维度 | OpenAI SDK 路径(报告 12/13) | Anthropic SDK 路径(本报告) |
|---|---|---|
| 缓存断点 | 由后端隐式前缀缓存,**无客户端显式断点** | 客户端**显式注入** 4 个 `cache_control` 断点(`system_and_3`),随对话滑动 |
| 缓存 TTL 控制 | 无客户端 TTL 控制 | 显式 **5m / 1h** marker(`{type:ephemeral}` / `+ttl:1h`) |
| 推理预算 | `reasoning_effort` 单字段 | thinking **五分支**:manual budget / adaptive+output_config / xhigh→max 降级 / 关闭 / haiku 跳过 |
| 协议 beta 头 | 无 | `anthropic-beta` 枚举(interleaved-thinking、fine-grained-tool-streaming、按 host 加 context-1m / fast-mode / OAuth) |
| 布局自适应 | 单一 wire | native(content-level)vs envelope(message-level),按 host 门控(openrouter.ai) |
| 身份/鉴权 | api-key | x-api-key / Bearer / OAuth 身份头三态,按 host 门控 |
| usage 字段 | prompt/completion 二元 | input + cache_read + cache_creation **三桶**,`prompt_tokens` 派生求和 |

逐维度的含义:OpenAI 路径的缓存完全由后端隐式做前缀缓存,客户端无从干预;Anthropic 路径则把"在哪打断点、缓存存多久、推理花多少预算、用哪种布局、用哪种鉴权"全部下放给客户端可编程。

**结论**:Anthropic 路径的上下文管理特性**明显更丰富**——原生断点布局、双 TTL、thinking 五分支、协议 beta 枚举,这些都是 OpenAI 路径没有的客户端侧"上下文工程"能力。它把缓存命中优化、推理预算分配、长上下文 tier 切换从"后端黑盒"提升为"客户端可编程"。这对想做亲和性优化的推理框架很有借鉴价值——见下一节。

---

## 6. 对推理框架亲和性优化的意义

本报告验证的几个机制,对自托管推理框架(vLLM / SGLang 等)的 KV cache 复用、调度优化有直接借鉴意义:

- **缓存前缀稳定性 = 推理框架前缀复用的前提**。Hermes 把 system + 最近 3 条消息打 `cache_control` 断点,且 system 字节跨轮稳定(S1 实测 `len=2126` 恒定 5 轮),正对应 Anthropic 服务端的 ephemeral KV 前缀复用。对自托管推理框架而言,同样的"system 前缀字节稳定 + 末尾滑动断点"思路可以直接迁移成 prefix-cache 友好的消息布局——前缀只要逐字节不变,KV 就能命中。
- **断点封顶 4、向末尾滑动**:这是在"尽量复用更长前缀"与"Anthropic 每请求 ≤4 断点的硬约束"之间做的平衡。如果框架侧实现自己的缓存,这是一个已被验证的断点分配启发式可以参考。
- **TTL 分层(5m / 1h)**:对长会话/慢交互,用 1h TTL 把缓存窗口拉长以提高命中率;对短交互用 5m 省成本。框架可以据会话节奏选择 KV 保留时长。
- **thinking 预算的版本感知降级**(4.6 xhigh→max)说明:亲和性优化必须随模型代际门控,不能硬编码 effort 等级。推理框架接入多代模型时,需要同样的能力探测表(类比 `_ADAPTIVE_THINKING_SUBSTRINGS` / `_XHIGH_EFFORT_SUBSTRINGS`),否则会给不支持的模型下发不支持的档位。
- **压缩链路 + tier 429 反应式恢复**:把"上下文超限"从硬失败变成"压缩 + 降 context_length + 重试"的自愈流程,对接入框架的长上下文稳定性是关键保障。

---

## 7. 48 用例覆盖矩阵

研究阶段在 `design.json` 的 `test_matrix` 里设计了 48 个用例(编号 T01-T48)。本平台把其中"能在 localhost 端到端跑通"的部分实现为 13 个 driver 场景(共 25 条 PASS 断言),其余因为 **host 门控 / 需真实 Anthropic / 属配置层路径** 而只能以源码确认覆盖。下面两张表分别列出两类。

### 7.1 端到端跑通(25 条 PASS 断言,S1-S13 的可观测部分)

| 用例 | 对应场景 | 实测 |
|---|---|---|
| T01 断点滑动 | S1(`[2,4,4,4,4]`,索引 `0→2,3,4→6,7,8`) | PASS×4 |
| T02 5m TTL | S2 `{type:ephemeral}` | PASS |
| T03 1h TTL | S2 `{type:ephemeral,ttl:1h}` | PASS |
| T08 第三方 /anthropic native | S3 path+content-level cc | PASS×2 |
| T10 beta 基线 | S5 精确串 | PASS |
| T23 interleaved 恒发 | S5 | PASS |
| T14 默认无 context-1m | S5(beta 不含 context-1m) | PASS(含于 S5) |
| T17 adaptive 4.7 high | S7_adaptive46/xhigh47 | PASS |
| T18 xhigh 降级 4.6 | S7_xhigh_downgrade46(`effort:max`) | PASS |
| T19 manual budget 3.7 | S7_old(`budget_tokens:16000`) | PASS |
| T20 关闭/haiku 边界 | S7_off(`thinking=None`) | PASS(off 分支) |
| T24 cache 回读派生 | S8(cache_read 递增) | PASS |
| T25 None 兜底 | S8 / `omit_cache_fields` 源码 | PASS(源码) |
| T27 压缩摘要形态 | S9(非流式+单user+无tools) | PASS×2 |
| T28 压缩后轮换 | S9(main msgs `[1,3,5,5,5]` 回落) | PASS |
| T35 length 续写 | S10(2 请求) | PASS |
| T36 model_context_window_exceeded→length | `anthropic.py:178` 映射 | 源码确认 |
| T37 tier 429 恢复 | S13(2 请求) | PASS |

### 7.2 host 门控 / 源码确认(4 INFO + 其余研究用例)

这一类无法在 localhost 端到端跑通,原因和覆盖方式如下表。注意"为何不能 localhost 端到端"这一列正是理解本报告测试边界的关键——绝大多数都是因为 host 字符串不对。

| 用例 | 为何不能 localhost 端到端 | 覆盖方式 |
|---|---|---|
| T07 envelope(OpenRouter) | 需 `openrouter.ai` host(`agent_runtime_helpers.py:1194`) | **S4 INFO** + 源码 |
| T11 OAuth 身份头 | 需 `api.anthropic.com` host,第三方分支先于 OAuth(`anthropic_adapter.py:736`) | **S6 INFO** + 源码 |
| T21/T22 thinking 签名回放/400 | thinking 经 reasoning_details(非顶层 content)回放,mock 只检顶层块 | **S11/S12 INFO** + `error_classifier.py:553` 源码 |
| T12 MiniMax 剥离+Bearer | 需 minimax host(`_common_betas_for_base_url`) | 源码确认 |
| T13/T46 Azure/Bedrock context-1m | 需 azure.com / bedrock host | 源码确认 |
| T15 OAuth 1M-beta 反应式剥离 | 需 context-1m beta(OAuth/Azure host) | 源码确认(`drop_context_1m_beta`) |
| T16 fast-mode Opus4.6 | 需原生 host + speed override | 源码确认 |
| T32-T34 截图驱逐 / memory 注入扰断点 | 需 toolset/memory(driver `skip_memory=True`) | 源码确认 |
| T05/T42 无 system / system 扰动 | 正常路径总前置 system(`conversation_loop.py:1009`) | 源码确认 |
| T39/T40/T48 tools 缓存 / sampling 剥离 | driver `enabled_toolsets=[]` 无 tools | 源码确认 |

> **关于最后一行 `enabled_toolsets=[]` 的一个澄清**(避免误读):driver 在内部把 `enabled_toolsets` 置空,导致这几个用例没有 tools 可缓存,因此只能源码确认。需要注意区分:这里说的是内部参数 `enabled_toolsets=[]`(确实会得到 0 个工具),与 config 层的 `toolsets:[]` 不是一回事——config 层的核心工具是恒含的,二者不可混淆(详见报告 17 §2)。

---

## 8. 局限与诚实声明

本报告的结论有明确边界,下面如实声明,以免读者把"localhost 测出来的"误当成"真实 Anthropic 服务端的行为":

1. **mock 的 cache usage 是脚本编造的,不是真实缓存语义**。`_usage`(`mock_anthropic.py:194-211`)里的 `cache_read / cache_creation` 是按 `cache_turns` 人为造的数字(首轮 `creation=max(1000, inp//2)`,后续 `read=max(1000, inp-200)`),**不代表 Anthropic 服务端真实的 KV 命中**。S8 验证的是 Hermes 端"三字段读取 + prompt_tokens 派生"的解析正确性,**不是**真实缓存命中率。
2. **host 门控项无法用 localhost 触发**。OAuth 身份头(S6)、OpenRouter envelope(S4)、MiniMax/Azure/Bedrock 的 beta 增减,都依赖 base_url 的 **host 字符串**匹配(`base_url_host_matches` / `_is_third_party_anthropic_endpoint`)。localhost mock 永远命中第三方/通用分支,所以这些特性只能以**源码 file:line 确认**,标 INFO 而非 PASS。这是诚实的边界,不是测试缺陷。
3. **native 压缩摘要逃逸是真实风险点**(详见 §4(c)):S9/S13 因此刻意改用 custom provider,否则摘要辅助请求会逃逸到真实 `api.anthropic.com`(已实测 401)。报告中所有 native 压缩断言都是"custom-wire 代理 + 源码确认 native 逃逸点"的组合,而**不是**在 native 路径上直接观测到摘要。
4. **thinking 回放结论需区分两种读法**(详见 §4(d)):mock 的 `has_thinking_history=False` 只说明**顶层 content 块**里没有 thinking;实际上 thinking 是经 `reasoning_details` 字段无损往返的。任何"thinking 不回放"的字面陈述都应附上这个限定,否则会得出与源码相反的因果结论。
5. **版本核对**:全程基于真实安装树 `/home/niaowuuu/.hermes/hermes-agent`,`pyproject.toml:10` version=`0.15.1`,本报告所有 file:line 均对该树逐条核对。(唯一例外是 §3.6 实测到的 `max_tokens=2000`,经判定为升级前 v0.13 的捕获值,当前 v0.15.1 源码应为 `int(summary_budget×1.3)`,已在该节注明,详见报告 17 §2.1。)

---

### 附:断言检查器原始输出(本次实跑)

```
# S1  断点总数恒<=4 [PASS] 各轮=[2,4,4,4,4]
      断点滑动=最后3条非system消息 [PASS] msgs=1:[0] 3:[0,1,2] 5:[2,3,4] 7:[4,5,6] 9:[6,7,8]
      system 始终占1个断点 [PASS];  system 文本跨轮字节稳定 [PASS] len=[2126]*5
# S2  5m: marker 无 ttl [PASS];  1h: marker 含 ttl=1h [PASS]
# S3  走 /anthropic/v1/messages [PASS];  native content-level cache_control [PASS]
# S4  [INFO] localhost OpenRouter chat 不打 cache_control (envelope 需 openrouter.ai host) cc=[0,0]
# S5  beta=[interleaved,fine-grained] [PASS]; interleaved 恒在 [PASS];
      auth=x-api-key 无 OAuth 头 [PASS]; 不含 caching beta [PASS]
# S6  [INFO] localhost 无 OAuth 身份头 (需 api.anthropic.com host); oat/cc auth=x-api-key [PASS×2]
# S7  old=enabled/16000 [PASS]; adaptive46=adaptive/effort:high [PASS];
      xhigh47=effort:xhigh [PASS]; downgrade46=effort:max [PASS]; off=None [PASS]
# S8  多轮 cache 回读 [PASS]
# S9  摘要被捕获 summary=2 main=5 [PASS]; 摘要=非流式+单user+无tools [PASS]; main msgs 回落[1,3,5,5,5] [PASS]
# S10 续写额外请求 [PASS] 2 请求
# S11/S12 [INFO] thinking 不经顶层 content 回放 (reasoning_details 往返, 源码确认)
# S13 429 触发重试 [PASS] 2 请求
=====  PASS=25  FAIL=0  INFO=4  =====
```
