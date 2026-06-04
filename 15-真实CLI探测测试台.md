# 15 — 真实 hermes CLI + mock 探测测试台

> 目标:用**真实 `hermes` CLI**(`hermes chat` 子命令,走完整 config 解析 + 全量 system prompt + tools 入口)对 mock 跑,捕获 agent hints + 验证触发场景,产出**后端无关 fixtures** 作为后期**真实后端测试台**的输入。
> 动机:补 import-driver(报告 12/13/14 用 `AIAgent.run_conversation()` 程序化驱动)的已知盲区 —— **无 tools / system prompt 缩水 / 绕过 config→状态映射 / monkeypatch 强制触发**。
> 方法:`hermes chat` 每轮一个子进程 + 共享 SessionDB 多轮;mock 复用 `anthropic_platform/mock_anthropic.py`(加 `/v1/responses`)。**12 个场景,25 PASS / 0 FAIL / 3 INFO,0 外部逃逸**。

---

## 1. 为什么要真实 CLI(与 import-driver 的关键差异)

| 维度 | import-driver(报告 12-14) | 真实 CLI(本报告) |
|------|---------------------------|--------------------|
| 入口 | `AIAgent.run_conversation()` 直接构造 | `hermes chat` 子命令,走 CLI + config 解析 |
| tools | `enabled_toolsets=[]` → **0 tools** | 即便 `toolsets:[]` → **29 tools**(browser_*/execute_code/terminal/web_search/todo/...) |
| system prompt | ~1700 字符(缩水) | **16096 字符**(含 AGENTS/rules/工具说明,不加 `--ignore-user-config`) |
| 触发方式 | monkeypatch(`_supports_reasoning_extra_body`/`threshold_tokens`) | **纯 config + 真实模型名**(`agent.reasoning_effort` 等),无 monkeypatch |
| 配置映射 | 手设 agent 内部状态 | 经 `config.yaml` → agent 状态(覆盖真实映射链路) |
| 多轮 | 手动传 `result['messages']` | `--resume <SID>` 经 SessionDB 还原(= gateway 真实路径) |

## 2. CLI harness 设计(关键契约,均实测确认)

- **受控多轮必须走 `hermes chat`,NOT `-z`**:`-z` oneshot 分支(`main.py:15516`)在 resume 短路(`:15528`)之前,完全忽略 `--continue/--resume`(scout 现象 msgs 恒=2 的根因)。改用 `hermes chat`:turn1 建会话并从 stderr 抓 `session_id`(`\d{8}_\d{6}_[0-9a-f]{6}`),后续轮 `--resume <SID>` 经 SessionDB 还原历史 → mock 看到 msgs 累积(实测 1→3→5)。
- **`chat` 路径不能加 `--ignore-user-config`**:它把 `{HERMES_HOME}/config.yaml` 当 user config,加了会丢弃场景配置。
- **provider 名 = custom_providers 条目 name**(不是字面 "custom");否则解析器返回空 key 报错。
- **`security.allow_lazy_installs:false` + `-t ''`** 避免可选 toolset(edge-tts 等)懒安装卡死子进程。
- **压缩/tier 场景必须用第三方 anthropic-wire**(`custom` + `/anthropic` base_url):native `provider=anthropic` 的压缩摘要辅助 client 会逃逸真实 api.anthropic.com。
- **单个 `-q` 参数有 ~128KB(MAX_ARG_STRLEN)限制**:压缩场景的大 filler 须 < 128KB,历史经 SessionDB 累积(不占 argv)。

平台文件 `cli_platform/`:`driver_cli.py`(子进程编排 + SID 抓取 + mock 控制,**不 import hermes,只 shell out**)、`check_cli.py`(断言)、`gen_fixtures.py`(后端无关 fixtures 生成)、`cli_requests.jsonl`(捕获)、`fixtures.json`。

## 3. 场景矩阵与结果(12 场景,25 PASS / 0 FAIL / 3 INFO)

| 场景 | 映射用例 | 触发(config/CLI) | 实测结果 |
|------|---------|--------------------|----------|
| **RC-01** | A | custom chat_completions | **29 tools + system 16096 字符** + stream_options;无 body.session_id ✅ |
| **RC-02** | E,S3,B,S5 | 第三方 /anthropic + 3 轮 `--resume` | native cache_control 断点滑动 **`[0]→[0,1,2]→[2,3,4]`**,封顶 4;beta 2;system 字节稳定(16110×3);带 29 tools ✅ |
| **RC-03** | S2 | `prompt_caching.cache_ttl:1h` | marker `{type:ephemeral, ttl:1h}` ✅ |
| **RC-04a** | D,S7 | `claude-opus-4-7` + `reasoning_effort:xhigh` | `thinking={adaptive}` + `output_config={effort:xhigh}` ✅ |
| **RC-04b** | S7 | `claude-opus-4-6` + xhigh | `output_config={effort:max}`(版本降级)✅ |
| **RC-04c** | S7 | `claude-3-7-sonnet` + high | `thinking={enabled, budget_tokens:16000}` ✅ |
| **RC-05** | H | custom `api_mode:codex_responses` | `/v1/responses`、`store=false`、`prompt_cache_key`、`instructions`(非 messages)、无 codex header ✅ |
| **RC-06** | G | provider=openrouter | **INFO**:openrouter profile 需 host==openrouter.ai,localhost 无法激活(实跑会打真实 openrouter.ai 401)→ 留真实后端阶段 |
| **RC-07** | J,S4 | openrouter + reasoning | **INFO**:双 host 门控(reasoning extra_body + envelope 布局)→ 留真实后端阶段 |
| **RC-08** | F,S9 | 第三方 + `compression` + 大 filler | 压缩触发(`📦 Preflight ~111005 ≥ 100000` 阈值;主请求 msgs 非单调 compaction);summary 辅助请求非流式单 user 留在 mock(verbose 验证)✅ |
| **RC-09** | S13 | `inject_tier_429_once` | 429 → 降 context_length + 压缩 + retry(≥2 请求)✅ |
| **RC-10** | S10 | `stop_reason_once:max_tokens` | length 续写(≥2 请求);system 跨续写稳定 ✅ |

> **RC-04 尤其有价值**:import-driver 靠 monkeypatch `_supports_reasoning_extra_body=lambda:True` 才触发 reasoning;真实 CLI 用 `agent.reasoning_effort` config + 真实模型名**自然触发** thinking 五分支,比 import-driver 更忠实。

## 4. 关键发现

- **CLI 入口的请求远比 import-driver 丰富**:29 tools + 16096 字符 system,即便 `toolsets:[]`。推理框架在真实部署下看到的是这个量级的 prefix,不是 import-driver 的缩水版 —— 这直接影响 KV cache 前缀命中与亲和性。
- **压缩阈值有 64000 token 硬地板**(`threshold_tokens=max(ctx*0.5, MINIMUM_CONTEXT_LENGTH=64000)`),且 `context_length` 配置会被**模型真实窗口元数据覆盖**(claude-sonnet-4 → 200000 → 阈值 100000)。CLI 无法像 import-driver 那样 override 阈值,只能让消息内容真累积过阈值才触发。
- **host 门控是真实约束**:OpenRouter profile 的 `body.session_id`/`x-grok-conv-id`/envelope 布局/reasoning extra_body 都要求 base_url host 命中 `openrouter.ai`;`provider=openrouter`(内置)不采用 custom_providers 同名条目的 base_url override,localhost 一律打不通 → 这些 hint **只能在接真实 openrouter 后端时验证**(正是 fixtures 的用途)。
- **`--pass-session-id` 只注入 system prompt 文本**,不进任何 header/body 业务字段(`system_prompt.py:332`)。
- **多轮 = gateway 真实路径**:`--resume` 每轮 fresh AIAgent + 从 SessionDB 还原历史,与 gateway 每条入站消息新建 AIAgent 一致。

## 5. 真实后端测试台输入(fixtures)

`cli_platform/fixtures.json` 沉淀 12 个后端无关 fixture,每个含:**触发**(指向 `driver_cli.py` 的 config + `hermes chat` 命令)、**期望 agent-hint 签名**(从捕获提取)、**容差**(session_id/时间戳用 pattern `^\d{8}_\d{6}_[0-9a-f]{6}$`;system 校验量级与字节稳定不逐字;tools 校验 count 与关键子集不锁完整集)。

**用法(真实后端阶段)**:把同一 `hermes chat` 命令的 base_url 指向**真实推理后端**,用 fixture 的签名 + 容差对照实际请求 —— 即可验证真实后端是否按预期收到这些 agent hints,并回归 host 门控项(RC-06/07)。

## 6. 局限与诚实声明

- RC-06/07(OpenRouter)host 门控,localhost mock 测不了,标 INFO 留真实后端阶段(已跳过执行避免打真实 openrouter.ai)。
- RC-08 的 summary 辅助请求在批量跑中偶发未捕获(compaction 必触发;summary 在 verbose 单独验证中稳定捕获,`/v1/v1/messages kind=summary stream=false`)。
- mock 的 usage/cache 数字为脚本编造,非真实缓存语义;断点**位置**真实,缓存**命中率**不做断言。
- 复现:`<venv-python> cli_platform/driver_cli.py 8920`(自动起停 mock)→ `check_cli.py` → `gen_fixtures.py`。
