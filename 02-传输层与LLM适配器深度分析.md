# Hermes Agent 传输层与LLM适配器深度分析

> **导言 / TL;DR**
>
> Hermes 是一个 agent 框架:它在内部只用**一种**消息格式(OpenAI Chat Completions 风格的 `messages[]` 数组,即「role + content + tool_calls」那一套)来组织对话历史与工具调用,但它需要把同一段对话发往**许多家**互不兼容的大模型后端——OpenAI、Anthropic、AWS Bedrock、Google Gemini、xAI/Grok,以及一大批「OpenAI 兼容」的第三方网关。每家后端的线上协议(wire protocol,即 HTTP 请求体的字段结构)各不相同:Anthropic 的 system 是独立字段、思考块带专有签名;OpenAI 的新 Responses API 用 `input[]` 而不是 `messages[]`;Bedrock 走 boto3 的 `converse()` 而非 HTTP JSON。
>
> **传输层(transport layer)就是这层「翻译适配器」**:把 Hermes 内部统一的 OpenAI 风格消息,翻译成目标后端能听懂的原生格式;再把后端五花八门的响应翻译回 Hermes 内部统一的 `NormalizedResponse` 类型。本报告自底向上拆解这套机制:先讲抽象基类与四类具体传输如何协作(第一节),再逐一深入四条具体翻译路径(第二至五节:Chat Completions / Anthropic / Codex Responses / Bedrock),然后说明 agent 主循环每一回合是如何挑选并调用传输的(第六节),最后单列 Gemini 这一整套独立适配生态(第七节)。
>
> **读者画像**:你熟悉推理服务、KV cache、LLM 工程的一般概念,但不了解 Hermes 内部结构。文中凡 Hermes 专有名词(如 `api_mode`、`provider_data`、sentinel 键、思考签名)首次出现都会先解释再使用;凡给出 `文件:行` 坐标的地方都会说明「那行在做什么」。
>
> **本报告性质说明**:这是一份**架构走读(code walk-through)**,基于对源码结构的静态阅读,聚焦「代码长什么样、各模块如何分工」,不含运行时实测数据(没有 PASS/FAIL/INFO 计数、没有时延数字)。如需 agent hint 注入、KV/调度信号的实测结论,详见报告 16、报告 17。

---

## 一、传输抽象层架构

### 1.1 核心抽象:为什么用桥接模式

传输层采用**桥接模式(Bridge Pattern)**。桥接模式是一种设计模式,它的要点是把「抽象」和「实现」拆成两根可以各自独立变化的轴。放到这里:一根轴是「Hermes 内部的 OpenAI 风格消息格式」(抽象,基本固定),另一根轴是「各家提供商的原生 API 协议」(实现,种类多且会变)。两根轴通过一个统一接口解耦,于是新增一个后端只需新写一个实现类,而不必改动 agent 主循环——主循环始终只跟统一接口打交道。

抽象基类与四个具体实现的继承结构如下:

```
ProviderTransport (ABC) — agent/transports/base.py
├── ChatCompletionsTransport   — 16+ OpenAI兼容提供商
├── AnthropicTransport         — Anthropic Messages API
├── ResponsesApiTransport      — OpenAI Responses API (GPT-5.x)
└── BedrockTransport           — AWS Bedrock Converse
```

这里 `ProviderTransport` 是抽象基类(ABC,Abstract Base Class,即只定义接口、不能直接实例化的「合同」),定义在 `agent/transports/base.py`。它用 `abc.ABC` + `@abstractmethod` 强制所有子类必须实现下面四个方法(在 `base.py:16` 的 `class ProviderTransport(ABC)` 处声明,四个抽象方法分别在 `base.py:26`、`base.py:35`、`base.py:43`、`base.py:60`)。四个具体子类分别对应四条翻译路径,后面四节逐一展开。

### 1.2 统一接口:四个方法构成的「翻译流水线」

任何一个传输类,无论目标后端是谁,都必须实现下面四个方法。可以把它们理解成一条流水线:前两个负责把「请求侧」的两类数据(对话消息、工具定义)翻译出去,第三个把它们组装成一次完整 API 调用的参数,第四个负责把「响应侧」翻译回来。

| 方法 | 职责 |
|------|------|
| `convert_messages()` | OpenAI消息 → 提供商原生格式 |
| `convert_tools()` | OpenAI工具定义 → 提供商原生格式 |
| `build_kwargs()` | 构建完整API调用参数字典（主入口） |
| `normalize_response()` | 原生响应 → NormalizedResponse统一类型 |

逐条解释这四步在做什么:

- **`convert_messages()`**:输入是 Hermes 内部那份 OpenAI 风格的 `messages[]`(一个个 `{role, content, ...}` 的字典),输出是目标后端原生的消息结构。比如对 Anthropic,它要把单一的 `messages[]` 拆成 `(system, messages)` 二元组,因为 Anthropic 协议里 system 提示是请求体顶层的独立字段,而不是 messages 里一条 `role:"system"` 的消息。
- **`convert_tools()`**:输入是 OpenAI 风格的工具定义(`{type:"function", function:{name, parameters, ...}}`),输出是目标后端能接受的工具 schema。不同后端对 JSON Schema 的容忍度不同(后文会看到 Anthropic 会拒绝某些 schema 键),所以这一步常常包含「清洗」逻辑。
- **`build_kwargs()`**:这是请求侧的**主入口**。它内部会调用上面两个转换方法,再把模型名、采样参数、流式开关、各类 header 等一起拼成「一次 API 调用所需的完整关键字参数字典(kwargs)」。agent 主循环拿到这个字典后直接喂给对应的客户端 SDK。
- **`normalize_response()`**:输入是后端返回的原生响应(各家结构迥异),输出是 Hermes 内部统一的 `NormalizedResponse`(见 1.3)。有了它,主循环解析响应时不必关心「这次到底是哪家后端」。

### 1.3 归一化共享类型(agent/transports/types.py)

四条路径翻译回来的响应必须落到同一套数据类型上,主循环才能用统一代码处理。这套共享类型定义在 `agent/transports/types.py`,核心有三个:

- **`ToolCall`**(`types.py:19` 的 `class ToolCall`):统一的「工具调用」表示。一个细节是它需要**向后兼容旧代码里 `tc.function.name` 这种访问写法**——历史上 agent 主循环是按 OpenAI SDK 对象的 `tc.function.name` / `tc.function.arguments` 来读工具名和参数的。`types.py:51` 处用一个返回 `self` 的 `function` 属性把这个访问链接住,使新结构在旧调用点上仍然能用(`types.py:41` 的注释明确写了「agent loop reads tc.function.name / tc.function.arguments」)。
- **`Usage`**(`types.py:80` 的 `class Usage`):Token 使用统计的归一化容器。
- **`NormalizedResponse`**(`types.py:90` 的 `class NormalizedResponse`):归一化后的 API 响应。它有一个关键字段 `provider_data`(`types.py:109` 声明为 `provider_data: dict[str, Any] | None`),专门用来携带**某一协议特有、其它协议没有的元数据**。这样设计的好处是:统一类型的「公共字段」保持干净,而协议特有的东西(例如 Codex 的 `call_id` / `response_item_id`、Gemini 的思考签名 `extra_content`)塞进 `provider_data` 字典里,只有「懂这个协议」的代码路径才去读它。`ToolCall` 上也有同名的 `provider_data` 字段(`types.py:38`),作用相同,只是粒度到单个工具调用——例如 Codex 的 `call_id` 就通过 `types.py:56` 的属性从 `provider_data` 里取出。

一句话总结这套类型的设计哲学:**「能统一的字段就统一,统一不了的就装进 `provider_data` 让特定路径自己解读」**。

### 1.4 传输注册表:api_mode 如何映射到传输类

Hermes 用一个字符串 `api_mode` 来标识「这次该走哪条翻译路径」。`api_mode` 是 Hermes 内部的关键枚举,共四类:`chat_completions` / `anthropic_messages` / `codex_responses` / `bedrock_converse`,分别对应 1.1 那四个传输类。注册表负责把 `api_mode` 字符串映射到对应的传输类,定义在 `agent/transports/__init__.py`:

```python
_REGISTRY: dict = {}   # api_mode → transport_cls
register_transport("chat_completions", ChatCompletionsTransport)
register_transport("codex_responses", ResponsesApiTransport)
register_transport("anthropic_messages", AnthropicTransport)
register_transport("bedrock_converse", BedrockTransport)
```

这里 `_REGISTRY` 是一个普通字典(`__init__.py:17`),`register_transport(api_mode, transport_cls)` 就是往字典里塞一条 `api_mode → 类` 的映射(`__init__.py:21`–`23`)。取用时通过 `get_transport(api_mode)`(`__init__.py:26`)按字符串查表,查不到返回 `None`。

**「延迟导入 + 增量发现——未命中时重新扫描」** 的含义是:传输类不是在进程启动时一次性全部 import 进来(那样会拖慢启动、也会在某些可选依赖缺失时直接报错),而是按需懒加载。`get_transport()` 第一次查表没命中时(`__init__.py:36` 先 `_REGISTRY.get(api_mode)`),会触发一次重新扫描/导入,再查一次(`__init__.py:43` 再 `_REGISTRY.get(api_mode)`)。这样做特别是为了应对测试或导入顺序的影响——正如 `__init__.py:41` 的注释所说,目的是「让测试/顺序相关的导入不会让某个合法 api_mode 变得不可用」。

---

## 二、ChatCompletionsTransport — 默认路径

这是覆盖面最广的一条路径,**约 16+ 家 OpenAI 兼容提供商共用**(包括 OpenRouter、Nous、NVIDIA、Qwen、Ollama、DeepSeek、Kimi 等)。所谓「OpenAI 兼容」,是指这些后端都暴露了与 OpenAI `/v1/chat/completions` 端点同构的接口,因此可以共用同一套翻译逻辑——这也是为什么它被当作「默认路径」。

但「兼容」不等于「完全一致」,各家总有自己的小脾气,所以这条路径里塞了不少特化处理。关键设计有四点:

- **消息净化(message sanitization)**:Hermes 内部的消息字典上挂着一些**纯内部字段**,比如 `codex_reasoning_items`(Codex 路径用的加密推理项)、`call_id`、`response_item_id` 等。这些字段对一个普通 OpenAI 兼容后端毫无意义,直接发过去轻则被忽略、重则触发后端 schema 校验报错。所以发请求前必须把它们**剥离干净**——这就是「净化」。

- **双路径模式:新旧迁移并存**。Hermes 正处在配置体系的迁移期。「新」路径走 `ProviderProfile`(一个结构化的「提供商画像」对象,把某家后端的各种行为差异收敛成声明式配置);「旧」路径走散落各处的遗留布尔开关(legacy flag)。两套并存,逐步把旧 flag 迁移到 profile,所以代码里能看到同一个行为既有 profile 分支又有 flag 分支。

- **Gemini 思考配置转换**:函数 `_build_gemini_thinking_config()` 负责把 Hermes 内部的「思考(thinking/reasoning)」意图,翻译成 Gemini 那套 `thinkingConfig` 结构,并且**按模型家族精确调整**——不同 Gemini 模型对思考预算的字段要求不一样,所以要按家族分别构造。(注:此处指经 OpenAI 兼容 facade 走 Chat Completions 形态访问 Gemini 的情况;Gemini 还有独立的原生适配生态,见第七节。)

- **提供商特化分支**:针对个别后端的怪癖各开一个小分支,例如 Moonshot 的工具净化、Kimi 的思考模式处理、Tencent TokenHub、LM Studio 等。这些分支共享主干流程,只在差异点上岔开,是「默认路径」承载多家后端的代价。

---

## 三、AnthropicTransport — Messages API

`AnthropicTransport` 本身是一层薄壳,所有实际转换逻辑都**委托给 `agent/anthropic_adapter.py`**(该文件约 2300 行——实测为 2303 行,是整个适配层里最重的一块)。之所以单独抽出一个大适配器,是因为 Anthropic 的 Messages API 在消息结构、思考块签名、认证方式、模型能力等多个维度都和 OpenAI 风格差异很大,逻辑量大到值得独立成文件。下面分四块说明。

### 3.1 消息转换(convert_messages_to_anthropic)

核心任务是把 Hermes 内部的 `messages[]` 翻译成 Anthropic 的 `(system, messages)` 二元组。关键点是 **Anthropic 把 system 提示放在请求体顶层的独立字段**,而不是像 OpenAI 那样作为 `messages` 里一条 `role:"system"` 的消息;所以转换时要把 system 内容「抽」出来单独成项。

这一块最微妙的是**思考签名管理(thinking signature)**。背景:Anthropic 在返回带「扩展思考(extended thinking)」的内容时,会给思考块附上一个**专有签名(signature)**,作为该思考块的完整性/来源凭证;下一轮把思考块回传时,签名必须和签发它的端点匹配,否则会被拒。问题在于,Hermes 允许把同一段对话在不同端点之间搬动(比如换了 provider),这时旧签名就可能「水土不服」。于是按签发来源分三种策略处理:

- **第三方端点(third-party)**:签名一律不可信,**全部剥离**思考块的签名。
- **Kimi / DeepSeek**:这些后端也走 Anthropic 兼容形态,但签名语义对不上——策略是**剥离带签名的块,保留未签名的块**。
- **直接 Anthropic(direct)**:签名是自己签发的、可信——**保留带签名的块,把未签名的块降级处理**。

这套三分策略的根因都是一个:**思考块的签名只在「签发它的那个端点」上有效**,跨端点重放必须按可信度分别取舍,否则要么被后端拒绝、要么丢失思考连续性。

### 3.2 工具转换(convert_tools_to_anthropic)

把 OpenAI 工具定义翻成 Anthropic 工具定义,有三处关键改写:

- **字段改名**:OpenAI 的 `function.parameters` 对应 Anthropic 的 `input_schema`。
- **移除可空联合类型**:通过 `strip_nullable_unions` 这个辅助函数,把 JSON Schema 里「可为 null 的联合类型」(例如 `type: ["string", "null"]` 这类)清理掉,因为 Anthropic 的 schema 校验器对这类写法不友好。
- **剥离顶层组合关键字**:把 schema 顶层的 `oneOf` / `allOf` / `anyOf` 去掉,因为 **Anthropic 的验证器会直接拒绝**带这些顶层组合关键字的 `input_schema`。

这三处都属于「目标后端 schema 比源格式更严格」时的必要降级——不清洗就会在请求阶段被后端打回。

### 3.3 客户端构建:五种认证方式

构建 Anthropic 客户端时支持 **5 种认证方式**,对应不同的接入身份:

1. **API Key**:标准的 Anthropic Console API key,走 `x-api-key` 头。
2. **setup-token**:Anthropic 的 OAuth setup-token(形如 `sk-ant-oat*`),走 Bearer 认证并需要附加 beta header(见 `anthropic_adapter.py:9` 的注释)。
3. **OAuth token**:完整 OAuth 流程拿到的访问令牌。
4. **callable token provider**:一个「可调用的令牌提供者」,用于像 Entra ID(微软 Azure 的身份服务)这种需要动态获取/刷新令牌的企业场景。
5. **Bedrock 适配器**:经由 Bedrock 通道访问 Anthropic 模型时的认证。

此外,**OAuth token 支持自动刷新**,走的是 PKCE 流程(PKCE 是 OAuth 的一种增强,避免授权码被中途截获)。这保证长时间运行的 agent 不会因为令牌过期而中断。

### 3.4 模型能力检测

不同 Claude 模型支持的「思考」能力不一样,适配器用几个判定函数来分流:

- `_supports_adaptive_thinking()`(`anthropic_adapter.py:210`):该模型是否支持「自适应思考」(由模型自己决定思考预算,而非调用方给定固定值)。
- `_supports_xhigh_effort()`(`anthropic_adapter.py:215`):是否支持 xhigh 这一档更高的推理 effort。
- `_forbids_sampling_params()`(`anthropic_adapter.py:226`):该模型是否**禁止**携带采样参数(某些思考型模型不接受 temperature 等采样参数,带了会报错)。

判定结果决定思考模式怎么设:支持自适应的走 **adaptive** 模式,不支持的就退回 **manual budget_tokens** 模式(由调用方显式给出思考预算的 token 数)。一句话:**按模型家族能力,在「自适应」与「手动给预算」之间二选一**。

---

## 四、ResponsesApiTransport — Codex路径

这条路径对应 OpenAI 的 **Responses API**(GPT-5.x 等使用的新一代接口,内部 `api_mode` 名为 `codex_responses`)。Responses API 与传统 Chat Completions 的最大区别是:它用 `input[]` 数组而非 `messages[]`,并且把「推理项(reasoning items)」作为一等公民在多轮之间传递。具体逻辑委托给 `agent/codex_responses_adapter.py`(约 1261 行——实测 1260 行)。

### 4.1 消息转换(_chat_messages_to_responses_input)

函数 `_chat_messages_to_responses_input`(定义在 `codex_responses_adapter.py:279`)把 Hermes 内部的 OpenAI 风格 `messages[]` 翻译成 Responses API 的 `input[]` 项序列。三个关键机制:

- **多轮推理连续性**:Responses API 允许在后续回合**重放(replay)前一轮的加密推理项**,让模型「记得」上一轮自己的思考过程,从而维持跨回合的推理连续性。

- **跨签发者防护(cross-issuer guard)**:这是最精巧的一处。背景:加密推理项是由「某个具体端点/签发者」生成的,只有同一签发者才能正确解读;如果把它误发给另一个不兼容的端点,轻则被拒、重则出错。防护办法是给每个推理项盖一个 `_issuer_kind` 戳(stamp,标识它是哪个签发者发的),转换时拿当前回合的签发者去比对:

  - `codex_responses_adapter.py:284` 引入参数 `current_issuer_kind`(当前回合的签发者种类);
  - `codex_responses_adapter.py:366` 取出推理项上携带的 `item_issuer = ri.get("_issuer_kind")`;
  - `codex_responses_adapter.py:368`–`370` 判定:若两者不一致(`item_issuer != current_issuer_kind`),则**过滤掉该不兼容推理项**;
  - `codex_responses_adapter.py:387`–`392` 在真正发出前还会**剥掉这个纯内部的 `_issuer_kind` 戳**(它只是 Hermes 内部标记,不属于线上协议字段),只保留 `id` 之外该过滤掉它(代码里写明 `if k not in ("id", "_issuer_kind")`)。

  按 `codex_responses_adapter.py:307`–`317` 的文档说明:存在两层粒度——一层是「全局开关」(某些情况直接丢弃**全部**重放),另一层就是这里逐项判定的 `current_issuer_kind` 过滤器。

- **prefix cache 优化**:重放时会**重放完整的 assistant message 项**。这么做是为了对齐后端的 prefix cache(前缀缓存):只要请求前缀逐字节一致,后端就能命中 KV cache、省下重复 prefill。所以这里「重放完整项」不是冗余,而是为了缓存命中刻意保持前缀稳定。

### 4.2 响应归一化(_normalize_codex_response)

把 Responses API 的原生响应翻回 `NormalizedResponse`,有两处特殊处理:

- **工具调用泄漏恢复(tool-call leakage recovery)**:有时模型本该用结构化的工具调用字段返回,却把工具调用「泄漏」成了普通文本内容(文本里夹带了 harmony 序列化标记——harmony 是 OpenAI 这套模型使用的一种结构化序列化格式)。归一化时若**检测到文本内容里带有 harmony 序列化标记**,说明发生了泄漏,于是**触发重试**,争取拿到正确的结构化输出。

- **完成原因推断(finish reason inference)**:Responses API 的「为什么停下」语义比 Chat Completions 复杂,需要额外推断,包括处理 `incomplete` 状态(输出未完成)、以及「这一轮只产出了推理项、没有正式回答」这类边界情况。

### 4.3 三后端支持

同一条 Responses 路径服务**三个后端**:OpenAI Codex 原生、GitHub Models、xAI/Grok。它们都暴露 Responses 兼容接口(见 `codex_responses_adapter.py:4` 的文件头注释:「used by OpenAI Codex, xAI, GitHub Models, and other Responses-compatible endpoints」),其中 xAI 这一支有专门的开关 `is_xai_responses`(`codex_responses_adapter.py:28`、`:282`)和模型名判定 `is_xai_model`(`codex_responses_adapter.py:928`,按 `grok-` / `x-ai/grok-` 前缀识别)来分流其特化行为。

---

## 五、BedrockTransport — AWS Bedrock

`BedrockTransport` 对应 AWS Bedrock 的 **Converse API**(`api_mode` 名为 `bedrock_converse`)。它和前几条路径最大的不同是:**不走 HTTP JSON,而是走 boto3 这个 AWS 官方 Python SDK 的 `converse()` 方法**。因此它的设计重点不在「拼 HTTP 请求体」,而在「与 boto3 客户端、AWS 凭证体系打交道」。关键设计有四点:

- **注入 sentinel 键用于分发识别**:sentinel(哨兵)键是一种「内部标记字段」,名字刻意用双下划线包裹以避免和真实业务字段冲突。这里在 `build_kwargs` 阶段往参数字典里塞了两个:`kwargs["__bedrock_converse__"] = True`(`bedrock.py:63`)和 `kwargs["__bedrock_region__"] = region`(`bedrock.py:64`,region 默认 `us-east-1`,见 `bedrock.py:51`)。下游的分发(dispatch)逻辑看到 `__bedrock_converse__` 这个哨兵,就知道「这次该调 boto3 的 converse 而不是某个 HTTP 客户端」,并从 `__bedrock_region__` 取出目标 region。

- **兼容两种输入形态**:归一化响应时,Bedrock 可能拿到两种输入——一种是 boto3 返回的**原始 dict**,另一种是分发点已经预先归一化好的 **`SimpleNamespace`**(一个带 `.choices` 属性、长得像 OpenAI 响应对象的轻量容器,见 `bedrock.py:72`、`bedrock.py:76`、`bedrock.py:129`)。归一化代码两种都能吃,谁来都能正确处理。

- **按 region 缓存客户端,并检测过期连接自动重建**:boto3 客户端的创建有开销,所以按 region 缓存复用;同时检测连接是否已失效(过期),失效则自动重建,避免拿着死连接发请求。

- **AWS 凭证优先级链**:按固定优先级依次尝试多种凭证来源——**Bearer token > Access Key > Profile > 容器(container)> Web Identity > IAM Role**。这条链覆盖了从「显式传入令牌」到「本地配置文件」再到「云上容器/实例角色自动获取」的各类部署形态,谁先可用就用谁。

---

## 六、调用路由流程:一次 agent 回合发生了什么

前五节讲的是「四条翻译路径各自长什么样」,这一节把它们串起来,看 agent 主循环每一回合是怎么挑选并驱动传输的。整体流程如下:

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

逐步拆解:

1. **初始化阶段**:`AIAgent` 启动时先**确定 `api_mode`**(根据所选 provider/模型解析出该走哪条路径),再用它从 1.4 的注册表里**取到对应的 transport 实例**。这一步只做一次,之后整个会话复用。

2. **每个 agent 回合(turn)** 重复以下三段:

   - **构建请求**:`build_api_kwargs` 调用 `transport.build_kwargs()`,得到本次调用的完整参数字典(内部已经做完了 `convert_messages` / `convert_tools`,见 1.2)。

   - **发起调用**:`interruptible_api_call`(「可中断的 API 调用」,意味着用户可以在生成中途打断)按 `api_mode` **分派**到对应客户端——
     - `codex_responses` → `_run_codex_stream()`(Codex 路径,流式)
     - `anthropic_messages` → `_anthropic_messages_create()`(Anthropic Messages 调用)
     - `bedrock_converse` → boto3 的 `client.converse()`(走 AWS SDK,呼应第五节的哨兵分发)
     - `chat_completions` → `openai_client.chat.completions.create()`(默认 OpenAI 兼容路径)

   - **归一化并落库**:`normalize_response` 调 `transport.normalize_response()` 把原生响应翻成 `NormalizedResponse`;最后 `build_assistant_message` 把它转回 Hermes 内部那份统一的 assistant 消息 dict,追加进对话历史,供下一回合使用。

这条流程清楚地体现了桥接模式的收益:**主循环自始至终只跟「统一接口」和「统一类型」打交道,四家后端的协议差异全被关在各自的 transport 里**。新增一家后端,主循环一行不用改。

---

## 七、Gemini集成生态

Google Gemini 的接入比其它后端复杂——它既能以「OpenAI 兼容」形态走默认路径(见第二节的 `_build_gemini_thinking_config()`),也有一整套**独立的原生适配生态**来直接访问 Google 自家后端。这套生态由下面五个文件分工协作:

- **`gemini_native_adapter.py`**:对外暴露一个「OpenAI 兼容的门面(facade)」,对内实际调用的是 **Gemini 原生 REST API**。所谓 facade,就是让上层代码以为自己在跟一个 OpenAI 兼容接口对话,底下却悄悄翻译成 Gemini 原生调用——这样上层无需为 Gemini 单开一套逻辑。

- **`gemini_cloudcode_adapter.py`**:走另一条入口——通过 **Google OAuth PKCE** 调用 **Code Assist 后端**。请求带 Bearer token,并包在一层「Cloud Code 信封(envelope)」里(envelope 指把实际请求体再裹一层外层结构,以符合 Code Assist 的协议要求)。这条路对应的是 Google 面向编码场景的 Code Assist 服务,与上面的原生 REST 是两个不同后端。

- **`gemini_schema.py`**:**递归剥离 Gemini 不接受的 JSON Schema 键**。和第三节 Anthropic 的工具 schema 清洗同理——Gemini 对工具/参数 schema 也有自己的限制,需要把它不认的键递归地从嵌套 schema 中清掉,否则请求会被拒。

- **`google_code_assist.py`**:Code Assist 的**控制面(control plane)**。控制面指「管理类」而非「推理类」的操作,这里具体包括项目发现(discover project)、用户入职(onboarding)、配额查询(quota)等——都是发推理请求之前/周边要办的手续。

- **`google_oauth.py`**:完整的 **OAuth PKCE 流程**实现。两个值得点出的工程细节:一是它会起一个**本地回调 HTTP 服务器**来接收 OAuth 授权码(浏览器授权后重定向回 localhost);二是做了**跨进程并发刷新去重**——当多个 Hermes 进程同时发现令牌过期、都想刷新时,用去重机制避免重复刷新打架(否则可能互相把对方刚拿到的新令牌作废)。

总的看,Gemini 这一支的复杂度主要来自 **Google 同时存在「原生 REST」和「Code Assist」两个后端、且都用 OAuth PKCE 这套较重的认证**,因此被单独抽成一组文件,而不是塞进第二节的默认路径里。
