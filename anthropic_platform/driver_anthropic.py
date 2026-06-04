#!/usr/bin/env python3
"""
Hermes 上下文管理深度测试平台 — Anthropic 路径 Driver

用真实 AIAgent.run_conversation() 驱动一组 Anthropic 路径场景, 每个场景前通过
mock 的 /__mock/control 端点下发响应行为 (usage/content/stop_reason/错误注入),
真实触发上下文管理特性, 由 mock_anthropic.py 捕获到 anthropic_requests.jsonl。

用法:
  1. python3 mock_anthropic.py 8910
  2. <hermes_venv_python> driver_anthropic.py [scenario_id...]   # 不带参数跑全部
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/home/niaowuuu/.hermes/hermes-agent")

HERMES_HOME = "/home/niaowuuu/.claude/jobs/15bd174c/tmp/hermes-anth"
os.makedirs(HERMES_HOME, exist_ok=True)
os.environ["HERMES_HOME"] = HERMES_HOME
for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY"):
    os.environ.pop(k, None)

MOCK = "http://127.0.0.1:8910"
from run_agent import AIAgent  # noqa: E402


def control(**cfg):
    data = json.dumps(cfg).encode()
    req = urllib.request.Request(MOCK + "/__mock/control", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def snapshots():
    with urllib.request.urlopen(MOCK + "/__mock/snapshots", timeout=5) as r:
        return json.loads(r.read())["snapshots"]


def banner(t):
    print(f"\n{'='*80}\n  {t}\n{'='*80}", flush=True)


def build_agent(*, model="claude-sonnet-4-20250514", provider="anthropic",
                base_url=MOCK, api_mode="anthropic_messages", api_key="sk-ant-mock",
                reasoning_config=None, cache_ttl=None, max_iterations=2,
                threshold_tokens=None, protect_last_n=None, protect_first_n=None,
                context_length=None):
    a = AIAgent(model=model, provider=provider, base_url=base_url, api_key=api_key,
                api_mode=api_mode, enabled_toolsets=[], skip_memory=True,
                skip_context_files=True, load_soul_identity=False, quiet_mode=True,
                max_iterations=max_iterations, reasoning_config=reasoning_config)
    if cache_ttl is not None:
        a._cache_ttl = cache_ttl
    print(f"  provider={a.provider} api_mode={a.api_mode} model={a.model} "
          f"cache={getattr(a,'_use_prompt_caching','?')} native={getattr(a,'_use_native_cache_layout','?')} "
          f"ttl={getattr(a,'_cache_ttl','?')}", flush=True)
    if threshold_tokens is not None or context_length is not None:
        for attr in ("context_compressor", "_context_engine"):
            comp = getattr(a, attr, None)
            if comp is not None:
                try:
                    if context_length is not None:
                        comp.context_length = context_length
                    if threshold_tokens is not None:
                        comp.threshold_tokens = threshold_tokens
                    if protect_last_n is not None:
                        comp.protect_last_n = protect_last_n
                    if protect_first_n is not None:
                        comp.protect_first_n = protect_first_n
                    print(f"  [override] {attr}.context_length={getattr(comp,'context_length','?')} "
                          f"threshold_tokens={getattr(comp,'threshold_tokens','?')}", flush=True)
                except Exception as e:
                    print(f"  [override err] {e}", flush=True)
    return a


def converse(a, turns, *, big=False):
    history = None
    filler = (" lorem ipsum dolor sit amet" * 400) if big else ""
    for t in range(turns):
        try:
            r = a.run_conversation(f"Turn {t+1}: please acknowledge.{filler}",
                                   conversation_history=history)
            history = r.get("messages") if isinstance(r, dict) else None
            pt = r.get("prompt_tokens") if isinstance(r, dict) else "?"
            cr = r.get("session_cache_read_tokens", r.get("cache_read_tokens", "?")) if isinstance(r, dict) else "?"
            print(f"  turn {t+1}: msgs={len(history) if history else 0} prompt_tokens={pt} cache_read={cr}", flush=True)
        except Exception as e:
            print(f"  turn {t+1} [异常] {type(e).__name__}: {str(e)[:140]}", flush=True)
            break
    return history


# ───────────────────────── 场景 ─────────────────────────
def s1_breakpoint_sliding():
    banner("S1. cache_control 断点滑动 (native, 5 轮) + system 字节稳定")
    control(scenario="S1_sliding", content=["text"], stop_reason="end_turn",
            usage_mode="auto", cache_turns=True, reset=False)
    a = build_agent(); converse(a, 5); a.close()


def s2_ttl():
    banner("S2. cache_control TTL: 5m(默认) vs 1h")
    control(scenario="S2_ttl_5m", content=["text"], usage_mode="auto")
    a = build_agent(cache_ttl="5m"); converse(a, 1); a.close()
    control(scenario="S2_ttl_1h")
    a = build_agent(cache_ttl="1h"); converse(a, 1); a.close()


def s3_thirdparty_native():
    banner("S3. 第三方 /anthropic 网关 (native 布局, model 含 claude)")
    control(scenario="S3_thirdparty_native")
    a = build_agent(provider="custom", base_url=MOCK + "/anthropic",
                    api_mode="anthropic_messages", model="claude-sonnet-4-20250514")
    converse(a, 2); a.close()


def s4_envelope():
    banner("S4. envelope 布局 (provider=openrouter + claude -> chat_completions message-level cache_control)")
    os.environ["OPENROUTER_API_KEY"] = "sk-or-mock-xyz"
    control(scenario="S4_envelope")
    a = build_agent(provider="openrouter", base_url=MOCK + "/v1",
                    api_mode="chat_completions", model="anthropic/claude-sonnet-4",
                    api_key="sk-or-mock-xyz")
    converse(a, 2); a.close()
    os.environ.pop("OPENROUTER_API_KEY", None)


def s5_beta_baseline():
    banner("S5. anthropic-beta 基线 (native api-key) + interleaved-thinking 恒在")
    control(scenario="S5_beta_baseline")
    a = build_agent(); converse(a, 1); a.close()


def s6_oauth_identity():
    banner("S6. OAuth 身份头 (provider=anthropic + oauth token 前缀)")
    for tok, tag in [("sk-ant-oat01-MOCKTOKEN", "oat"), ("cc-MOCKTOKEN", "cc")]:
        control(scenario=f"S6_oauth_{tag}")
        a = build_agent(api_key=tok); converse(a, 1); a.close()


def s7_thinking_budget():
    banner("S7. thinking 配置: 老模型手动 budget vs 新模型 adaptive")
    control(scenario="S7_think_old", content=["text"])
    a = build_agent(model="claude-3-7-sonnet-20250219",
                    reasoning_config={"effort": "high", "enabled": True})
    converse(a, 1); a.close()
    control(scenario="S7_think_adaptive46")  # 4.6 -> adaptive + output_config.effort
    a = build_agent(model="claude-opus-4-6",
                    reasoning_config={"effort": "high", "enabled": True})
    converse(a, 1); a.close()
    control(scenario="S7_think_xhigh47")  # 4.7 + xhigh 保持; 4.6 + xhigh 应降级为 max
    a = build_agent(model="claude-opus-4-7",
                    reasoning_config={"effort": "xhigh", "enabled": True})
    converse(a, 1); a.close()
    control(scenario="S7_think_xhigh_downgrade46")
    a = build_agent(model="claude-opus-4-6",
                    reasoning_config={"effort": "xhigh", "enabled": True})
    converse(a, 1); a.close()
    control(scenario="S7_think_off")
    a = build_agent(model="claude-sonnet-4-20250514",
                    reasoning_config={"enabled": False})
    converse(a, 1); a.close()


def s8_cache_readback():
    banner("S8. cache 命中回读 (cache_turns: 首轮 creation, 后续 read)")
    control(scenario="S8_cache_readback", cache_turns=True, usage_mode="auto",
            force_prompt_tokens=None, reset=False)
    a = build_agent(); converse(a, 3); a.close()


def s9_compression():
    banner("S9. 上下文压缩链路 (第三方 anthropic-wire, 摘要辅助请求留在 mock)")
    # 用第三方 anthropic-wire (custom provider) 而非 native: native 路径压缩摘要的
    # 辅助 client 会逃逸到真实 api.anthropic.com (不继承 mock base_url, 已实测 401),
    # 而 custom provider 无"真实 Anthropic"默认, 摘要请求会留在 localhost mock。
    control(scenario="S9_compression", usage_mode="auto", force_prompt_tokens=90000,
            cache_turns=False)
    a = build_agent(provider="custom", base_url=MOCK + "/anthropic",
                    api_mode="anthropic_messages", model="claude-sonnet-4-20250514",
                    context_length=8000, threshold_tokens=4000,
                    protect_last_n=2, protect_first_n=1)
    converse(a, 5, big=True); a.close()


def s10_length_continuation():
    banner("S10. 输出截断续写 (stop_reason_once=max_tokens -> length)")
    control(scenario="S10_length_cont", content=["text"], stop_reason_once="max_tokens")
    a = build_agent(max_iterations=5); converse(a, 1); a.close()


def s11_signature_retry():
    banner("S11. thinking 签名 400 清空重试")
    # turn1: 让响应带 thinking 块, 使 turn2 历史含 thinking
    control(scenario="S11_sig_turn1", content=["thinking", "text"])
    a = build_agent(model="claude-3-7-sonnet-20250219",
                    reasoning_config={"effort": "high", "enabled": True}, max_iterations=3)
    h = converse(a, 1)
    # turn2: 注入签名 400 一次 (body 含 thinking 时), 期望 Hermes 清空 reasoning 重试
    control(scenario="S11_sig_turn2", content=["text"], inject_signature_400_once=True)
    try:
        a.run_conversation("Turn 2: continue.", conversation_history=h)
        print("  turn2 完成 (应已清空 thinking 重试)", flush=True)
    except Exception as e:
        print(f"  turn2 [异常] {type(e).__name__}: {str(e)[:140]}", flush=True)
    a.close()


def s12_redacted_thinking():
    banner("S12. redacted_thinking 块 round-trip")
    control(scenario="S12_redacted", content=["redacted_thinking", "text"])
    a = build_agent(model="claude-3-7-sonnet-20250219",
                    reasoning_config={"effort": "high", "enabled": True}, max_iterations=3)
    h = converse(a, 1)
    control(scenario="S12_redacted_t2", content=["text"])
    try:
        a.run_conversation("Turn 2: continue.", conversation_history=h)
        print("  turn2 完成", flush=True)
    except Exception as e:
        print(f"  turn2 [异常] {type(e).__name__}: {str(e)[:140]}", flush=True)
    a.close()


def s13_tier_429():
    banner("S13. long-context tier 429 -> 降 context_length + 压缩重试 (第三方路径避免逃逸)")
    control(scenario="S13_tier429", inject_tier_429_once=True, usage_mode="auto")
    a = build_agent(provider="custom", base_url=MOCK + "/anthropic",
                    api_mode="anthropic_messages", model="claude-sonnet-4-20250514",
                    max_iterations=3); converse(a, 1); a.close()


SCENARIOS = {
    "s1": s1_breakpoint_sliding, "s2": s2_ttl, "s3": s3_thirdparty_native,
    "s4": s4_envelope, "s5": s5_beta_baseline, "s6": s6_oauth_identity,
    "s7": s7_thinking_budget, "s8": s8_cache_readback, "s9": s9_compression,
    "s10": s10_length_continuation, "s11": s11_signature_retry,
    "s12": s12_redacted_thinking, "s13": s13_tier_429,
}


def main():
    which = [x.lower() for x in sys.argv[1:]] or list(SCENARIOS)
    for key in which:
        fn = SCENARIOS.get(key)
        if fn:
            try:
                fn()
            except Exception as e:
                print(f"  [场景 {key} 异常] {type(e).__name__}: {str(e)[:200]}", flush=True)
    print(f"\n{'='*80}\n  完成. 日志: anthropic_platform/anthropic_requests.jsonl\n{'='*80}", flush=True)


if __name__ == "__main__":
    main()
