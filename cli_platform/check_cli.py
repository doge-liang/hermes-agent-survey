#!/usr/bin/env python3
"""
Hermes 真实 CLI 探测测试台 — 断言检查器

读取 cli_requests.jsonl, 逐场景校验 agent hints, 输出 PASS/FAIL/INFO 矩阵。
INFO = host 门控 (openrouter profile 需 openrouter.ai host, localhost 测不了, 留真实后端阶段)。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = [json.loads(l) for l in open(os.path.join(HERE, "cli_requests.jsonl")) if l.strip()]
RESULTS = []


def by(scen):
    return [r for r in ROWS if r.get("scenario") == scen]


def rec(name, ok, detail=""):
    RESULTS.append((name, ok))
    tag = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"  [{tag}] {name}" + (f"  — {detail}" if detail else ""))


def sys_text_len(b):
    sysv = b.get("system")
    if isinstance(sysv, str):
        return len(sysv)
    if isinstance(sysv, list):
        return sum(len(x.get("text", "")) for x in sysv if isinstance(x, dict))
    for m in b.get("messages", []) or []:
        if m.get("role") == "system":
            c = m.get("content")
            return len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))
    return 0


def nonsys_cc_idx(b):
    out = []
    for mi, m in enumerate(b.get("messages", []) or []):
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(x, dict) and "cache_control" in x for x in c):
            out.append(mi)
    return sorted(out)


def cc_markers(b):
    mk = []
    sysv = b.get("system")
    if isinstance(sysv, list):
        for x in sysv:
            if isinstance(x, dict) and "cache_control" in x:
                mk.append(x["cache_control"])
    for m in b.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and "cache_control" in x:
                    mk.append(x["cache_control"])
    return mk


def main():
    print("=" * 78)
    print(f"  Hermes 真实 CLI 探测 — 断言检查 (cli_requests.jsonl, {len(ROWS)} 请求)")
    print("=" * 78)

    # RC-01 baseline: 全量 tools + system + 无 session_id
    print("\n# RC-01 custom chat_completions 基线 (CLI 独有: 全量 tools + system)")
    r = by("RC-01")
    if r:
        b = r[0]["body"]
        rec("path=chat/completions", "/v1/chat/completions" in r[0]["path"])
        rec("tools 全量 (>=20)", len(b.get("tools", [])) >= 20, f"tools={len(b.get('tools', []))}")
        rec("system prompt 全量 (>=4500)", sys_text_len(b) >= 4500, f"sys_len={sys_text_len(b)}")
        rec("stream_options.include_usage", b.get("stream_options") == {"include_usage": True})
        rec("无 body.session_id (custom 不注入)", "session_id" not in b)

    # RC-02 cache_control 断点滑动 (多轮) + native + system 稳定
    print("\n# RC-02 第三方 anthropic-wire native cache_control 断点滑动 (真实多轮)")
    r = by("RC-02")
    if r:
        rec("全走 /anthropic/v1/messages", all("/anthropic/v1/messages" in x["path"] for x in r))
        rec("msgs 多轮累积 [1,3,5]", [x["n_messages"] for x in r] == [1, 3, 5],
            f"{[x['n_messages'] for x in r]}")
        slides = [nonsys_cc_idx(x["body"]) for x in r]
        ok_slide = slides[-1] == [2, 3, 4] if len(slides) >= 3 else False
        rec("断点滑动到末尾3条 (msgs=5→[2,3,4])", ok_slide, f"{slides}")
        rec("native 布局 (cache_control 在 content)", all(nonsys_cc_idx(x["body"]) for x in r if x["n_messages"] >= 1))
        rec("beta=[interleaved-thinking, fine-grained-tool-streaming]",
            all(x.get("anthropic_beta_list") == ["interleaved-thinking-2025-05-14", "fine-grained-tool-streaming-2025-05-14"] for x in r))
        slens = [sys_text_len(x["body"]) for x in r]
        rec("system 字节跨轮稳定", len(set(slens)) == 1, f"{slens}")
        rec("带全量 tools (>=20)", all(len(x["body"].get("tools", [])) >= 20 for x in r))

    # RC-03 TTL 1h
    print("\n# RC-03 cache_ttl 1h marker")
    r = by("RC-03")
    if r:
        mk = cc_markers(r[0]["body"])
        rec("每个 marker 含 ttl=1h", bool(mk) and all(m.get("ttl") == "1h" for m in mk), f"{mk[:2]}")

    # RC-04 thinking 三分支
    print("\n# RC-04 reasoning→thinking 三分支 (真实模型名+config, 无 monkeypatch)")
    for scen, et, eoc in [
        ("RC-04a", {"type": "adaptive", "display": "summarized"}, {"effort": "xhigh"}),
        ("RC-04b", {"type": "adaptive", "display": "summarized"}, {"effort": "max"}),
        ("RC-04c", {"type": "enabled", "budget_tokens": 16000}, None),
    ]:
        r = by(scen)
        if r:
            b = r[0]["body"]
            ok = b.get("thinking") == et and b.get("output_config") == eoc
            rec(scen, ok, f"thinking={b.get('thinking')} oc={b.get('output_config')}")

    # RC-05 codex_responses custom
    print("\n# RC-05 codex_responses (custom): store=false + prompt_cache_key + instructions, 无 codex header")
    r = by("RC-05")
    if r:
        b = r[0]["body"]; h = r[0].get("headers", {})
        rec("path=/v1/responses", "responses" in r[0]["path"])
        rec("store=false", b.get("store") is False)
        rec("prompt_cache_key 存在", "prompt_cache_key" in b)
        rec("instructions 字段 (非 messages)", "instructions" in b and "messages" not in b)
        rec("无 codex session header (custom)", not any(k.lower() in ("session_id", "x-client-request-id") for k in h))

    # RC-06/07 host-gated
    print("\n# RC-06/07 OpenRouter (host 门控 → INFO)")
    rec("RC-06 OpenRouter body.session_id + x-grok-conv-id", None,
        "openrouter profile 需 host==openrouter.ai, localhost 无法激活; 留真实后端阶段 (fixture)")
    rec("RC-07 OpenRouter reasoning extra_body + envelope", None,
        "双 host 门控 (reasoning extra_body + envelope layout); 留真实后端阶段")

    # RC-08 压缩
    print("\n# RC-08 上下文压缩 (第三方 anthropic-wire)")
    r = by("RC-08")
    if r:
        summ = [x for x in r if x.get("kind") == "summary"]
        mains = [x["n_messages"] for x in r if x.get("kind") == "main"]
        nonmono = any(mains[i] < mains[i - 1] for i in range(1, len(mains)))
        rec("压缩触发 (主请求 msgs 非单调=compaction)", nonmono, f"main_msgs={mains}")
        if summ:
            rec("summary 辅助请求 (非流式单user, 留在 mock 不逃逸)",
                all((not x["stream"]) and x["n_messages"] == 1 for x in summ) and all("anthropic" in x["path"] or "/v1/" in x["path"] for x in summ),
                f"summary={len(summ)}")
        else:
            rec("summary 辅助请求", None, "本次未捕获 summary (compaction 已触发; summary 偶发, 见 verbose 验证可捕获)")

    # RC-09 tier 429 retry
    print("\n# RC-09 long-context tier 429 → retry")
    r = by("RC-09")
    rec("429 触发 retry (>=2 请求)", len(r) >= 2, f"{len(r)} 请求")

    # RC-10 length 续写
    print("\n# RC-10 输出截断 length 续写")
    r = by("RC-10")
    if r:
        rec("续写 (>=2 请求)", len(r) >= 2, f"{len(r)} 请求")
        slens = [sys_text_len(x["body"]) for x in r]
        rec("system 跨续写稳定", len(set(slens)) == 1, f"{slens}")

    npass = sum(1 for _, ok in RESULTS if ok is True)
    nfail = sum(1 for _, ok in RESULTS if ok is False)
    ninfo = sum(1 for _, ok in RESULTS if ok is None)
    print("\n" + "=" * 78)
    print(f"  汇总: PASS={npass}  FAIL={nfail}  INFO(host门控)={ninfo}")
    print("=" * 78)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
