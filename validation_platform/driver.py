#!/usr/bin/env python3
"""
Hermes 上下文管理验证平台 — Driver (v2)

用 Hermes 真实的 AIAgent.run_conversation() 驱动多个场景，真实触发上下文管理特性。
所有请求由 mock_backend.py 捕获到 requests.jsonl。

v2 改进:
  - 多轮对话用 conversation_history 累积 (上一轮返回的 messages 传回下一轮)
  - 用 provider='anthropic' 真实触发 cache_control(native) + thinking budget
  - 用 base_url 以 /anthropic 结尾真实触发第三方 anthropic-wire(envelope)
  - 压缩场景: 累积历史 + 大 prompt_tokens + 小 context_length

用法:
  1. python3 mock_backend.py 8900
  2. <hermes_venv_python> driver.py
"""
import os
import sys
import uuid

# 用用户真实安装 (v0.15.1), 不是源码树 — 验证真实行为必须用实际运行的版本
sys.path.insert(0, "/home/niaowuuu/.hermes/hermes-agent")

HERMES_HOME = "/tmp/hermes-validation"
os.makedirs(HERMES_HOME, exist_ok=True)
os.environ["HERMES_HOME"] = HERMES_HOME
for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY"):
    os.environ.pop(k, None)
os.environ["HERMES_SESSION_SOURCE"] = "validation"

MOCK_HOST = "http://127.0.0.1:8900"
MOCK = MOCK_HOST + "/v1"

from run_agent import AIAgent


def banner(title):
    print(f"\n{'='*78}\n  场景: {title}\n{'='*78}", flush=True)


def build_agent(*, model, provider, base_url, api_mode=None, reasoning_config=None,
                context_length=None, protect_last_n=None, protect_first_n=None):
    a = AIAgent(
        model=model, provider=provider, base_url=base_url,
        api_key="sk-mock-" + uuid.uuid4().hex[:6], api_mode=api_mode,
        enabled_toolsets=[], skip_memory=True, skip_context_files=True,
        load_soul_identity=False, quiet_mode=True, max_iterations=2,
        reasoning_config=reasoning_config,
    )
    print(f"  provider={a.provider} api_mode={a.api_mode} model={a.model}", flush=True)
    print(f"  _use_prompt_caching={getattr(a,'_use_prompt_caching','?')} "
          f"native_layout={getattr(a,'_use_native_cache_layout','?')} "
          f"cache_ttl={getattr(a,'_cache_ttl','?')}", flush=True)
    print(f"  compression_enabled={getattr(a,'compression_enabled','?')}", flush=True)
    if context_length is not None:
        for attr in ("context_compressor", "_context_engine"):
            comp = getattr(a, attr, None)
            if comp is not None:
                try:
                    comp.context_length = context_length
                    tp = getattr(comp, "threshold_percent", 0.5)
                    comp.threshold_tokens = int(context_length * tp)
                    if protect_last_n is not None:
                        comp.protect_last_n = protect_last_n
                    if protect_first_n is not None:
                        comp.protect_first_n = protect_first_n
                    print(f"  [override] {attr}.context_length={comp.context_length} "
                          f"threshold_tokens={comp.threshold_tokens} "
                          f"protect_first_n={getattr(comp,'protect_first_n','?')} "
                          f"protect_last_n={getattr(comp,'protect_last_n','?')}", flush=True)
                except Exception as e:
                    print(f"  [override err] {e}", flush=True)
    return a


def converse_multi(a, turns, prompt_tokens=None, big_content=False):
    """多轮对话，累积 conversation_history。big_content=True 时每轮发大消息以堆高 token。"""
    if prompt_tokens is not None:
        os.environ["MOCK_PROMPT_TOKENS"] = str(prompt_tokens)
    else:
        os.environ.pop("MOCK_PROMPT_TOKENS", None)
    history = None
    filler = (" lorem ipsum dolor sit amet" * 350) if big_content else ""
    for t in range(turns):
        try:
            result = a.run_conversation(
                f"This is turn {t+1}. Please acknowledge briefly.{filler}",
                conversation_history=history,
            )
            history = result.get("messages") if isinstance(result, dict) else None
            nmsg = len(history) if history else 0
            ptoks = result.get("prompt_tokens") if isinstance(result, dict) else "?"
            print(f"  turn {t+1}: history_len={nmsg} prompt_tokens={ptoks} "
                  f"session={result.get('session_id') if isinstance(result,dict) else '?'}", flush=True)
        except Exception as e:
            print(f"  turn {t+1} [异常] {type(e).__name__}: {str(e)[:160]}", flush=True)
            break


def main():
    # ── A. 默认 chat_completions 基线 ──
    banner("A. 默认 custom provider (chat_completions 基线)")
    a = build_agent(model="mock-model", provider="custom", base_url=MOCK)
    converse_multi(a, 1)
    a.close()

    # ── B. native Anthropic → cache_control(native) + anthropic-beta ──
    banner("B. provider=anthropic (触发 cache_control native + anthropic-beta header)")
    a = build_agent(model="claude-sonnet-4-20250514", provider="anthropic",
                    base_url=MOCK_HOST, api_mode="anthropic_messages")
    converse_multi(a, 1)
    a.close()

    # ── C. native Anthropic 多轮 → cache_control 断点随轮次移动 ──
    banner("C. provider=anthropic 多轮 (观察 cache_control 断点位置移动)")
    a = build_agent(model="claude-sonnet-4-20250514", provider="anthropic",
                    base_url=MOCK_HOST, api_mode="anthropic_messages")
    converse_multi(a, 4)
    a.close()

    # ── D. native Anthropic + reasoning high → thinking budget 注入 ──
    banner("D. provider=anthropic + reasoning effort=high (观察 thinking budget)")
    a = build_agent(model="claude-sonnet-4-20250514", provider="anthropic",
                    base_url=MOCK_HOST, api_mode="anthropic_messages",
                    reasoning_config={"effort": "high", "enabled": True})
    converse_multi(a, 1)
    a.close()

    # ── E. 第三方 anthropic-wire (base_url 以 /anthropic 结尾) ──
    banner("E. base_url 以 /anthropic 结尾 (第三方 anthropic-wire cache_control)")
    a = build_agent(model="claude-sonnet-4-20250514", provider="custom",
                    base_url=MOCK_HOST + "/anthropic")
    converse_multi(a, 1)
    a.close()

    # ── F. 触发上下文压缩 (累积历史 + 大 content + 小 ctx + 小 protect 区) ──
    banner("F. 触发上下文压缩 (观察摘要辅助请求 + 压缩后主请求)")
    a = build_agent(model="mock-model", provider="custom", base_url=MOCK,
                    context_length=8000, protect_last_n=2, protect_first_n=1)
    converse_multi(a, 8, big_content=True)
    a.close()

    # ── G. OpenRouter 路径 → body.session_id 注入 (v0.15.1, 全模型) ──
    # 升级到 v0.15.1 后, OpenRouterProfile.build_extra_body 对所有模型注入 body.session_id;
    # grok 模型额外带 x-grok-conv-id header。需要 OPENROUTER_API_KEY 才会走 openrouter profile。
    os.environ["OPENROUTER_API_KEY"] = "sk-or-mock-" + uuid.uuid4().hex[:8]
    for or_model in ("qwen/qwen-2.5-72b-instruct", "x-ai/grok-4"):
        banner(f"G. provider=openrouter model={or_model} (观察 body.session_id)")
        a = build_agent(model=or_model, provider="openrouter", base_url=MOCK)
        converse_multi(a, 1)
        a.close()

    # ════════ OpenAI SDK 路径增强场景 (codex_responses + reasoning) ════════
    # 需要 mock_backend 的 /responses 支持 Responses API SSE 事件流。

    # ── H. codex_responses body 特征 (api_mode override, custom provider) ──
    # 观察: store=False + prompt_cache_key(=session_id) + reasoning + include + instructions字段。
    # custom provider 下 is_codex_backend=False, 故无 session_id/x-client-request-id header。
    banner("H. api_mode=codex_responses (custom) — store/prompt_cache_key/reasoning/instructions")
    a = build_agent(model="gpt-5", provider="custom", base_url=MOCK, api_mode="codex_responses")
    converse_multi(a, 1)
    a.close()

    # ── I. 真正的 openai-codex provider → session_id + x-client-request-id header ──
    # is_codex_backend = (provider=="openai-codex" or chatgpt.com/backend-api/codex);
    # 触发后 codex transport 注入 session_id + x-client-request-id header (与 prompt_cache_key 同值)。
    # 传入 api_key 即可绕过该 provider 的 oauth_external 流程指向 mock。
    banner("I. provider=openai-codex — session_id/x-client-request-id header + prompt_cache_key (三者同值)")
    a = build_agent(model="gpt-5", provider="openai-codex", base_url=MOCK)
    converse_multi(a, 1)
    a.close()

    # ── J. chat_completions + reasoning → extra_body.reasoning ──
    # OpenRouter profile: supports_reasoning 时把 reasoning_config 整体放入 extra_body.reasoning。
    # mock 模型不在 hermes model_metadata 内, 强制 _supports_reasoning_extra_body 以观察注入。
    banner("J. provider=openrouter + reasoning effort=high — extra_body.reasoning")
    a = build_agent(model="deepseek/deepseek-r1", provider="openrouter", base_url=MOCK,
                    reasoning_config={"effort": "high", "enabled": True})
    try:
        a._supports_reasoning_extra_body = lambda: True
    except Exception as e:
        print(f"  [force reasoning err] {e}", flush=True)
    converse_multi(a, 1)
    a.close()

    print(f"\n{'='*78}\n  全部场景完成. 日志: validation_platform/requests.jsonl\n{'='*78}", flush=True)


if __name__ == "__main__":
    main()
