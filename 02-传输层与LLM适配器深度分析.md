# Hermes Agent 传输层与LLM适配器深度分析

## 一、传输抽象层架构

### 1.1 核心抽象

传输层采用**桥接模式（Bridge Pattern）**，将内部 OpenAI 风格消息格式与各提供商的原生 API 协议解耦。

```
ProviderTransport (ABC) — agent/transports/base.py
├── ChatCompletionsTransport   — 16+ OpenAI兼容提供商
├── AnthropicTransport         — Anthropic Messages API
├── ResponsesApiTransport      — OpenAI Responses API (GPT-5.x)
└── BedrockTransport           — AWS Bedrock Converse
```

### 1.2 统一接口四管道

| 方法 | 职责 |
|------|------|
| `convert_messages()` | OpenAI消息 → 提供商原生格式 |
| `convert_tools()` | OpenAI工具定义 → 提供商原生格式 |
| `build_kwargs()` | 构建完整API调用参数字典（主入口） |
| `normalize_response()` | 原生响应 → NormalizedResponse统一类型 |

### 1.3 归一化共享类型 (agent/transports/types.py)

- `ToolCall`: 统一工具调用表示（向后兼容 `tc.function.name`）
- `Usage`: Token使用统计
- `NormalizedResponse`: 归一化API响应，通过 `provider_data` 携带协议特有元数据

### 1.4 传输注册表 (agent/transports/__init__.py)

```python
_REGISTRY: dict = {}   # api_mode → transport_cls
register_transport("chat_completions", ChatCompletionsTransport)
register_transport("codex_responses", ResponsesApiTransport)
register_transport("anthropic_messages", AnthropicTransport)
register_transport("bedrock_converse", BedrockTransport)
```

延迟导入 + 增量发现——未命中时重新扫描。

## 二、ChatCompletionsTransport — 默认路径

**约16+ OpenAI兼容提供商共用**（OpenRouter、Nous、NVIDIA、Qwen、Ollama、DeepSeek、Kimi等）。

关键设计：
- **消息净化**：剥离 `codex_reasoning_items`、`call_id`、`response_item_id` 等内部字段
- **双路径模式**：`ProviderProfile` 路径（新）与遗留flag路径（旧）并存迁移
- **Gemini思考配置转换**：`_build_gemini_thinking_config()` 按模型家族精确调整 thinkingConfig
- **提供商特化分支**：Moonshot工具净化、Kimi思考模式、Tencent TokenHub、LM Studio等

## 三、AnthropicTransport — Messages API

所有方法委托给 `agent/anthropic_adapter.py`（~2300行）：

**消息转换（convert_messages_to_anthropic）：**
- OpenAI messages[] → Anthropic (system, messages) 元组
- **思考签名管理**：Anthropic使用专有签名保护思考块
  - 第三方端点：全部剥离
  - Kimi/DeepSeek：剥离签名块，保留未签名块
  - 直接Anthropic：保留签名块，降级未签名块

**工具转换（convert_tools_to_anthropic）：**
- `function.parameters` → `input_schema`
- 通过 `strip_nullable_unions` 移除可为空联合类型
- 剥离顶层 `oneOf`/`allOf`/`anyOf`（Anthropic验证器拒绝）

**客户端构建：**
- 支持5种认证方式：API Key、setup-token、OAuth token、callable token provider (Entra ID)、Bedrock适配器
- OAuth token自动刷新（PKCE流程）

**模型能力检测：**
- `_supports_adaptive_thinking()`、`_supports_xhigh_effort()`、`_forbids_sampling_params()`
- 按模型家族的thinking模式设置（adaptive vs manual budget_tokens）

## 四、ResponsesApiTransport — Codex路径

委托给 `agent/codex_responses_adapter.py`（~1261行）：

**消息转换（_chat_messages_to_responses_input）：**
- OpenAI messages[] → Responses API input[] items
- **多轮推理连续性**：重放前一轮的加密推理项
- **跨签发者防护**：通过 `_issuer_kind` stamp检测和过滤跨端点不兼容推理块
- **prefix cache优化**：重放完整assistant message items

**响应归一化（_normalize_codex_response）：**
- 工具调用泄漏恢复：检测带有和谐序列化标记的文本内容，触发重试
- 完成原因推断（含 `incomplete` 状态、仅有推理的输出等）

**三后端支持：** OpenAI Codex原生、GitHub Models、xAI/Grok

## 五、BedrockTransport — AWS Bedrock

**关键设计：**
- 注入sentinel键（`__bedrock_converse__`、`__bedrock_region__`）用于分发识别
- 兼容两种输入形态：原始boto3 dict 或 预归一化SimpleNamespace
- 按region缓存客户端，过期连接检测自动重建
- AWS凭证优先级链：Bearer token > Access Key > Profile > 容器 > Web Identity > IAM Role

## 六、调用路由流程

```
AIAgent初始化 → 确定api_mode → 获取transport
     ↓
每次agent回合:
  build_api_kwargs → transport.build_kwargs()
  interruptible_api_call → 按api_mode分派:
    codex_responses → _run_codex_stream()
    anthropic_messages → _anthropic_messages_create()
    bedrock_converse → boto3 client.converse()
    chat_completions → openai_client.chat.completions.create()
  normalize_response → transport.normalize_response()
  build_assistant_message → 内部消息dict
```

## 七、Gemini集成生态

- **gemini_native_adapter.py**：暴露OpenAI兼容facade调用Gemini原生REST API
- **gemini_cloudcode_adapter.py**：通过Google OAuth PKCE调用Code Assist后端（Bearer token + Cloud Code信封）
- **gemini_schema.py**：递归剥离Gemini不接受的JSON Schema键
- **google_code_assist.py**：Code Assist控制面（项目发现、用户入职、配额查询）
- **google_oauth.py**：完整OAuth PKCE流程（本地回调HTTP服务器 + 跨进程并发刷新去重）
