# Hermes Agent — 所有传输层 Agent Hints 完整对比

> 测试环境：Hermes Agent **v0.15.1** · OpenAI SDK 2.24.0 · Anthropic SDK 0.87.0
> Mock 后端：`/tmp/mock_server_v3.py`(监听 `127.0.0.1:19998`)
> 测试脚本：`/tmp/test_all_transports.py`
>
> 📌 经真实安装 venv 端到端复核：**OpenRouter 的 chat_completions 路径会在 HTTP body 顶层注入 `session_id`(对全部模型生效)**，这是一个会话亲和(session affinity)字段，详见 §2.2。

---

## TL;DR — 这份报告在讲什么

**一句话**：Hermes 是一个 LLM agent。它在每次向推理后端发请求时，会顺带塞进一批"元数据"——本报告统一把这类元数据叫 **agent hint**（agent 随推理请求下发、用来辅助后端做 KV cache / 调度决策的附加信息）。本报告把 Hermes 支持的**所有传输层路径**逐一发请求、把请求录下来，系统地比对"每条路径分别带了哪些 agent hint、放在 HTTP 的哪个位置、是干什么用的"。

**为什么要做这件事**：如果你打算自研一个 KV-aware 的推理后端（按"哪些请求共享前缀、哪些请求属于同一会话"来复用 KV cache、做亲和调度），你必须先搞清楚 agent 端到底会发来什么信号。本报告就是这张"信号清单"的传输层视角总览——它告诉你：同一个 Hermes，走不同的 provider，发出来的 hint 形状差别很大，后端的解析逻辑必须按路径分别处理。

**几个先解释清楚的核心概念**（后文反复用到，第一次出现先在这里定义）：

- **transport / provider 路径**：Hermes 把"往哪个后端、用什么 wire 协议发请求"抽象成传输层。不同 provider（OpenAI、Anthropic、OpenRouter、Kimi、xAI/Grok、Codex、NVIDIA NIM……）走不同的传输路径，发出来的 HTTP 请求结构各不相同。
- **api_mode（API 模式）**：Hermes 内部把 wire 协议分成四类——`chat_completions`（OpenAI 兼容的 `/chat/completions`）、`anthropic_messages`（Anthropic 的 `/messages`）、`codex_responses`（OpenAI Codex 的 `/responses`）、`bedrock_converse`（AWS Bedrock 的 Converse）。同一个对话，落到不同 api_mode 上，agent hint 的承载方式完全不同。
- **prefix cache（前缀缓存）**：推理框架把"system prompt + 工具定义 + 历史消息"这段稳定前缀对应的 KV cache 缓存下来，下次同前缀的请求直接复用，省掉重复 prefill。这是对推理后端最值钱的优化点。
- **cache_control 断点**：在 Anthropic 协议族里，agent 用 `cache_control` 标记"前缀缓存到这里为止"——这个标记位置就叫**断点（breakpoint）**。Hermes 的断点布局是 **system_and_3**：即 system 段 + 最后 3 条非 system 消息，最多 4 个断点。
- **会话亲和键（session affinity key）**：用来把"同一个会话的多次请求"路由到同一推理节点、从而最大化 KV cache 复用的标识符。本报告涉及三种：`session_id`、`prompt_cache_key`、`x-grok-conv-id`。
- **host 门控（host gating）**：Hermes 在注入某些 provider 专属 hint **之前**，会先检查"这次请求要发往的 `base_url`，其主机名是不是某个特定域名"（由函数 `base_url_host_matches` 判定）。只有命中才注入。这导致很多 hint 在本地 mock（`localhost`）上根本不会出现——详见 §2.2 的 provider 区分说明，机制完整展开见报告 15 §4.3。

**和其它报告的关系**：本报告（连同报告 13、14）是用 **import-driver** 方式驱动 Hermes 采到的——即在 Python 里直接 `import` Hermes、手工 `new` 一个 `AIAgent` 再调它的 `run_conversation()`。这种方式快但会"从对话循环半路切进去"，跳过配置解析、工具组装、完整 system prompt 拼装等真实部署必经的前半段，因此看到的请求是"缩水版"（工具常常是 0 个、system prompt 只有一两千字符）。报告 15 改用 **真实 CLI**（子进程跑 `hermes chat`）补上了这个盲区，实测到的是 29 个工具 + 约 16096 字符 system 的真实量级——但请注意，这两个数字是**环境/配置相关的捕获值，不是常量，绝不可硬编码**（来龙去脉见报告 15 §1 与报告 17 §2.1）。本报告聚焦"传输层 hint 的形状"，这一层的结论不受上述工具数差异影响。

---

## 一、总览对比表

下面这张表是全报告的"地图"：把 11 条已实测的传输路径横向铺开，逐列对比它们的 User-Agent、产品归属头、缓存头、以及携带的上下文管理字段。读法是——**每一行是一条 provider 路径，每一列是一个观察维度**。表后会逐类拆开讲细节，这里先建立整体印象：大多数 chat_completions 路径默认什么 hint 都不带，只有 OpenRouter、Anthropic、Codex、xAI 这几条会主动注入会话/缓存相关的字段。

```
+---+-----------------------------------------+-------------------+-----------------+---------------+-----------------------------------------------------------------------------------------------------------------------------------------------------+
| # | Transport / Provider Path                | User-Agent        | Product Header  | Cache Header  | Context Management Features & Fields                                                                                                               |
+---+-----------------------------------------+-------------------+-----------------+---------------+-----------------------------------------------------------------------------------------------------------------------------------------------------+
| 1 | chat_completions (default/custom)       | OpenAI/Python     | (none)          | (none)        | (none)                                                                                                                                              |
| 2 | chat_completions (OpenRouter)           | OpenAI/Python     | X-Title:        | X-OR-Cache    | **body.session_id**（全模型，会话亲和）；grok 模型额外 header.x-grok-conv-id（同值）                                                                     |
|   |                                         |                   | Hermes Agent    | true          |                                                                                                                                                     |
| 3 | chat_completions (Nous Portal)          | OpenAI/Python     | (none)          | (none)        | body.tags = [product=hermes-agent, client=hermes-client-v0.15.1]  ← 产品归属                                                                         |
| 4 | anthropic_messages (cache_control)      | Anthropic/Python  | (none)          | cache_control | cache_control (4 breakpoints in message content blocks: system + 3 tail messages), anthropic-beta: fine-grained-tool-streaming                     |
| 5 | chat_completions (OR + cache_control)   | OpenAI/Python     | X-Title:        | X-OR-Cache    | cache_control (4 breakpoints — envelope layout: system + 3 tail messages)                                                                          |
|   |                                         |                   | Hermes Agent    | true          |                                                                                                                                                     |
| 6 | chat_completions (NVIDIA NIM)           | OpenAI/Python     | X-BILLING:      | (none)        | (none — 仅计费归属)                                                                                                                                  |
|   |                                         |                   | HermesAgent     |               |                                                                                                                                                     |
| 7 | chat_completions (Kimi)                 | claude-code/0.1.0 | (none)          | (none)        | User-Agent Spoofing — 绕过 Kimi 的 API 验证                                                                                                          |
| 8 | codex_responses                         | OpenAI/Python     | (none)          | (none)        | header.session_id, header.x-client-request-id, body.prompt_cache_key  ← 三者使用相同的 session_id 值                                                 |
| 9 | xai_grok                                | OpenAI/Python     | (none)          | (none)        | header.x-grok-conv-id, body.prompt_cache_key  ← 两者使用相同的 session_id 值                                                                         |
|10 | anthropic_messages (thinking config)    | Anthropic/Python  | (none)          | cache_control | body.thinking = {type:enabled, budget_tokens:16000}, cache_control, anthropic-beta                                                                  |
|11 | anthropic_messages (with tools)         | Anthropic/Python  | (none)          | (none)        | anthropic-beta: interleaved-thinking, fine-grained-tool-streaming                                                                                   |
+---+-----------------------------------------+-------------------+-----------------+---------------+-----------------------------------------------------------------------------------------------------------------------------------------------------+
```

**怎么读这张表 / 几条关键观察**：

- **第 1 行是"裸路径"基线**：默认的 / 自定义的 chat_completions 走 OpenAI Python SDK，不带任何产品头、缓存头或上下文管理字段。换句话说，如果你只接一个普通的 OpenAI 兼容后端，Hermes 给你的就是一个干净请求——没有任何 agent hint 可供后端利用。所有"花活"都集中在后面那些专属 provider 路径上。
- **第 2、5 行（OpenRouter）是 hint 最丰富的 chat_completions 路径**：既带产品归属头（`X-Title: Hermes Agent`）、又带缓存头（`X-OR-Cache: true`）、还在 body 顶层注入会话亲和键 `session_id`。第 5 行额外开启了 envelope 式的 `cache_control` 断点布局。注意第 2 行末尾的细节：**只有 grok 模型经 OpenRouter 时，才额外带 `x-grok-conv-id` 头，且其值与 body 的 `session_id` 相同**——同一个会话标识，用两个字段表达，分别服务 OpenRouter 路由和 xAI 后端粘性。
- **第 4、10、11 行（Anthropic messages）是缓存断点的主战场**：这三行都用 `cache_control` 做 4 断点（system + 3 tail），并带 `anthropic-beta` 功能开关头。第 10 行额外带了 thinking 配置，第 11 行带的是 interleaved-thinking 这个 beta。
- **第 8、9 行（Codex / xAI）共享一个设计**：它们不用 content 级的 `cache_control` 标记，而是用 body 顶层的 `prompt_cache_key` 字段，把一个会话标识当 cache key 用。Codex 还额外在 HTTP header 里放 `session_id` 和 `x-client-request-id`——**三者用的是同一个 session_id 值**；xAI 则在 header 里放 `x-grok-conv-id`，与 body 的 `prompt_cache_key` 同值。
- **第 6 行（NVIDIA NIM）只带计费归属**：`X-BILLING-INVOKE-ORIGIN: HermesAgent`，没有任何上下文管理 hint。它在表里出现，是为了说明"产品头不等于缓存 hint"——这条路径对 KV 调度毫无信号价值。
- **第 7 行（Kimi）是身份伪装的特例**：它把 User-Agent 伪装成 `claude-code/0.1.0`，目的不是带 hint，而是骗过 Kimi 的 API 验证（详见 §2.5）。

---

## 二、按上下文管理功能分类

上面那张总览表是"按路径横切"。这一节换个角度——**按功能纵切**：把所有路径里同类的 agent hint 归到一起讲。这样你能看清"同一个目的（比如前缀缓存、会话追踪）在不同 provider 上分别是怎么实现的、差异在哪"。

### 2.1 Prefix Cache（提示词前缀缓存）

**这是对推理框架最关键的亲和性优化点**。原理回顾：一次请求的开头那一大段（system prompt + 工具定义 + 已累积的历史消息）在同一会话/同一部署里是高度重复的；推理框架若能把这段前缀对应的 KV cache 缓存住、下次直接复用，就省掉了重复 prefill 的算力。问题在于"前缀到哪里为止可以安全复用"——这就需要 agent 端用某种标记告诉后端**断点**在哪。下表对比五种 provider 各自的标记机制：

| Transport | 机制 | 注入位置 | Cache TTL | 断点数量 | 布局方式 |
|-----------|------|---------|-----------|---------|---------|
| **Anthropic native** | `cache_control: {type: ephemeral}` | 消息 **content block** 内部 | 5min（默认）/ 1h | 4 个（system + 3 tail） | Native（content-level） |
| **OpenRouter wire** | `cache_control: {type: ephemeral}` | 消息 **envelope** 级别 | 5min（默认）/ 1h | 4 个（system + 3 tail） | Envelope（message-level） |
| **Nous Portal (Qwen)** | `cache_control` envelope layout | 消息 envelope 级别 | 5min | 4 个 | Envelope |
| **MiniMax Anthropic-compat** | `cache_control` native layout | 消息 content block 内部 | 5min | 4 个 | Native |
| **Codex/xAI** | `prompt_cache_key: <session_id>` | **HTTP body** 顶层字段 | 服务端决定 | N/A | Body-level key |

**逐行解释这张表在说什么**：

- **断点数量恒为 4，布局是 system_and_3**：前四行都是 4 个断点，对应 Hermes 的 system_and_3 布局——system 段一个断点，加上最后 3 条非 system 消息各一个断点。注意一个协议差异：在 Anthropic wire 下，system 是请求 body 里的**独立顶层字段**（不在 messages 数组里），所以 system 的 `cache_control` 标在那个独立字段上；而 chat_completions/envelope 路径下 system 是 messages[0]。"最后 3 条"取的是非 system 消息的尾部三条（`non_sys[-3:]`）——即历史里最新的三条，因为它们最可能在下一轮被复用为前缀。
- **`{type: ephemeral}` 是什么**：Anthropic 的缓存标记类型，表示"这是个临时缓存"，配合 TTL 决定缓存能活多久。默认 5 分钟（`5m`），可配成 1 小时（`1h`）。
- **Native vs Envelope 是本表的核心区别**，决定后端的解析代码怎么写：
  - **Native（content-level）** —— `cache_control` 标在**消息的 content block 内部**（每条消息的 content 是一个 block 数组，标记打在某个 block 上）。后端要解析到 content block 这一层，逐个 block 查 `cache_control` 字段。Anthropic 原生路径和 MiniMax 的 Anthropic-兼容路径走这种。
  - **Envelope（message-level）** —— `cache_control` 标在**消息顶层**（消息这个对象本身的属性，而不是它内部的 content block）。后端只需在消息级别检查 `cache_control`，不用钻进 content。OpenRouter 和 Nous Portal(Qwen) 走这种。
  - 为什么会有两种布局？因为 OpenRouter / Qwen 是"OpenAI 兼容 wire 上转译 Anthropic 缓存语义"，它们把断点上提到了消息顶层（envelope）；而 Anthropic / MiniMax 走原生 Anthropic content-block 结构。Hermes 内部由一个 `_apply_cache_marker(native_anthropic=…)` 参数来切换这两种落点。
- **Codex/xAI 是完全不同的范式**：它们**不在 content 或 envelope 上打断点**，而是在 HTTP body 顶层放一个 `prompt_cache_key` 字段，值就是会话的 session_id。后端拿这个 key 去"按 key 路由 / 按 key 找缓存桶"，断点位置由服务端自己决定（所以断点数量列是 N/A，TTL 也由服务端决定）。这对自研后端的含义是：Codex/xAI 给的是"会话级粒度"的缓存提示，比 cache_control 的"消息级断点"粒度粗，但实现简单——你只要按 key 做亲和即可。

### 2.2 Session Tracking（会话追踪）

会话追踪解决的是 §2.1 里没覆盖的另一半问题：前缀缓存告诉后端"缓存到哪"，会话追踪告诉后端"这次请求属于哪个会话"——后者用于把同会话的请求路由到同一节点（路由亲和），从而让前者的缓存真的能被命中。下表列出所有会话追踪字段、它们在 HTTP 的哪个位置、做什么用：

| Transport | 字段 | 位置 | 用途 |
|-----------|------|------|------|
| **Codex Responses** | `session_id` | HTTP Header | 会话路由，prompt cache scoping |
| **Codex Responses** | `x-client-request-id` | HTTP Header | 请求级 tracing，与 session_id 同值 |
| **xAI/Grok** | `x-grok-conv-id` | HTTP Header | 会话持续性路由 |
| **Codex/xAI** | `prompt_cache_key` | HTTP Body | Cache key scoping |
| **OpenRouter (chat_completions)** | `session_id` | **HTTP Body 顶层** | 会话亲和（v0.15.1 起，全模型；端到端实测确认） |
| **OpenRouter + Grok** | `x-grok-conv-id` | HTTP Header | 与 body.session_id 同值，xAI 后端粘性 |

**逐行解释**：

- **Codex Responses 带两个 header**：`session_id`（会话路由 + 给 prompt cache 划定作用域）和 `x-client-request-id`（请求级 tracing，比如日志关联）。关键细节：**这两个 header 的值是同一个 session_id**——也就是说，对 Codex 而言，"会话标识"和"请求追踪标识"复用了同一个值。
- **xAI/Grok 用 `x-grok-conv-id` 这个 header** 做会话持续性路由（conv = conversation）。
- **`prompt_cache_key` 在 body 里**，Codex 和 xAI 都用，作为 cache key 的作用域标识。结合 §2.1：Codex/xAI 的"前缀缓存"和"会话追踪"其实是同一个机制的两面——都靠这个 body 级的 key。
- **OpenRouter 在 chat_completions 路径上、body 顶层注入 `session_id`**（注意是 body 顶层，不是 header，也不是 extra_body 里更深的位置）。这一条是**端到端实测确认**的：从 v0.15.1 起，对**全部模型**生效，而不只是某些模型。它的作用是会话亲和——让 OpenRouter 把同会话请求粘到同一上游。
- **OpenRouter + Grok 这一行**说明一个组合情况：当经 OpenRouter 访问 grok 模型时，除了 body 里的 `session_id`，还会额外在 header 里带 `x-grok-conv-id`，且**两者同值**。原因是 OpenRouter 自己要 `session_id` 做路由，而它背后的 xAI 后端要 `x-grok-conv-id` 做粘性——一个会话标识，喂给路由链上两个不同环节。

> ⚠️ **provider 区分（这是个容易踩的坑，务必看清）**：`session_id` / `x-client-request-id` 这两个 **header**，只有走**真正的 OpenAI Codex provider** 路径时才会被注入。
> 而 `openai/gpt-5` 这类模型如果是**经 OpenRouter** 访问的，虽然也会进入 `codex_responses` 模式，但**只带 body 里的 `prompt_cache_key`（其值 = session_id），不带那两个 header**。
> 换句话说："走 codex 协议"和"是真正的 Codex provider"是两回事——后端不能仅凭 api_mode 是 `codex_responses` 就假设那两个 header 一定在。这背后是 §0 提到的 host 门控机制在起作用：header 类的身份/会话字段往往要求 base_url 主机名命中特定域名才注入，机制全貌见报告 15 §4.3。

### 2.3 Thinking / Reasoning 配置

"Thinking / Reasoning" 指让模型在正式作答前先做一段显式推理（思维链/推理预算）。这类 hint 对推理后端的价值在于：它**预告了这次 decode 大概会有多长**——比如 `budget_tokens: 16000` 意味着模型可能要多生成上万 token 的思考内容，后端可据此预留资源、估算 SLO。两种传输路径的承载方式不同：

| Transport | 字段 | 位置 | 格式 |
|-----------|------|------|------|
| **Anthropic messages** | `thinking` | Body 顶层 | `{type: enabled, budget_tokens: N}` |
| **Chat Completions** | `reasoning` | `extra_body` | `{effort: high, enabled: true}` 或 `{max_tokens: N}` |

**解释**：

- **Anthropic 路径**把推理配置放在 body 顶层的 `thinking` 字段，格式是 `{type: enabled, budget_tokens: N}`——明确给出一个 token 预算 N（总览表第 10 行实测值为 16000）。这是个"硬预算"风格的配置。
- **Chat Completions 路径**把它放在 `extra_body.reasoning` 里（`extra_body` 是 OpenAI SDK 用来塞非标准字段的口袋），格式有两种风格：`{effort: high, enabled: true}`（按强度档位 effort 表达）或 `{max_tokens: N}`（按 token 上限表达）。注意：chat_completions 路径的 reasoning 注入受 host 门控约束（要求 base_url 命中 openrouter / nousresearch.com 等），localhost 上不会出现。

### 2.4 Product Attribution（产品归属）

产品归属头不影响推理或缓存，它们的作用是让后端/网关知道"这个请求来自 Hermes Agent 这个产品（及版本）"——用于计费归属、流量统计、产品来源标记。这类头**不是 agent hint（对 KV 调度无价值）**，但列在这里是为了让你在解析请求时能正确识别和忽略它们。

| Transport | Header | 含义 |
|-----------|--------|------|
| **OpenRouter** | `HTTP-Referer: https://hermes-agent.nousresearch.com` | 产品来源 |
| **OpenRouter** | `X-Title: Hermes Agent` | 产品名称 |
| **OpenRouter** | `X-OpenRouter-Categories: productivity,cli-agent` | 产品类别 |
| **NVIDIA NIM** | `X-BILLING-INVOKE-ORIGIN: HermesAgent` | 计费归属 |
| **Nous Portal** | `extra_body.tags: [product=hermes-agent, client=hermes-client-v0.15.1]` | 产品+版本 |

**解释**：OpenRouter 用三个标准头（来源 URL、产品名、类别）做归属；NVIDIA NIM 用一个 `X-BILLING-INVOKE-ORIGIN` 头做计费归属；Nous Portal 则不是用 header，而是在 `extra_body.tags` 里塞一个数组，同时标出产品名（`hermes-agent`）和带版本号的客户端标识（`hermes-client-v0.15.1`）——这是唯一在归属信息里带版本号的路径。

### 2.5 User-Agent Spoofing（身份伪装）

有些后端会**根据 User-Agent 做认证或限流控制**——即只有 UA 长得"对"才放行。Hermes 为了能正常接入这些后端，会按目标后端伪装成不同的 User-Agent。下表列出五种伪装及其原因：

| 后端 | User-Agent | 原因 |
|------|-----------|------|
| **Kimi** | `claude-code/0.1.0` | Kimi `/coding` 端点要求此 UA |
| **Anthropic OAuth** | `claude-cli/<ver> (external, cli)` | OAuth 基础设施验证 UA 版本 |
| **Codex Cloudflare** | `codex_cli_rs/0.0.0 (Hermes Agent)` | 绕过 SDK 指纹检测 |
| **Gemini native** | `hermes-agent (gemini-native)` | 产品标识 |
| **Gemini CloudCode** | `hermes-agent (gemini-cli-compat)` | 产品标识 |

**解释**：

- **Kimi** 的 `/coding` 端点硬性要求 UA 是 `claude-code/0.1.0`（它把自己当成给 Claude Code 用的端点），所以 Hermes 必须伪装成这个值才能用——这也是总览表第 7 行 UA 异常的原因。
- **Anthropic OAuth** 路径要求 UA 是 `claude-cli/<ver> (external, cli)`，因为 Anthropic 的 OAuth 基础设施会校验 UA 版本（external 表示第三方 CLI）。
- **Codex Cloudflare** 用 `codex_cli_rs/0.0.0 (Hermes Agent)`——这是为了**绕过 Cloudflare 对 SDK 指纹的检测**（OpenAI 官方 codex CLI 是 Rust 实现，故 UA 带 `_rs`），同时在括号里诚实标出 `Hermes Agent`。
- **Gemini native / CloudCode** 两条路径的 UA 只是单纯的产品标识（分别标明走原生还是 CLI-兼容 facade），不涉及绕过验证。

---

## 三、推理框架亲和性优化建议

前两节是"观察到了什么"，这一节是"如果你在写推理后端，该怎么利用这些观察"。下面给出后端侧需要解析的字段优先级、以及基于这些字段的缓存/路由亲和策略。

### 3.1 需要解析的关键字段

这张表是给后端实现者的"解析清单"：每个字段在 HTTP 的哪里、优先级多高（即对 KV 调度的价值多大）。优先级越高，越应该优先支持解析。

| 字段 | 解析位置 | 优先级 |
|------|---------|--------|
| `cache_control: {type: ephemeral}` | Body → messages[N] → content[N] 或 消息顶层 | **最高** — 决定 KV cache 保留 |
| `prompt_cache_key` | Body 顶层 或 extra_body | **最高** — Codex/xAI 缓存路由 |
| `session_id` | HTTP Header | **高** — 会话级缓存范围 |
| `x-client-request-id` | HTTP Header | **中** — 请求追踪 |
| `x-grok-conv-id` | HTTP Header | **高** — xAI 会话路由 |
| `anthropic-beta` | HTTP Header | **中** — 功能开关（fine-grained-tool-streaming） |
| `anthropic-version` | HTTP Header | **低** — API 版本（始终 2023-06-01） |
| `thinking: {type, budget_tokens}` | Body | **中** — 推理配置 |
| `tags[N]` | Body 或 extra_body | **低** — 产品归属 |

**怎么读优先级**：

- **两个"最高"——`cache_control` 和 `prompt_cache_key`**，因为它们直接决定 KV cache 怎么保留和路由（前者是消息级断点，后者是会话级 key），是后端做亲和的根本依据。注意 `cache_control` 的解析位置有两种（content 级 / 消息顶层），对应 §2.1 的 native/envelope——解析器要两种都能处理。
- **三个"高"——`session_id`、`x-grok-conv-id`**（会话/路由亲和键），它们决定"同会话请求是否能落到同一节点"，进而决定缓存能否真正命中。
- **"中"的几个**：`x-client-request-id`（只用于追踪，不影响调度）、`anthropic-beta`（功能开关，告诉你这次请求开了哪些特性，如 fine-grained-tool-streaming）、`thinking`（推理配置，对 decode 长度有提示价值）。
- **"低"的几个**：`anthropic-version`（始终是 `2023-06-01`，是个常量，解析它意义不大）、`tags`（产品归属，对调度无用）。

### 3.2 Cache 亲和策略

下面是基于上表字段、给后端写的缓存判定伪代码。读法：对每个进来的请求，按 1→2→3 的顺序检查并处置。

```
对每个请求:
  1. 检查是否存在 prompt_cache_key (body/extra_body)
     → 有: 按 key 匹配缓存桶
  2. 检查是否存在 cache_control 字段 (body)
     → 检测布局:
       - Native: content_blocks[N].cache_control
       - Envelope: messages[N].cache_control
     → 在断点位置保留 KV cache，断点之间的系统提示词可跨请求复用
  3. 对不携带任何缓存标记的请求:
     → 按 (model, auth_token_hash, system_prompt_prefix) 做被动 cache
```

**逻辑解释**：

- **第 1 步优先看 `prompt_cache_key`**（Codex/xAI 路径），因为它最直接——有 key 就按 key 匹配缓存桶，会话粒度，简单可靠。
- **第 2 步看 `cache_control`**（Anthropic / OpenRouter / Qwen / MiniMax 路径）：先判断是 native 还是 envelope 布局（前者在 content block 里、后者在消息顶层），然后在断点位置保留 KV cache——这样断点之间的稳定前缀（尤其 system prompt）就能跨请求复用。
- **第 3 步是兜底**：对总览表第 1 行那种"裸路径"（什么缓存标记都不带），后端只能做**被动缓存**——用 `(model, auth_token_hash, system_prompt_prefix)` 三元组做指纹去匹配。这没有 agent 的显式提示精确，但聊胜于无。

### 3.3 路由亲和策略

缓存策略解决"怎么存"，路由策略解决"请求该送到哪个节点"——两者配合才能让缓存真正被命中。

```
按 session/conv-id 做会话亲和:
  - Codex: session_id header
  - xAI: x-grok-conv-id header
  → 同 session 的请求路由到同一推理节点，最大化 KV cache 复用

按 User-Agent 做路由区分:
  - Anthropic/Python 0.87.0 → Anthropic Messages API 格式
  - OpenAI/Python 2.24.0  → Chat Completions 格式
```

**逻辑解释**：

- **会话亲和**：用会话/会话标识（Codex 看 `session_id` header、xAI 看 `x-grok-conv-id` header）把同会话请求都路由到同一推理节点。因为 KV cache 是节点本地的，只有路由到同一节点，§3.2 存下的缓存才能被复用——这是会话亲和与缓存亲和必须配套的根本原因。
- **按 UA 做路由区分**：从 User-Agent 就能判断请求走的是哪种 wire 格式——`Anthropic/Python 0.87.0` 对应 Anthropic Messages API 格式、`OpenAI/Python 2.24.0` 对应 Chat Completions 格式。后端可据此把请求分派到对应的解析/处理管线。（注意 §2.5 提到的伪装情形会让 UA 不可靠，UA 路由仅适用于未伪装的标准路径。）

---

## 四、测试复现方法

下面三步可以从零复现本报告的全部捕获。前提：用的是真实安装的 venv（路径见下），mock 后端会把每个收到的请求落盘成 JSONL 供事后检查。

```bash
# 1. 启动 mock server（监听 19998 端口，模拟各家后端）
python3 /tmp/mock_server_v3.py 19998 &

# 2. 运行全传输测试（用真实安装的 venv 跑测试脚本，依次打所有传输路径）
/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3 /tmp/test_all_transports.py

# 3. 查看捕获的请求（每行一个请求，格式化输出）
cat /tmp/hermes_all_requests.jsonl | python3 -m json.tool
```

**说明**：第 1 步起的 mock 监听 `127.0.0.1:19998`，伪装成各家后端接收请求；第 2 步用 `~/.hermes` 下真实安装的 Python 解释器跑测试脚本，依次触发 11 条传输路径；第 3 步把捕获文件 `/tmp/hermes_all_requests.jsonl`（每行一条请求记录）格式化打出来，逐条核对 agent hint。

---

## 五、补充：未被覆盖的传输路径

下表这些路径**在本次 import-driver 测试里没有实测覆盖**——它们要么需要真实 API Key，要么需要额外环境（OAuth 流程、子进程通信、AWS 签名等），在 mock 上跑不通或跑不全。但它们的 agent hint 行为**可以从源码确定**，故一并列出，供你做完整性参考。

| Transport | API Mode | 上下文管理机制 | 备注 |
|-----------|----------|---------------|------|
| **Bedrock Converse** | `bedrock_converse` | 无自定义 header（AWS SigV4 签名原生处理） | 使用独立的 boto3 客户端 |
| **Gemini Native** | `chat_completions` (facade) | `User-Agent: hermes-agent (gemini-native)` | 自建 HTTP client，非 OpenAI SDK |
| **Gemini CloudCode** | `chat_completions` (facade) | `User-Agent: hermes-agent (gemini-cli-compat)` + `x-activity-request-id` | Bearer OAuth token |
| **Anthropic OAuth** | `anthropic_messages` | `user-agent: claude-cli/<ver> (external, cli)` | OAuth PKCE 流程 |
| **OpenAI Codex App Server** | `codex_responses` (via subprocess) | 同 Codex Responses，但通过本地子进程通信 | 不走 HTTP |

**逐行解释**：

- **Bedrock Converse**（api_mode `bedrock_converse`）：不带任何自定义 header——因为 AWS 的认证靠 SigV4 签名原生处理，Hermes 用的是独立的 boto3 客户端（不是 OpenAI/Anthropic SDK），所以本报告那套基于 SDK 的捕获方法对它不适用。
- **Gemini Native**（走 `chat_completions` facade，即"披着 chat_completions 外壳"）：用自建 HTTP client（非 OpenAI SDK），UA 为 `hermes-agent (gemini-native)`。
- **Gemini CloudCode**（同样是 chat_completions facade）：UA 为 `hermes-agent (gemini-cli-compat)`，额外带 `x-activity-request-id` 头，认证用 Bearer OAuth token。
- **Anthropic OAuth**（api_mode `anthropic_messages`）：走 OAuth PKCE 流程，UA 为 `claude-cli/<ver> (external, cli)`（呼应 §2.5 的身份伪装——OAuth 基础设施会校验这个 UA）。
- **OpenAI Codex App Server**（api_mode `codex_responses`，但经子进程）：hint 行为和 Codex Responses 一致，区别是它**通过本地子进程通信、不走 HTTP**——所以基于 HTTP 抓包的方法也覆盖不到它。

> 配套阅读：本报告（连同报告 13、14）走的是 import-driver 路径，看到的是传输层 hint 的"形状"。要看真实部署量级（29 工具 + 16096 字符 system，以及多轮缓存断点滑动 `[0]→[0,1,2]→[2,3,4]`）请见报告 15（真实 CLI 测试台）；要看每个信号"在源码哪一行、怎么提取、什么保真度"的实现规格，以及对本报告坐标的勘误，请见报告 17 §1–§2。
