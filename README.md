# Hermes Agent 深度调研与上下文管理验证

对 [Hermes Agent](https://github.com/NousResearch/hermes-agent)(Nous Research)代码库的深度架构调研，以及上下文管理 / 会话亲和特性的**源码分析 + 真实请求实测验证**。

> 测试版本：Hermes Agent **v0.15.1**（真实安装 `~/.hermes/hermes-agent/` 与源码树一致）
> 验证方式：用真实 `AIAgent.run_conversation()` 驱动 + mock 后端捕获真实 HTTP 请求

---

## TL;DR（先读这一段）

Hermes Agent 是一个 LLM agent 框架，能把同一段对话路由到不同的后端 provider（OpenAI、Anthropic、OpenRouter、Codex/xAI 等）。本仓库做了两件事：

1. **架构调研**（报告 01–11）：自顶向下 + 自底向上把 Hermes 的分层、传输层、工具/技能系统、网关、内存与上下文管理、CLI/配置、插件与多前端都摸了一遍。
2. **上下文管理与 agent hint 实测**（报告 12–17 + 三套验证平台）：聚焦一个推理服务工程师真正关心的问题——**Hermes 在每个推理请求里到底下发了哪些与 KV cache / 调度相关的元数据**（即 **agent hint**：agent 随推理请求下发给后端、辅助 KV/调度决策的元数据），这些字段长什么样、由什么条件门控、能不能被下游后端用来做缓存复用与调度优化。

本仓库定义了几个反复出现的术语，先在这里一次性解释，后文不再展开：

- **import-driver**：在 Python 进程里直接 `import hermes` 并调 `AIAgent.run_conversation()` 来驱动一次对话。优点是可编程、好 mock；缺点是绕过了 CLI 的完整 config 解析，会漏掉一部分只有真实 CLI 才有的行为。
- **真实 CLI**：用子进程跑 `hermes chat` 命令，走完整的配置加载、system prompt 组装、工具注入。它能看到 import-driver 看不到的东西（见报告 15）。
- **host 门控**：Hermes 会根据请求 `base_url` 的主机名（源码里的 `base_url_host_matches` 判断）决定某些 agent hint 发不发。比如 OpenRouter 专有的那套 hint 只在主机名命中 `openrouter.ai` 时才注入——这导致一部分特性在 `localhost` mock 上**根本激活不了**，只能靠源码确认或留到真实后端阶段验证。
- **cache_control / 断点 / `system_and_3`**：Anthropic 路径上用来标记"这段前缀可缓存"的字段。Hermes 的断点策略叫 `system_and_3`，即 system 消息 + 最后 3 条非 system 消息，共 ≤4 个断点，随对话增长向数组末尾滑动。
- **native（content 级）vs envelope（消息顶层）**：cache_control 标记可以打在消息 `content` 块内部（native），也可以打在消息对象顶层（envelope）。两种布局由 provider/host 决定。
- **压缩 / compaction**：上下文超阈值时，Hermes 用一次辅助请求把历史摘要成一段 `SUMMARY_PREFIX`（摘要前缀）开头的文本，替换掉被压缩的历史。压缩阈值是 `max(ctx*0.5, 64000)`。
- **`api_mode` 四类**：`chat_completions` / `anthropic_messages` / `codex_responses` / `bedrock_converse`，对应四种线路协议。
- **会话亲和键**：用来让后端把同一会话的多次请求落到同一份 KV cache 上的标识，包括 `session_id` / `prompt_cache_key` / `x-grok-conv-id`。

报告 16/17 还引入了一组面向"自研 KV/调度"的分类标签，在那两份报告里会大量出现：

- **G1 / G2 / G3**：三类调度目标——G1 = KVCache 主动管理，G2 = Agent 状态感知调度，G3 = SLO 感知调度。
- **W / S / H**：信号的三种提取机制——W = wire 可见、零改（直接从出站请求里读到）；S = 读 Hermes 内部对象（需进程内访问）；H = 埋 emit hook（需要在源码里插桩）。
- **MOCK 段 vs REAL 段**：测试台的两类用途——MOCK 段验"决策对不对"（行为/字段），REAL 段验"时延"（性能）。

---

## 一、架构调研报告（自顶向下 + 自底向上）

这一组(报告 01–11)是对 Hermes 整体结构的逆向梳理，不假设读者了解 Hermes 内部，从分层和数据流讲起，逐步下沉到具体模块。

| # | 报告 | 内容 |
|---|------|------|
| 01 | [总体架构概览](01-总体架构概览.md) | 整体分层、模块划分、数据流 |
| 02 | [传输层与 LLM 适配器深度分析](02-传输层与LLM适配器深度分析.md) | `ProviderTransport` 抽象基类(ABC)、四类传输 |
| 03 | [工具系统与注册表架构](03-工具系统与注册表架构.md) | 工具注册、调度 |
| 04 | [技能系统架构](04-技能系统架构.md) | skill 加载与执行 |
| 05 | [网关与多平台架构](05-网关与多平台架构.md) | gateway、多平台接入 |
| 06 | [内存与上下文管理架构](06-内存与上下文管理架构.md) | memory、context engine |
| 07 | [CLI 与配置系统架构](07-CLI与配置系统架构.md) | CLI、config 加载 |
| 08 | [插件/API/凭证与代码执行架构](08-插件-API-凭证与代码执行架构.md) | plugin、credential、code exec |
| 09 | [Web/TUI/ACP 与批处理架构](09-Web-TUI-ACP与批处理架构.md) | 多前端 |
| 10 | [测试与开发基础设施](10-测试与开发基础设施.md) | 测试体系 |
| 11 | [关键代码片段与实现总结](11-关键代码片段与实现总结.md) | 关键实现汇总 |

## 二、Agent Hints 与上下文管理（重点）

这一组(报告 12–17)是本仓库的核心，回答"Hermes 在每个推理请求里到底下发了什么、由什么门控、能怎么被下游 KV/调度利用"。建议按编号顺序读：12 给出全景对照表，13/14/15 是三条线路的实测，16/17 是面向自研后端的前瞻设计与提取规格。

| # | 报告 | 内容 |
|---|------|------|
| 12 | [所有传输层 Agent-Hints 总结](12-所有传输层Agent-Hints总结.md) | 各传输/provider 注入的 header、body 字段对比表 |
| 13 | [上下文管理特性 — 源码与实测验证](13-上下文管理特性-源码与实测验证.md) | OpenAI SDK 路径(`chat_completions` + `codex_responses`)上下文管理源码 + 真实请求实测 |
| 14 | [Anthropic 路径上下文管理深度测试](14-Anthropic路径上下文管理深度测试.md) | **最深入**：Anthropic SDK 路径(`anthropic_messages`)—— 原生 cache_control 断点 / TTL / beta / thinking 五分支 / usage 聚合 / 压缩 / 续写，对抗验证 + 25 断言 |
| 15 | [真实 CLI 探测测试台](15-真实CLI探测测试台.md) | **真实 `hermes` CLI** + mock 探测：走完整 config + 全量 system(16k 字符)+ 29 tools 入口，补 import-driver 盲区；12 场景 25 断言；产出真实后端测试台 fixtures |
| 16 | [Dynamo 风格 KV/调度 — 信息采集与测试台设计](16-Dynamo风格KV调度-信息采集与测试台设计.md) | **前瞻设计**：受 NVIDIA Dynamo 的 agent hint 机制启发，将 Hermes 已实测的 agent hint 映射到 KVCache 主动管理(G1) / Agent 状态感知调度(G2) / SLO 感知调度(G3)——8 大信号组采集规格 + 5 层测试台架构(MOCK 验决策 / REAL 验时延)+ 实验矩阵；经对抗评审回源码修正(§7 留痕) |
| 17 | [自研 KV/调度的 Hermes 信号提取规格](17-自研KV调度的Hermes信号提取规格.md) | **实现输入规格**：自研全栈(后端与 NVIDIA 不兼容、NeMo+Dynamo 从零重做)前提下，**29 个信号的逐条提取配方**（确切 file:line + W/S/H 提取机制 + 保真度 / 成本 / 接缝，两轮回 v0.15.1 源码核验）+ 对报告 12–16 的坐标勘误 + 自有全栈相对 NVIDIA 的解锁清单 + host 门控重评估 + 采集层落地路线 |

### 核心发现（v0.15.1）

下面是跨三条线路的实测结论摘要。每条都在对应报告里有 file:line 证据和实测请求支撑，详见报告 13（OpenAI 路径）/ 14（Anthropic 路径）/ 15（真实 CLI）。

- **prompt caching / `cache_control`**：两种线路、两种布局——Anthropic native 把 `cache_control` 打在 content 块级别(content-level)，OpenRouter wire 把它打在消息顶层 envelope(envelope-level)。断点策略一致：system + 最后 3 条非系统消息(`system_and_3`)，随对话增长向数组末尾滑动。
- **会话亲和 / `session_id`**：不同 provider 的会话标识差别很大，直接决定下游能否做 KV 复用——
  - 标准 OpenAI Chat Completions：**无**会话标识（请求体只有 `messages` / `model` / `stream`），后端无从把多次请求关联到同一会话。
  - OpenRouter（`chat_completions`）：**`body.session_id`（全模型都带）**；grok 模型额外带 `x-grok-conv-id` header。
  - Codex / xAI（Responses API）：header `session_id` / `x-client-request-id` + body `prompt_cache_key`（三者同值），让后端可按此键做前缀缓存命中。
- **上下文压缩**：preflight（请求前按估算长度预判）+ real-usage（请求后按后端返回的真实 usage 复判）**双触发**；压缩本身用一次辅助请求生成摘要，期间 `session_id` 全程不变，因此压缩不会打断会话亲和。
- **context 探测**：Ollama 后端会先打 `/api/show` 探测模型的 `context_length`，据此确定压缩阈值。

## 三、验证平台 `validation_platform/`

第一套验证平台，用 import-driver 驱动 OpenAI / Anthropic / Responses / Gemini 四类线路，捕获出站请求做字节级核对。它是可复现的 mock 验证环境：所有请求打到本地 mock 后端，不触碰真实 API。

| 文件 | 说明 |
|------|------|
| [mock_backend.py](validation_platform/mock_backend.py) | mock LLM 后端，支持 Chat Completions / Anthropic Messages / Responses / Gemini，完整记录请求（`Authorization` 头脱敏），可控 usage 触发压缩 |
| [driver.py](validation_platform/driver.py) | 用真实 `AIAgent.run_conversation()` 驱动场景 A–J |
| [requests.jsonl](validation_platform/requests.jsonl) | 捕获的 26 个真实请求（v0.15.1） |
| [key_samples.json](validation_platform/key_samples.json) / [codex_xai_samples.json](validation_platform/codex_xai_samples.json) | 关键请求样本 |

**复现**：

```bash
# 1. 启动 mock 后端（监听 8900 端口）
python3 validation_platform/mock_backend.py 8900

# 2. 用真实 hermes 安装的 venv 跑 driver（确保用的是被测版本的解释器）
~/.hermes/hermes-agent/venv/bin/python3 validation_platform/driver.py

# 3. 查看捕获请求
cat validation_platform/requests.jsonl | python3 -m json.tool
```

## 四、Anthropic 深度测试平台 `anthropic_platform/`

第二套平台，专门针对 **Anthropic SDK 路径**(`api_mode=anthropic_messages`)，配套报告 14。相比第一套，它的 mock 加了一个**控制端点**，可以按场景下发任意 usage / cache / stop_reason / 错误，从而精确触发 Anthropic 路径独有的上下文管理分支。

| 文件 | 说明 |
|------|------|
| [mock_anthropic.py](anthropic_platform/mock_anthropic.py) | 增强版 mock Anthropic 后端：**控制端点** `/__mock/control` 按场景下发行为（可编程 usage / cache / stop_reason / content-block / 错误注入）+ 协议正确的 Anthropic SSE 流 |
| [driver_anthropic.py](anthropic_platform/driver_anthropic.py) | 13 个场景 S1–S13（断点滑动 / TTL / native·envelope 布局 / beta / OAuth / thinking 五分支 / cache 回读 / 压缩 / length 续写 / 签名 400 / redacted_thinking / tier 429） |
| [check_assertions.py](anthropic_platform/check_assertions.py) | 程序化断言检查器（**25 PASS / 0 FAIL / 4 INFO**） |
| [anthropic_requests.jsonl](anthropic_platform/anthropic_requests.jsonl) | 捕获的 37 个真实请求 |

**关键发现**（详见报告 14，均经对抗性验证；这里的 INFO 项指"有 file:line 源码证据、但 localhost mock 触发不了"的断言）：

- **cache_control = `system_and_3`**：system + 最后 3 条非系统消息，封顶 4 个断点，随对话向数组末尾滑动（实测断点索引序列 `[0]→[0,1,2]→[2,3,4]→[4,5,6]`）。
- **TTL**：5m 缓存写成 `{type:ephemeral}`（不带 ttl 键）；1h 缓存写成 `{type:ephemeral, ttl:"1h"}`。缓存能力已 GA，因此请求里**不再**带 prompt-caching / extended-cache-ttl 这两个 beta 头。
- **thinking 五分支**：老模型（≤4.5）用 `{type:enabled, budget_tokens:N}`（high 档 = 16000）；4.6+ 用 `{type:adaptive}` + `output_config.effort`；**4.6+ 的 xhigh 档会降级为 max**；thinking 关闭或 haiku 模型则完全不注入 thinking 字段。
- **usage 三字段聚合**：`prompt_tokens = input + cache_read + cache_creation`，由 SDK 的 `get_final_message()` 聚合得到。注意 Hermes 内部把缓存写入这一桶叫 `cache_write_tokens`，只有 Anthropic wire 字段才叫 `cache_creation_input_tokens`。
- **host 门控发现**：OAuth 身份头要求 `base_url` 含 `anthropic.com` 子串（是子串匹配而非严格 host 相等，连空 base_url 也会触发）；envelope 布局则要求 host 命中 `openrouter.ai`——这两类在 localhost mock 上都触发不了，只能靠源码确认。
- **native 压缩摘要逃逸**：native Anthropic 路径做压缩时，生成摘要的辅助 client 不继承 mock 的 base_url，会直接打到真实的 `api.anthropic.com`；而第三方 / custom 路径下，摘要请求会乖乖留在配置的 base_url 上。这是测试时必须警惕的一个真实逃逸点。

**复现**：

```bash
python3 anthropic_platform/mock_anthropic.py 8910 &
~/.hermes/hermes-agent/venv/bin/python3 anthropic_platform/driver_anthropic.py
~/.hermes/hermes-agent/venv/bin/python3 anthropic_platform/check_assertions.py
```

## 五、真实 CLI 探测测试台 `cli_platform/`（配套报告 15）

前两套平台都用 import-driver（在进程内调 `AIAgent.run_conversation()` 程序化驱动）。第三套平台改用**真实 `hermes` CLI**（即 `hermes chat` 子命令），走完整的 config 解析 + **全量 system prompt（16k 字符）+ 29 tools** 入口，目的是补上 import-driver 的盲区，并产出**真实后端测试台的 fixtures**。

| 文件 | 说明 |
|------|------|
| [driver_cli.py](cli_platform/driver_cli.py) | 子进程编排真实 `hermes chat`（**完全不 import hermes，只 shell out**）；多轮对话经 `--resume` + SessionDB 串接；自动起停 mock |
| [check_cli.py](cli_platform/check_cli.py) | 程序化断言（**25 PASS / 0 FAIL / 3 INFO**） |
| [gen_fixtures.py](cli_platform/gen_fixtures.py) / [fixtures.json](cli_platform/fixtures.json) | 后端无关 fixtures（触发命令 + 期望 agent-hint 签名 + 容差）→ 真实后端测试台的输入 |
| [cli_requests.jsonl](cli_platform/cli_requests.jsonl) | 捕获的真实 CLI 请求 |

12 个场景编号 RC-01～RC-10（RC = Real-CLI，分别映射回报告 13 的 A–J 与报告 14 的 S1–S13 用例）。**关键差异**：真实 CLI 即便配置里写 `toolsets:[]`，出站请求里照样带 29 tools + 16096 字符 system，而 import-driver 在同等输入下只看到 0 工具 / ~1700 字符 system。

> ⚠️ **务必看懂"29 个工具"的含义**（报告 17 §2 已勘误）：这不是矛盾，而是**两个不同的层**——
> - import-driver 调的是 Hermes 内部函数 `get_tool_definitions(enabled_toolsets=[])`。该函数里有个 `if enabled_toolsets is not None` 判断，而**空列表 `[]` 不等于 `None`**，于是它对一个空列表做循环，结果是 **0 个工具**。
> - 真实 CLI 读的是 `config.yaml` 里的 `toolsets: []`，CLI 这一层**不会**把它原样当成内部的 `enabled_toolsets=[]`——转换后**仍然带上一批核心工具**(`_HERMES_CORE_TOOLS`)，实测出站请求里有 **29 个**。
>
> 所以 **config 层的 `toolsets:[]` ≠ 内部参数 `enabled_toolsets=[]`**：两个"0 工具 / 29 工具"都对，只是测的不是同一层。同理"16096 字符 system"是"默认带上 AGENTS.md 等上下文文件"时的量级，**不是固定常量**——换配置、换工具集都会变。**29 与 16096 都是环境/配置相关的捕获值，采集时一律从真实请求(wire)里读，绝不硬编码。**

此外，thinking 经 `agent.reasoning_effort` 这个 config 项**自然触发**（不需要 monkeypatch）；OpenRouter 系列因 host 门控（需 `openrouter.ai` host）在 localhost 激活不了，对应场景标 INFO 留到真实后端阶段。本平台复用 `anthropic_platform/mock_anthropic.py`（通过 `MOCK_LOGFILE` 环境变量把日志重定向开）。

**复现**：

```bash
~/.hermes/hermes-agent/venv/bin/python3 cli_platform/driver_cli.py 8920   # 自动起停 mock
~/.hermes/hermes-agent/venv/bin/python3 cli_platform/check_cli.py
~/.hermes/hermes-agent/venv/bin/python3 cli_platform/gen_fixtures.py
```

---

> ⚠️ 这些报告是对 Hermes Agent 的逆向 / 学习性分析，用途为推理框架的 Agent Hint 亲和性优化研究。请求样本中的密钥已脱敏；mock 的 cache usage 为脚本编造，非真实缓存语义。
