# Hermes Agent 深度调研与上下文管理验证

对 [Hermes Agent](https://github.com/NousResearch/hermes-agent)(Nous Research)代码库的深度架构调研，以及上下文管理 / 会话亲和特性的**源码分析 + 真实请求实测验证**。

> 测试版本：Hermes Agent **v0.15.1**（真实安装 `~/.hermes/hermes-agent/` 与源码树一致）
> 验证方式：用真实 `AIAgent.run_conversation()` 驱动 + mock 后端捕获真实 HTTP 请求

---

## 一、架构调研报告（自顶向下 + 自底向上）

| # | 报告 | 内容 |
|---|------|------|
| 01 | [总体架构概览](01-总体架构概览.md) | 整体分层、模块划分、数据流 |
| 02 | [传输层与 LLM 适配器深度分析](02-传输层与LLM适配器深度分析.md) | `ProviderTransport` ABC、四类传输 |
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

| # | 报告 | 内容 |
|---|------|------|
| 12 | [所有传输层 Agent-Hints 总结](12-所有传输层Agent-Hints总结.md) | 各传输/provider 注入的 header、body 字段对比表 |
| 13 | [上下文管理特性 — 源码与实测验证](13-上下文管理特性-源码与实测验证.md) | **最权威**：上下文管理特性源码 + 真实请求实测，含版本核对 |

**核心发现（v0.15.1）**：

- **prompt caching / `cache_control`**：Anthropic native（content-level）/ OpenRouter wire（envelope-level），断点策略 = system + 最后 3 条非系统消息，随对话增长向后滑动。
- **会话亲和 / session_id**：
  - 标准 OpenAI Chat Completions：**无**会话标识（仅 messages/model/stream）
  - OpenRouter（chat_completions）：**`body.session_id`（全模型，v0.15.1 起注入）**；grok 模型额外 `x-grok-conv-id` header
  - Codex / xAI（Responses API）：header `session_id`/`x-client-request-id` + body `prompt_cache_key`（同值）
- **上下文压缩**：preflight + real-usage 双触发，压缩用辅助请求生成摘要，session_id 全程不变。
- **context 探测**：Ollama `/api/show` 探测 `context_length`。

## 三、验证平台 `validation_platform/`

可复现的 mock 验证环境：

| 文件 | 说明 |
|------|------|
| [mock_backend.py](validation_platform/mock_backend.py) | mock LLM 后端，支持 Chat Completions / Anthropic Messages / Responses / Gemini，完整记录请求（Authorization 脱敏），可控 usage 触发压缩 |
| [driver.py](validation_platform/driver.py) | 用真实 `AIAgent.run_conversation()` 驱动场景 A–G |
| [requests.jsonl](validation_platform/requests.jsonl) | 捕获的 24 个真实请求（v0.15.1） |
| [key_samples.json](validation_platform/key_samples.json) / [codex_xai_samples.json](validation_platform/codex_xai_samples.json) | 关键请求样本 |
| [mock_capture.py](mock_capture.py) | 早期单文件抓包脚本 |

**复现**：

```bash
# 1. 启动 mock 后端
python3 validation_platform/mock_backend.py 8900

# 2. 用真实 hermes 安装的 venv 跑 driver
~/.hermes/hermes-agent/venv/bin/python3 validation_platform/driver.py

# 3. 查看捕获请求
cat validation_platform/requests.jsonl | python3 -m json.tool
```

---

> ⚠️ 这些报告是对 Hermes Agent 的逆向/学习性分析，用途为推理框架的 Agent Hint 亲和性优化研究。请求样本中的密钥已脱敏。
