# 14 — Anthropic SDK 路径上下文管理深度测试

> 目标:Hermes Agent **v0.15.1**（用户实际安装树 `/home/niaowuuu/.hermes/hermes-agent`,`pyproject.toml:10` version=`0.15.1`)
> 范围:Anthropic Messages SDK 路径(`api_mode=anthropic_messages`)的上下文管理特性 —— 原生 prompt-cache 断点布局 / 5m·1h TTL / `anthropic-beta` 枚举 / thinking 五分支 / cache usage 三字段聚合 / 上下文压缩链路 / `length` 续写 / long-context tier 429。
> 方法:增强版可编程 mock Anthropic 后端 + 真实 `AIAgent.run_conversation()` driver + 程序化断言检查器,捕获 37 个真实请求,**25 PASS / 0 FAIL / 4 INFO**。

---

## 1. 平台架构

测试平台位于 `/mnt/d/Workspace/Survey/hermes/DeepseekOutput/anthropic_platform/`,由四件套组成。它与报告 12/13 的 OpenAI 平台共享"mock 控制端点 + 真 AIAgent driver + 断言检查器"骨架,但 mock 把 **usage / cache / stop_reason / content-block / 错误注入** 全部做成"按场景可编程",以触发 Anthropic 路径独有的上下文管理特性。

### 1.1 增强版 mock 后端 — `mock_anthropic.py`

一个线程化 `http.server`(`ThreadingHTTPServer`,`mock_anthropic.py:406-408`),默认端口 8910。核心能力:

- **协议正确的 Anthropic SSE**(`_anthropic`,`mock_anthropic.py:276-325`):依次发 `message_start`(带 `usage`)→ 每个 content block 的 `content_block_start/delta/stop` → `message_delta`(带 `stop_reason` + output usage)→ `message_stop`。真实 `anthropic` SDK 0.87.0 可经 `stream.get_final_message()` 正常聚合。
- **可编程 content blocks**(`_emit_block`,`:327-352`):`text` / `thinking`(+`signature_delta`)/ `redacted_thinking`(+`data`)/ `tool_use`(+`input_json_delta`)。
- **可编程 usage**(`_usage`,`:194-211`):`message_start.usage` 给 `input_tokens / cache_read_input_tokens / cache_creation_input_tokens`,`message_delta.usage` 给 `output_tokens`。`cache_turns=True` 模拟首轮 `creation>0/read=0`、后续 `read` 增长(命中);`force_prompt_tokens` 直接抬高 `input_tokens` 触发压缩;`omit_cache_fields` 不发 cache 字段以测 None 兜底。
- **可编程 stop_reason**:`end_turn`(默认)/ `max_tokens`(触发 `length` 续写),`stop_reason_once` 仅作用下一个主请求。
- **内容门控的错误注入**(`_maybe_error`,`:254-273`):
  - thinking 签名 400 —— body 含 thinking 块时触发(`has_thinking_block`,`:121-128`)。
  - OAuth 1M-beta 400 —— `anthropic-beta` 含 `context-1m` 时触发。
  - long-context tier 429 —— 下一主请求触发。
  - 通用 `error_once`(status+message 直配)。
- **请求录制**(`_record`,`:151-191`):headers 脱敏(`_SENSITIVE`,`:49`)+ 最终 body 落 `anthropic_requests.jsonl`,并预计算 `cache_control_hits`(`enumerate_cache_control`,`:84-108` 递归扫 system/messages-envelope/content-block/tools)、`ttl_forms`、`system` 指纹、`has_thinking_history`、`thinking_field`、`output_config` 等切片,供字节稳定断言。

**控制端点**:`POST /__mock/control`(下发配置,可 `reset`)、`GET /__mock/snapshots`、`POST /__mock/reset`。
**业务端点**:`POST /v1/messages`(原生)、`POST /anthropic/v1/messages`(第三方 anthropic-wire)、`POST /v1/chat/completions`(OpenAI-wire 基线,验 `x-anthropic-beta` 旁路键名)、`GET /v1/models`、`POST /api/show`。

### 1.2 driver — `driver_anthropic.py`(13 个场景 S1-S13)

用真实 `AIAgent.run_conversation()` 驱动。关键设计点:

- `build_agent`(`:47-78`)统一构造 agent,可 override `_cache_ttl`、`context_compressor.{context_length,threshold_tokens,protect_last_n,protect_first_n}`。
- `converse`(`:81-95`)把上一轮 `result["messages"]` 作为 `conversation_history` 传回下一轮 —— 这正是 gateway 跨轮回放的真实路径(每条入站消息新建 fresh AIAgent)。
- driver 在每个场景前 `pop` 掉 `ANTHROPIC_API_KEY/OPENROUTER_API_KEY/...` 环境变量(`:23-24`),避免凭据泄漏污染 provider 解析。

13 个场景映射:S1 断点滑动、S2 TTL 5m/1h、S3 第三方 native、S4 envelope、S5 beta 基线、S6 OAuth 身份头、S7 thinking 五分支、S8 cache 回读、S9 压缩链路、S10 length 续写、S11 签名 400 重试、S12 redacted_thinking、S13 tier 429。

### 1.3 断言检查器 — `check_assertions.py`

读取 `anthropic_requests.jsonl`,逐场景程序化校验,输出 `PASS / FAIL / INFO`。`INFO` 专门标注**源码确认 / host 门控**类断言(localhost mock 无法触发,但有 file:line 证据)。`nonsys_idx`(`:27-34`)从 `cache_control_hits` 提取 `messages[i].content[j]` 索引集合用于滑动断言。

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

**最新断言汇总(本次实跑,37 请求):PASS=25  FAIL=0  INFO(源码/host 门控)=4。**

---

## 2. 特性总览表

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

布局策略实现于 `prompt_caching.py:49-79`,模块 docstring(`:3-4`)明确"Single layout: system_and_3。4 cache_control breakpoints — system prompt + last 3 non-system messages"。算法:

```
breakpoints_used = 0
if messages[0].role == "system":            # prompt_caching.py:70-72
    mark(messages[0]); breakpoints_used += 1   # system 占 1 个断点
remaining = 4 - breakpoints_used            # :74 总数封顶 4
non_sys = [非 system 消息的下标]              # :75
for idx in non_sys[-remaining:]:            # :76-77 末尾切片 → 向数组末尾滑动
    mark(messages[idx])
```

`_apply_cache_marker`(`:15-38`)在 native 路径下,把 marker 打到 content 列表的**最后一个 block**(`:35-38`);若 content 是字符串则先包成 `[{type:text,text,cache_control}]`(`:29-32`)。这解释了为什么 system 是 list 时,marker 落在**最后一个** system block。

**S1 真实捕获(native anthropic,5 轮,system 双块结构):**

| seq | n_messages | 断点总数 | 断点位置 |
|---|---|---|---|
| 1 | 1 | **2** | `system[1]`, `messages[0].content[0]` |
| 2 | 3 | **4** | `system[1]`, `messages[0,1,2].content[0]` |
| 3 | 5 | **4** | `system[1]`, `messages[2,3,4].content[0]` |
| 4 | 7 | **4** | `system[1]`, `messages[4,5,6].content[0]` |
| 5 | 9 | **4** | `system[1]`, `messages[6,7,8].content[0]` |

> system 断点固定(此处 system 是双块 list:`system[0]` 57 字节身份串无 marker、`system[1]` 2069 字节 + marker,marker 落最后一块);非 system 三断点沿数组末尾滑动 `0,1,2 → 2,3,4 → 4,5,6 → 6,7,8`。断点总数恒 ≤4(各轮 `[2,4,4,4,4]`)。

**system 字节跨轮稳定**(缓存前缀稳定性的前提):`system.len = [2126,2126,2126,2126,2126]` —— 5 轮完全一致(`check_assertions.py` PASS)。

第一条 user 消息的真实片段(content 字符串被包成 text block 并打 marker):
```json
{"role":"user","content":[{"type":"text","text":"Turn 1: please acknowledge.","cache_control":{"type":"ephemeral"}}]}
```

> 边角(不影响 claim,已限定 system-present):若首条非 system(`breakpoints_used=0`),`remaining=4`,4 个断点全落最后 4 条非 system 消息,名义上退化为"last 4"。但正常路径 `conversation_loop.py:1009` 总会前置 `{role:system}`,故实际运行布局即 system_and_3。

### 3.2 5m / 1h TTL marker

`_build_marker`(`prompt_caching.py:41-46`):`marker={type:ephemeral}`,**仅当** `ttl=="1h"` 才加 `marker["ttl"]="1h"`。

**S2 真实捕获:**
```
S2_ttl_5m  → markers = [{"type":"ephemeral"}, {"type":"ephemeral"}]
S2_ttl_1h  → markers = [{"type":"ephemeral","ttl":"1h"}, {"type":"ephemeral","ttl":"1h"}]
```
未知值(`30m`)经 `_build_marker` else 分支退化为 `{type:ephemeral}`(无 ttl,等价 5m),实测确认。TTL 切换与 `anthropic-beta` 头**完全解耦**:`_common_betas_for_base_url`(`anthropic_adapter.py:539-568`)签名只接受 `base_url/drop_context_1m_beta`,无任何 ttl 参数。

### 3.3 `anthropic-beta` 枚举

`_COMMON_BETAS`(`anthropic_adapter.py:261-264`)仅含两项:`interleaved-thinking-2025-05-14`、`fine-grained-tool-streaming-2025-05-14`。

**S5 真实捕获(native api-key):**
```
anthropic-beta: "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"
auth_kind: x-api-key   user_agent: "Anthropic/Python 0.87.0"   x_app: ""
```
- `interleaved-thinking` 恒在,与 thinking 注入解耦(thinking 关也带)。
- **不含** `prompt-caching` / `extended-cache-ttl` beta —— 缓存已 GA,全仓 `agent/` 源码 0 命中。
- OAuth-only beta(`claude-code-20250219`、`oauth-2025-04-20`)仅在 OAuth host 门控命中时追加(`:280-283`),localhost 不触发(见 §4)。

### 3.4 thinking 五分支(含 4.6+xhigh→max 降级)

注入逻辑 `anthropic_adapter.py:2249-2269`,预算表 `THINKING_BUDGET={xhigh:32000,high:16000,medium:8000,low:4000}`(`:58`)。门控子串:`_ADAPTIVE_THINKING_SUBSTRINGS=("4-6","4.6","4-7","4.7","4-8","4.8")`(`:85`),`_XHIGH_EFFORT_SUBSTRINGS=("4-7","4.7","4-8","4.8")`(`:80`)。

**S7 真实捕获(五分支全 PASS):**

| 场景 | model | `thinking` 字段 | `output_config` |
|---|---|---|---|
| 老模型 manual | `claude-3-7-sonnet-20250219` | `{type:enabled, budget_tokens:16000}` | `None` |
| 4.6 adaptive | `claude-opus-4-6` | `{type:adaptive, display:summarized}` | `{effort:high}` |
| 4.7 xhigh 保持 | `claude-opus-4-7` | `{type:adaptive, display:summarized}` | `{effort:xhigh}` |
| **4.6 xhigh→max 降级** | `claude-opus-4-6` | `{type:adaptive, display:summarized}` | `{effort:max}` |
| 关闭 | `claude-sonnet-4` `enabled:False` | `None` | `None` |

降级逻辑:`if adaptive_effort=="xhigh" and not _supports_xhigh_effort(model): adaptive_effort="max"`(`:2260-2261`)—— 4.6 不在 `_XHIGH_EFFORT_SUBSTRINGS` 名单,故 xhigh 被压成 max。老模型 manual 分支还强制 `temperature=1` 并抬 `max_tokens`(`:2268-2269`);`haiku` 整块跳过(`:2249`)。

### 3.5 cache usage 三字段聚合

SDK `get_final_message()` 返回原生 Anthropic Message,`.usage` 即 SDK 流式聚合后的最终值(`chat_completion_helpers.py:2041`,注释 `:1964-1967`)。`normalize_usage` 的 `anthropic_messages` 分支(`usage_pricing.py:723-727`)把 `input_tokens / cache_read_input_tokens / cache_creation_input_tokens` 各自独立 `_to_int` 读取,塞进 `CanonicalUsage` 三个**独立桶**(`:764-770`),缺省经 `getattr(...,0)` + `_to_int(int(value or 0))`(`:548-552`)双重兜底为 0。

> **重要校正**:三者之和(`input + cache_read + cache_write`)发生在 `CanonicalUsage.prompt_tokens` `@property`(`usage_pricing.py:39-41`),**不是** normalize_usage 本身。normalize_usage 刻意保持三桶分离(与 codex/openai 分支从总数中减去 cache 的语义相反)。详见 §4。

**S8 真实**:driver 日志显示 `cache_read` 跨轮递增(首轮 creation>0/read=0,后续 read 增长),Hermes 端正确回读(3 请求,PASS)。`omit_cache_fields` 路径下三字段缺省兜底为 0(源码确认)。

### 3.6 上下文压缩链路(摘要非流式 / 单 user / 无 tools)

压缩摘要构造于 `context_compressor.py:1380-1395`:`task="compression"`,`messages=[{role:user,content:prompt}]`,**无 tools**,经 `call_llm()` 走非流式 `messages.create`(`auxiliary_client.py:967,1012`)。

**S9 真实捕获(第三方 anthropic-wire,custom provider,5 主请求 + 2 摘要):**

| seq | kind | stream | n_messages | tools | path |
|---|---|---|---|---|---|
| 23 | main | True | 1 | False | `/anthropic/v1/messages` |
| 24 | main | True | 3 | False | `/anthropic/v1/messages` |
| 25 | main | True | 5 | False | `/anthropic/v1/messages` |
| **26** | **summary** | **False** | **1** | **False** | `/v1/v1/messages` |
| 27 | main | True | 5 | False | `/anthropic/v1/messages` |
| **28** | **summary** | **False** | **1** | **False** | `/v1/v1/messages` |
| 29 | main | True | 5 | False | `/anthropic/v1/messages` |

摘要请求 body(seq 26):`keys=[max_tokens, messages, model]`,`stream=None`,`tools=None`,单 user 消息,content 以 `"You are a summarization agent creating a context checkpoint..."` 开头,`max_tokens=2000`。主请求 `n_messages` 在压缩后回落不再单调增(`[1,3,5,5,5]`,封顶 5,PASS)。

> 注:此处刻意用 **custom provider** 而非 native。native anthropic 压缩摘要的辅助 client 会逃逸到真实 `api.anthropic.com`(见 §4c),用 custom 才能把摘要留在 localhost mock。摘要 path 显示 `/v1/v1/messages` 是 mock 对 custom base_url 的 SDK 拼接产物,不影响形态断言。

### 3.7 `length` 续写

映射 `stop_reason → finish_reason` 在 `transports/anthropic.py:175,178`(`max_tokens`、`model_context_window_exceeded` 均 → `length`),在 anthropic 路径上由 `conversation_loop.py:1606` 应用。`finish_reason=="length"`(`:1628`)且无 tool_call 时:`length_continue_retries += 1`(`:1726`),`< 3` 门控(`:1732`),注入续写文本(`_get_continuation_prompt`,文本定义 `:343-348`,role 实为 **user**、content 以 `[System: ...]` 开头)并 `restart_with_length_continuation=True`(`:1760-1769`);每次重启渐进提升输出预算 `_boost_base*(retries+1)` 封顶(`:3514-3528`),成功后 `length_continue_retries` 归零(`:4372`)。

**S10 真实捕获**:`stop_reason_once=max_tokens` → 2 请求(seq 30 `n_msgs=1` 原请求,seq 31 `n_msgs=3` 续写请求),PASS。

### 3.8 long-context tier 429

mock 注入 429(`message` 含 `extra usage`+`long context tier`,`mock_anthropic.py:265-268`)。**S13 真实捕获**:429 注入一次 → 降 context_length + 压缩 + retry → 2 请求(seq 36/37,均 `/anthropic/v1/messages`),PASS。

---

## 4. 关键发现专节(对抗验证)

四项核心结论分别经独立对抗验证(逐条 confirmed / partial / refuted + file:line 证据)。

### (a) OAuth 身份头 host 门控 —— `verdict: partial`

**机制(成立)**:OAuth 身份头(`auth_token` Bearer + UA `claude-cli` + `x-app:cli` + `oauth-2025-04-20`/`claude-code-20250219` beta)注入于 `anthropic_adapter.py:744-754`,但第三方分支 `elif _is_third_party_anthropic_endpoint(base_url)`(`:736`)**先于** OAuth 分支(`:744`)。localhost base_url 不含 `anthropic.com` 子串 → 第三方分支返回 True(`:382`)→ 注入 `kwargs["api_key"]`(x-api-key,`:741`),不注入 OAuth 头。会话 flag `_is_anthropic_oauth`(`agent_init.py:644-645`)是 token 前缀 + `provider==anthropic` 的判定,与身份头注入是**两个独立门控**(`build_anthropic_client` 不接收该 flag,`:646`)。

**S6 真实捕获(localhost,两种 OAuth token):**
```
S6_oauth_oat (sk-ant-oat01-...) → auth=x-api-key  ua="Anthropic/Python 0.87.0"  x_app=""
S6_oauth_cc  (cc-...)           → auth=x-api-key  ua="Anthropic/Python 0.87.0"  x_app=""
beta = [interleaved-thinking, fine-grained-tool-streaming]  (无 oauth/claude-code beta)
```

**为何 partial**:claim 说身份头"要求 host==api.anthropic.com"。源码实际门控是 `_is_third_party_anthropic_endpoint` 返回 False,其条件为(a)base_url 为空/None(`:378`),或(b)规范化 URL **子串包含** `anthropic.com`(`:380`)—— 这是子串匹配而非 host 相等。因此空 base_url 也触发 OAuth 头(claim 漏掉),且 `https://api.anthropic.com.evil.com/` 之类也会被误判为"直连"。在安全语境下这是真实语义偏差,故不给 confirmed。

### (b) envelope 布局 host 门控(openrouter.ai)—— `verdict: confirmed`

`is_openrouter = base_url_host_matches(eff_base_url, "openrouter.ai")`(`agent_runtime_helpers.py:1194`)纯 host 判定,无 provider-name 分支。OpenRouter+Claude → policy 返回 `(True, False)`(use_native_layout=False,marker 打 message envelope,`:1207-1208`)。`base_url_host_matches`(`utils.py:358-376`)要求 `hostname==domain or endswith('.'+domain)`;localhost hostname=`localhost`≠`openrouter.ai` → False,所有分支不匹配 → `return False,False`(`:1255`)→ `_use_prompt_caching=False` → `conversation_loop.py:1024` 的 `apply_anthropic_cache_control` 被跳过。

**S4 真实捕获(provider=openrouter,localhost):**
```
seq 10/11  path=/v1/chat/completions  cache_control_count=0  x_anthropic_beta=""  beta=[]
```
localhost 下 OpenRouter+Claude 的 chat/completions 请求**完全不带 cache_control**。已对抗排查:无其它路径在 localhost 重新启用 OpenRouter caching。

### (c) native 压缩摘要逃逸真实 Anthropic —— `verdict: confirmed`

native anthropic(`provider=anthropic`)路径的压缩摘要辅助 client **不继承被 override 的 mock base_url**,逃逸到真实 `api.anthropic.com`。链路:`context_compressor` 把运行时 override 放进 `main_runtime` → `call_llm` → `_get_cached_client` → `resolve_provider_client("auto")` → `_resolve_auto`。关键门控 `auxiliary_client.py:3129-3134`:`explicit_base_url=None`,**仅当** `main_provider in {custom, custom:}` 才把 `runtime_base_url`(mock)注入;anthropic 不满足 → mock 被丢弃。随后 anthropic 分支(`:3718-3725`)调 `_try_anthropic(explicit_api_key=...)` **完全忽略 explicit_base_url**;`_try_anthropic`(`:2112-2127`)只认 config.yaml 的 `model.base_url`(且要求 `model.provider==anthropic`),否则用 `_ANTHROPIC_DEFAULT_BASE_URL="https://api.anthropic.com"`(`:420`)→ `messages.create` 打到真实端点。

对比:custom/custom: 在 `:3131-3134` 显式把 mock 赋给 explicit_base_url(留在配置 base_url);通用 api_key provider 在 `:3744-3750` honor explicit_base_url。**唯独 native anthropic 分支不接收 explicit_base_url** —— 这是逃逸点。这正是 S9/S13 driver 注释为何刻意改用 custom provider 的原因(native 会 401 逃逸)。摘要形状与 §3.6 一致:非流式 `messages.create` + 单 user + 无 tools。

> 限定(不削弱 claim):若用户把 mock 写进 config.yaml `model.base_url` 且 `model.provider=anthropic`(`:2122-2125`),或 anthropic pool 凭据带自定义 base_url,则不逃逸 —— 但那属配置层路径,与 claim 针对的"CLI/gateway 运行时 override mock base_url"正交。

### (d) thinking 块不经 conversation_history "顶层 content" 回放 —— claim 字面前提勉强成立,但因果结论 `verdict: refuted`

**S11/S12 真实捕获**:turn2 请求 `has_thinking_history=False`(经 conversation_history 传回的历史 body 里 **没有顶层 thinking content 块**)。

**但 claim 推出的因果结论被源码推翻**:thinking 块以 `reasoning_details` 字段(含 `type='thinking'` 与 `signature`)端到端往返:
`normalize_response` 采集(`transports/anthropic.py:101-105` → `provider_data['reasoning_details']`)→ `build_assistant_message` 存入 `msg['reasoning_details']`(`chat_completion_helpers.py:910-925`)→ `messages.append`(`conversation_loop.py:3778`)→ `result['messages']` 原样返回(`:4733`,无剥离)→ 下一轮 `messages=list(conversation_history)` 浅拷贝(`:499`)→ Anthropic 转换层从 `reasoning_details` **重建签名 thinking content block**(`anthropic_adapter.py:1572-1586, 2064-2065`)发回 API。

因此签名 400 清空重试分支(`conversation_loop.py:2544-2562`)**完全可以仅靠 conversation_history 端到端触发**,无需"同一 agent 连续循环"。事实上 `conversation_loop.py:508-509` 注释指出 gateway 每条入站消息新建 fresh AIAgent,跨轮回放正是经 conversation_history 完成 —— 与 claim 所谓"需同一 agent 连续循环"相反。本平台 mock 因为只检测**顶层** thinking content 块(`has_thinking_block`,`mock_anthropic.py:121-128`),看不到藏在 `reasoning_details` 里的 thinking,所以 S11/S12 标 **INFO**(签名 400 分支由 `error_classifier.py:553-562` 子串匹配 + 源码确认,未在本 mock 端到端触发,而非"无法触发")。

**直接探针实证(本次补做)**:对 `claude-3-7-sonnet` + thinking high、mock 发带 signature 的 thinking 块,跑一轮后打印 `result['messages']`,assistant 消息确实带:

```
[1] role=assistant keys=['role','content','reasoning','finish_reason','reasoning_content','reasoning_details']
    reasoning_details=[{"signature":"SIG_PLACEHOLDER_MOCK","thinking":"[Mock] internal reasoning.","type":"thinking"}]
```

→ `reasoning_details`(含 signature)**确实**被采集并随 `result['messages']` 往返,实证了 refuted 结论。但同一数据集里 S11 turn2 的**最终 Anthropic 请求** assistant 消息仍只有 `text` 块 —— 说明 `_extract_preserved_thinking_blocks` 的重建是**有门控的**(疑似"仅最近一条 assistant 在特定布局下保留签名块,被更新的 user 轮跟随后降级为 text",与研究 spec 一致),精确门控值得后续定向测试。

---

## 5. 与 OpenAI SDK 路径(报告 12/13)的对比

| 维度 | OpenAI SDK 路径(报告 12/13) | Anthropic SDK 路径(本报告) |
|---|---|---|
| 缓存断点 | 由后端隐式前缀缓存,**无客户端显式断点** | 客户端**显式注入** 4 个 `cache_control` 断点(`system_and_3`),随对话滑动 |
| 缓存 TTL 控制 | 无客户端 TTL 控制 | 显式 **5m / 1h** marker(`{type:ephemeral}` / `+ttl:1h`) |
| 推理预算 | `reasoning_effort` 单字段 | thinking **五分支**:manual budget / adaptive+output_config / xhigh→max 降级 / 关闭 / haiku 跳过 |
| 协议 beta 头 | 无 | `anthropic-beta` 枚举(interleaved-thinking、fine-grained-tool-streaming、按 host 加 context-1m / fast-mode / OAuth) |
| 布局自适应 | 单一 wire | native(content-level)vs envelope(message-level),按 host 门控(openrouter.ai) |
| 身份/鉴权 | api-key | x-api-key / Bearer / OAuth 身份头三态,按 host 门控 |
| usage 字段 | prompt/completion 二元 | input + cache_read + cache_creation **三桶**,`prompt_tokens` 派生求和 |

**结论**:Anthropic 路径的上下文管理特性**明显更丰富** —— 原生断点布局、双 TTL、thinking 五分支、协议 beta 枚举,都是 OpenAI 路径没有的客户端侧"上下文工程"能力。这把缓存命中优化、推理预算分配、长上下文 tier 切换从"后端黑盒"提升为"客户端可编程"。

---

## 6. 对推理框架亲和性优化的意义

- **缓存前缀稳定性 = 推理框架(vLLM/SGLang 等)前缀复用的前提**。Hermes 把 system + 最近 3 条消息打 `cache_control` 断点、且 system 字节跨轮稳定(S1 实测 `len=2126` 恒定),正对应 Anthropic 服务端的 ephemeral KV 前缀复用。对自托管推理框架,相同的"system 前缀字节稳定 + 末尾滑动断点"思路可直接迁移为 prefix-cache 友好的消息布局。
- **断点封顶 4、向末尾滑动**:平衡了"复用尽量长的前缀"与"Anthropic 每请求 ≤4 断点"的硬约束。框架侧若实现自有缓存,这是一个经过验证的断点分配启发式。
- **TTL 分层(5m/1h)**:对长会话/慢交互,1h TTL 把缓存窗口拉长以提高命中率;短交互用 5m 省成本。框架可据会话节奏选择 KV 保留时长。
- **thinking 预算的版本感知降级**(4.6 xhigh→max)说明:亲和性优化必须随模型代际门控,不能硬编码 effort 等级 —— 推理框架接入多代模型时需同样的能力探测表(`_ADAPTIVE_THINKING_SUBSTRINGS` / `_XHIGH_EFFORT_SUBSTRINGS`)。
- **压缩链路 + tier 429 反应式恢复**:把"上下文超限"从硬失败变成"压缩 + 降 context_length + 重试",对接入框架的长上下文稳定性是关键保障。

---

## 7. 48 用例覆盖矩阵

研究阶段设计 48 用例(`design.json` `test_matrix`)。本平台把其中可在 localhost 端到端跑通的部分实现为 13 个 driver 场景(25 条 PASS 断言),其余因 **host 门控 / 需真实 Anthropic / 配置层路径** 而以源码确认覆盖。

### 7.1 端到端跑通(25 PASS 断言,S1-S13 部分)

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

---

## 8. 局限与诚实声明

1. **mock 的 cache usage 是脚本编造,非真实缓存语义**。`_usage`(`mock_anthropic.py:194-211`)的 `cache_read/cache_creation` 是按 `cache_turns` 人为造的数字(首轮 `creation=max(1000,inp//2)`,后续 `read=max(1000,inp-200)`),**不代表 Anthropic 服务端真实 KV 命中**。S8 验证的是 Hermes 端"三字段读取 + prompt_tokens 派生"的解析正确性,**不是**真实缓存命中率。
2. **host 门控项无法用 localhost 触发**。OAuth 身份头(S6)、OpenRouter envelope(S4)、MiniMax/Azure/Bedrock 的 beta 增减,都依赖 base_url **host 字符串**匹配(`base_url_host_matches` / `_is_third_party_anthropic_endpoint`)。localhost mock 永远命中第三方/通用分支,这些特性只能以**源码 file:line 确认**,标 INFO 而非 PASS。这是诚实的边界,不是测试缺陷。
3. **native 压缩摘要逃逸是真实风险点**(§4c):S9/S13 因此刻意改用 custom provider,否则摘要辅助请求会逃逸到真实 `api.anthropic.com`(已实测 401)。报告中所有 native 压缩断言均为"custom-wire 代理 + 源码确认 native 逃逸点"的组合,而非在 native 路径上直接观测到摘要。
4. **thinking 回放结论需区分两种读法**(§4d):mock `has_thinking_history=False` 只说明**顶层 content 块**无 thinking;实际 thinking 经 `reasoning_details` 字段无损往返。任何"thinking 不回放"的字面陈述都应附此限定,否则会得出与源码相反的因果结论。
5. **版本核对**:全程基于真实安装树 `/home/niaowuuu/.hermes/hermes-agent`,`pyproject.toml:10` version=`0.15.1`,本报告所有 file:line 均对该树逐条核对。

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
