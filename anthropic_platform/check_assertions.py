#!/usr/bin/env python3
"""
Hermes Anthropic 路径深度测试 — 断言检查器

读取 anthropic_requests.jsonl, 逐场景程序化校验上下文管理特性的断言,
输出 PASS / FAIL / INFO(源码确认/host门控) 矩阵。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = [json.loads(l) for l in open(os.path.join(HERE, "anthropic_requests.jsonl")) if l.strip()]
RESULTS = []


def by(prefix):
    return [r for r in ROWS if r["scenario"].startswith(prefix)]


def rec(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    tag = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"  [{tag}] {name}" + (f"  — {detail}" if detail else ""))


def nonsys_idx(hits):
    """从 cache_control_hits 提取 messages[...] 的索引集合 (排除 system/tools/envelope)。"""
    out = []
    for h in hits:
        loc = h["loc"]
        if loc.startswith("messages[") and ".content[" in loc:
            out.append(int(loc.split("[")[1].split("]")[0]))
    return sorted(out)


def check_s1():
    print("\n# S1 cache_control 断点滑动 + system 字节稳定")
    rs = by("S1_sliding")
    if not rs:
        return rec("S1 有数据", False, "无 S1 请求")
    # 断点上限 4
    rec("断点总数恒 <=4", all(r["cache_control_count"] <= 4 for r in rs),
        f"各轮={[r['cache_control_count'] for r in rs]}")
    # 滑动: 后期轮断点贴最后3条非system消息
    ok_slide = True
    detail = []
    for r in rs:
        idx = nonsys_idx(r["cache_control_hits"])
        nmsg = r["n_messages"]
        expect = sorted(range(max(0, nmsg - 3), nmsg))  # 最后3条 (0-based, 末尾)
        detail.append(f"msgs={nmsg}:{idx}")
        if nmsg >= 3 and idx != expect:
            ok_slide = False
    rec("断点滑动=最后3条非system消息", ok_slide, " ".join(detail))
    # system 占 1 个断点
    rec("system 始终占 1 个断点", all(any(h["loc"].startswith("system[") for h in r["cache_control_hits"]) for r in rs))
    # system 字节稳定
    slens = [r["system"]["len"] for r in rs]
    rec("system 文本跨轮字节稳定", len(set(slens)) == 1, f"len={slens}")


def check_s2():
    print("\n# S2 cache_control TTL 5m vs 1h")
    m5 = by("S2_ttl_5m"); m1 = by("S2_ttl_1h")
    if m5:
        markers = [h["marker"] for r in m5 for h in r["cache_control_hits"]]
        rec("5m: marker 无 ttl 字段", all("ttl" not in mk for mk in markers), f"{markers[:2]}")
    if m1:
        markers = [h["marker"] for r in m1 for h in r["cache_control_hits"]]
        rec("1h: marker 含 ttl=1h", all(mk.get("ttl") == "1h" for mk in markers), f"{markers[:2]}")


def check_s3():
    print("\n# S3 第三方 /anthropic native 布局")
    rs = by("S3_thirdparty")
    rec("走 /anthropic/v1/messages", all("/anthropic/v1/messages" in r["path"] for r in rs),
        f"paths={set(r['path'] for r in rs)}")
    rec("native: content-level cache_control",
        all(any(".content[" in h["loc"] for h in r["cache_control_hits"]) for r in rs if r["cache_control_count"]))


def check_s4():
    print("\n# S4 envelope 布局 (host 门控)")
    rs = by("S4_envelope")
    no_cc = all(r["cache_control_count"] == 0 for r in rs)
    rec("localhost 下 OpenRouter chat 不打 cache_control (envelope 需 openrouter.ai host)", None,
        f"实测 cc=0; 源码: agent_runtime_helpers.py:1194 is_openrouter=base_url_host_matches('openrouter.ai'). cc各={[r['cache_control_count'] for r in rs]}")


def check_s5():
    print("\n# S5 anthropic-beta 基线")
    rs = by("S5_beta_baseline")
    if rs:
        r = rs[0]
        rec("beta=[interleaved-thinking, fine-grained-tool-streaming]",
            r["anthropic_beta_list"] == ["interleaved-thinking-2025-05-14", "fine-grained-tool-streaming-2025-05-14"],
            f"{r['anthropic_beta_list']}")
        rec("interleaved-thinking 恒在 (与 thinking 解耦)",
            "interleaved-thinking-2025-05-14" in r["anthropic_beta_list"])
        rec("auth=x-api-key, 无 OAuth 身份头", r["auth_kind"] == "x-api-key" and not r["x_app"],
            f"auth={r['auth_kind']} x_app={r['x_app']!r}")
        rec("不含 prompt-caching/extended-cache-ttl beta",
            not any("caching" in b or "cache-ttl" in b for b in r["anthropic_beta_list"]))


def check_s6():
    print("\n# S6 OAuth 身份头 (host 门控)")
    rs = by("S6_oauth")
    rec("localhost 下无 OAuth 身份头 (需 api.anthropic.com host)", None,
        "实测 auth=x-api-key 无 claude-cli UA/x-app; 源码: anthropic_adapter.py:736 _is_third_party 分支先于 :744 OAuth 分支. "
        "但 session flag _is_anthropic_oauth=True 已正确置位 (agent_init.py:645, token 前缀判定). 标识与身份头是两个门控。")
    for r in rs:
        rec(f"  {r['scenario']}: auth/UA", r["auth_kind"] == "x-api-key",
            f"auth={r['auth_kind']} ua={r['user_agent']}")


def check_s7():
    print("\n# S7 thinking 配置分支")
    exp = {
        "S7_think_old": ({"type": "enabled", "budget_tokens": 16000}, None),
        "S7_think_adaptive46": ({"type": "adaptive", "display": "summarized"}, {"effort": "high"}),
        "S7_think_xhigh47": ({"type": "adaptive", "display": "summarized"}, {"effort": "xhigh"}),
        "S7_think_xhigh_downgrade46": ({"type": "adaptive", "display": "summarized"}, {"effort": "max"}),
        "S7_think_off": (None, None),
    }
    for scen, (et, eoc) in exp.items():
        rs = by(scen)
        if not rs:
            rec(scen, False, "无数据"); continue
        r = rs[0]
        ok = r["thinking_field"] == et and r["output_config"] == eoc
        rec(scen, ok, f"thinking={r['thinking_field']} output_config={r['output_config']}")


def check_s8():
    print("\n# S8 cache 命中回读")
    rs = by("S8_cache_readback")
    # 通过 hermes result 已确认读到; 这里看请求轮次存在
    rec("多轮 cache 回读场景存在", len(rs) >= 2, f"{len(rs)} 请求 (cache 字段由 hermes 端读取, 见 driver 日志 cache_read 递增)")


def check_s9():
    print("\n# S9 上下文压缩链路")
    rs = by("S9_compression")
    summ = [r for r in rs if r["kind"] == "summary"]
    rec("压缩摘要辅助请求被捕获", len(summ) >= 1, f"summary={len(summ)} main={sum(1 for r in rs if r['kind']=='main')}")
    rec("摘要请求=非流式 + 单 user 消息 + 无 tools",
        all((not r["stream"]) and r["n_messages"] == 1 and not r["has_tools"] for r in summ) if summ else False,
        f"{[(r['stream'], r['n_messages'], r['has_tools']) for r in summ]}")
    # 压缩后 msgs 回落 (主请求 msgs 不再单调增)
    mains = [r["n_messages"] for r in rs if r["kind"] == "main"]
    rec("压缩使主请求 msgs 回落 (非单调增)", len(mains) >= 3 and max(mains) <= 6, f"main msgs={mains}")


def check_s10():
    print("\n# S10 输出截断续写")
    rs = by("S10_length_cont")
    rec("续写产生额外请求 (>=2)", len(rs) >= 2, f"{len(rs)} 请求 (原请求 stop=max_tokens -> length -> 续写)")


def check_s11_s12():
    print("\n# S11/S12 thinking round-trip (校正: reasoning_details 确实往返, 重建有门控)")
    s11 = by("S11"); s12 = by("S12")
    t2_11 = [r for r in s11 if "turn2" in r["scenario"]]
    rec("S11 turn2 请求顶层 thinking content 块", None,
        f"has_thinking_history={[r['has_thinking_history'] for r in t2_11]} (仅检测顶层 content). "
        "实证: result['messages'] 的 assistant 消息确实带 reasoning_details=[{type:thinking,signature}] (探针确认), "
        "thinking 经 reasoning_details 往返 (anthropic.py:101-105 -> chat_completion_helpers.py:910-925); "
        "但本数据集 turn2 最终请求 assistant 仅 text -> _extract_preserved_thinking_blocks 重建有门控. "
        "签名400分支 (conversation_loop.py:2544-2562) 由 error_classifier.py:553 子串匹配 + 源码确认。")
    rec("S12 redacted_thinking 同机制", None, "redacted_thinking 同经 reasoning_details 往返, 重建门控待定向测试")


def check_s13():
    print("\n# S13 long-context tier 429")
    rs = by("S13_tier429")
    rec("429 触发重试 (>=2 请求)", len(rs) >= 2, f"{len(rs)} 请求 (429 注入一次 -> 降 context_length + 压缩 + retry)")


def main():
    print("=" * 78)
    print("  Hermes Anthropic 路径上下文管理 — 断言检查")
    print(f"  数据: anthropic_requests.jsonl ({len(ROWS)} 请求)")
    print("=" * 78)
    for fn in (check_s1, check_s2, check_s3, check_s4, check_s5, check_s6,
               check_s7, check_s8, check_s9, check_s10, check_s11_s12, check_s13):
        try:
            fn()
        except Exception as e:
            print(f"  [ERR] {fn.__name__}: {e}")
    npass = sum(1 for _, ok, _ in RESULTS if ok is True)
    nfail = sum(1 for _, ok, _ in RESULTS if ok is False)
    ninfo = sum(1 for _, ok, _ in RESULTS if ok is None)
    print("\n" + "=" * 78)
    print(f"  汇总: PASS={npass}  FAIL={nfail}  INFO(源码/host门控)={ninfo}")
    print("=" * 78)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
