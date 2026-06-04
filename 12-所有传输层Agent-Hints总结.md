# Hermes Agent — 所有传输层 Agent Hints 完整对比

> 测试环境：Hermes Agent **v0.15.1** · OpenAI SDK 2.24.0 · Anthropic SDK 0.87.0
> Mock 后端：`/tmp/mock_server_v3.py` (127.0.0.1:19998)
> 测试脚本：`/tmp/test_all_transports.py`
>
> 📌 经真实安装 venv 端到端复核：**OpenRouter chat_completions 路径会在 body 顶层注入 `session_id`(全模型)**，是会话亲和字段，详见 §2.2。

---

## 一、总览对比表

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

---

## 二、按上下文管理功能分类

### 2.1 Prefix Cache （提示词前缀缓存）

这是对推理框架**最关键的亲和性优化点**。

| Transport | 机制 | 注入位置 | Cache TTL | 断点数量 | 布局方式 |
|-----------|------|---------|-----------|---------|---------|
| **Anthropic native** | `cache_control: {type: ephemeral}` | 消息 **content block** 内部 | 5min (默认) / 1h | 4 个 (system + 3 tail) | Native (content-level) |
| **OpenRouter wire** | `cache_control: {type: ephemeral}` | 消息 **envelope** 级别 | 5min (默认) / 1h | 4 个 (system + 3 tail) | Envelope (message-level) |
| **Nous Portal (Qwen)** | `cache_control` envelope layout | 消息 envelope 级别 | 5min | 4 个 | Envelope |
| **MiniMax Anthropic-compat** | `cache_control` native layout | 消息 content block 内部 | 5min | 4 个 | Native |
| **Codex/xAI** | `prompt_cache_key: <session_id>` | **HTTP body** 顶层字段 | 服务端决定 | N/A | Body-level key |

**关键差异**：
- **Anthropic native** — `cache_control` 在 content block 内部 → 推理框架需要解析每个 content block 的 `cache_control` 字段
- **OpenRouter wire** — `cache_control` 在消息顶层 → 推理框架只需检查消息级的 `cache_control` 字段
- **Codex/xAI** — 不使用 content-level 标记，使用 body 级 `prompt_cache_key` 按 key 路由

### 2.2 Session Tracking（会话追踪）

| Transport | 字段 | 位置 | 用途 |
|-----------|------|------|------|
| **Codex Responses** | `session_id` | HTTP Header | 会话路由，prompt cache scoping |
| **Codex Responses** | `x-client-request-id` | HTTP Header | 请求级 tracing，与 session_id 同值 |
| **xAI/Grok** | `x-grok-conv-id` | HTTP Header | 会话持续性路由 |
| **Codex/xAI** | `prompt_cache_key` | HTTP Body | Cache key scoping |
| **OpenRouter (chat_completions)** | `session_id` | **HTTP Body 顶层** | 会话亲和（v0.15.1 起，全模型；端到端实测确认） |
| **OpenRouter + Grok** | `x-grok-conv-id` | HTTP Header | 与 body.session_id 同值，xAI 后端粘性 |

> ⚠️ **provider 区分**：`session_id`/`x-client-request-id` 两个 **header** 只有真正的 **OpenAI Codex provider** 路径注入。
> `openai/gpt-5` 等经 **OpenRouter** 时会走 `codex_responses` 模式，但只带 **body.prompt_cache_key**（=session_id），**不带**那两个 header。

### 2.3 Thinking / Reasoning 配置

| Transport | 字段 | 位置 | 格式 |
|-----------|------|------|------|
| **Anthropic messages** | `thinking` | Body 顶层 | `{type: enabled, budget_tokens: N}` |
| **Chat Completions** | `reasoning` | `extra_body` | `{effort: high, enabled: true}` 或 `{max_tokens: N}` |

### 2.4 Product Attribution（产品归属）

| Transport | Header | 含义 |
|-----------|--------|------|
| **OpenRouter** | `HTTP-Referer: https://hermes-agent.nousresearch.com` | 产品来源 |
| **OpenRouter** | `X-Title: Hermes Agent` | 产品名称 |
| **OpenRouter** | `X-OpenRouter-Categories: productivity,cli-agent` | 产品类别 |
| **NVIDIA NIM** | `X-BILLING-INVOKE-ORIGIN: HermesAgent` | 计费归属 |
| **Nous Portal** | `extra_body.tags: [product=hermes-agent, client=hermes-client-v0.15.1]` | 产品+版本 |

### 2.5 User-Agent Spoofing（身份伪装）

某些后端通过 User-Agent 做认证/限流控制：

| 后端 | User-Agent | 原因 |
|------|-----------|------|
| **Kimi** | `claude-code/0.1.0` | Kimi `/coding` 端点要求此 UA |
| **Anthropic OAuth** | `claude-cli/<ver> (external, cli)` | OAuth 基础设施验证 UA 版本 |
| **Codex Cloudflare** | `codex_cli_rs/0.0.0 (Hermes Agent)` | 绕过 SDK 指纹检测 |
| **Gemini native** | `hermes-agent (gemini-native)` | 产品标识 |
| **Gemini CloudCode** | `hermes-agent (gemini-cli-compat)` | 产品标识 |

---

## 三、推理框架亲和性优化建议

### 3.1 需要解析的关键字段

| 字段 | 解析位置 | 优先级 |
|------|---------|--------|
| `cache_control: {type: ephemeral}` | Body → messages[N] → content[N] 或 消息顶层 | **最高** — 决定 KV cache 保留 |
| `prompt_cache_key` | Body 顶层 或 extra_body | **最高** — Codex/xAI 缓存路由 |
| `session_id` | HTTP Header | **高** — 会话级缓存范围 |
| `x-client-request-id` | HTTP Header | **中** — 请求追踪 |
| `x-grok-conv-id` | HTTP Header | **高** — xAI 会话路由 |
| `anthropic-beta` | HTTP Header | **中** — 功能开关 (fine-grained-tool-streaming) |
| `anthropic-version` | HTTP Header | **低** — API 版本 (始终 2023-06-01) |
| `thinking: {type, budget_tokens}` | Body | **中** — 推理配置 |
| `tags[N]` | Body 或 extra_body | **低** — 产品归属 |

### 3.2 Cache 亲和策略

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

### 3.3 路由亲和策略

```
按 session/conv-id 做会话亲和:
  - Codex: session_id header
  - xAI: x-grok-conv-id header
  → 同 session 的请求路由到同一推理节点，最大化 KV cache 复用

按 User-Agent 做路由区分:
  - Anthropic/Python 0.87.0 → Anthropic Messages API 格式
  - OpenAI/Python 2.24.0  → Chat Completions 格式
```

---

## 四、测试复现方法

```bash
# 1. 启动 mock server
python3 /tmp/mock_server_v3.py 19998 &

# 2. 运行全传输测试
/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3 /tmp/test_all_transports.py

# 3. 查看捕获的请求
cat /tmp/hermes_all_requests.jsonl | python3 -m json.tool
```

---

## 五、补充：未被覆盖的传输路径

以下路径在当前测试中未覆盖（需要真实 API Key 或额外环境），但其 Agent Hint 行为可通过源码确定：

| Transport | API Mode | 上下文管理机制 | 备注 |
|-----------|----------|---------------|------|
| **Bedrock Converse** | `bedrock_converse` | 无自定义 header（AWS SigV4 签名原生处理） | 使用独立的 boto3 客户端 |
| **Gemini Native** | `chat_completions` (facade) | `User-Agent: hermes-agent (gemini-native)` | 自建 HTTP client，非 OpenAI SDK |
| **Gemini CloudCode** | `chat_completions` (facade) | `User-Agent: hermes-agent (gemini-cli-compat)` + `x-activity-request-id` | Bearer OAuth token |
| **Anthropic OAuth** | `anthropic_messages` | `user-agent: claude-cli/<ver> (external, cli)` | OAuth PKCE 流程 |
| **OpenAI Codex App Server** | `codex_responses` (via subprocess) | 同 Codex Responses，但通过本地子进程通信 | 不走 HTTP |
