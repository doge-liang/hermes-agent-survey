#!/usr/bin/env python3
"""
从真实 CLI 探测捕获生成**后端无关 fixtures** — 真实后端测试台的输入。

每个 fixture = {场景, 映射用例, 触发(config+CLI命令), 期望 agent-hint 签名(带容差), 状态}。
真实后端阶段: 把同一 CLI 命令指向真实后端, 用这些签名+容差对照实际请求。
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = [json.loads(l) for l in open(os.path.join(HERE, "cli_requests.jsonl")) if l.strip()]

# 动态字段 (真实后端会变, 用模式匹配而非精确值)
SID_PAT = r"^\d{8}_\d{6}_[0-9a-f]{6}$"

INTENT = {
    "RC-01": "custom chat_completions 基线: 验证 CLI 入口的全量 tools + system prompt (import-driver 盲区)",
    "RC-02": "第三方 anthropic-wire native cache_control 断点滑动 (真实多轮 --resume)",
    "RC-03": "cache_ttl 1h marker (config prompt_caching.cache_ttl)",
    "RC-04a": "Claude 4.7 + xhigh → adaptive thinking output_config.effort=xhigh",
    "RC-04b": "Claude 4.6 + xhigh → 版本感知降级 output_config.effort=max",
    "RC-04c": "Claude 3.7 + high → manual budget thinking budget_tokens=16000",
    "RC-05": "codex_responses(custom): store=false + prompt_cache_key + instructions, 无 codex header",
    "RC-06": "OpenRouter body.session_id + x-grok-conv-id (host 门控)",
    "RC-07": "OpenRouter reasoning extra_body + envelope cache 布局 (双 host 门控)",
    "RC-08": "第三方 anthropic-wire 上下文压缩 (preflight + summary 辅助 + compaction)",
    "RC-09": "long-context tier 429 → 降 context_length + 压缩 + retry",
    "RC-10": "输出截断 stop_reason=max_tokens → length 续写循环",
}


def sig(reqs):
    """从一组同场景请求提取后端无关签名。"""
    if not reqs:
        return None
    main = [r for r in reqs if r.get("kind") == "main"] or reqs
    b0 = main[0]["body"]
    s = {
        "request_count": len(reqs),
        "paths": sorted({r["path"] for r in reqs}),
        "tools_count_ge": 20 if len(b0.get("tools", [])) >= 20 else len(b0.get("tools", [])),
        "anthropic_beta": main[0].get("anthropic_beta_list") or None,
    }
    # 会话亲和
    if "session_id" in b0:
        s["body.session_id"] = {"present": True, "pattern": SID_PAT, "constant_across_turns": True}
    else:
        s["body.session_id"] = {"present": False}
    if "prompt_cache_key" in b0:
        s["body.prompt_cache_key"] = {"present": True, "pattern": SID_PAT}
    if b0.get("store") is not None:
        s["body.store"] = b0["store"]
    # thinking
    if b0.get("thinking"):
        s["body.thinking"] = b0["thinking"]
    if b0.get("output_config"):
        s["body.output_config"] = b0["output_config"]
    if b0.get("reasoning"):
        s["body.reasoning"] = b0["reasoning"]
    if "instructions" in b0:
        s["body.instructions_present"] = True
        s["body.messages_absent"] = "messages" not in b0
    # cache_control
    markers, locs = [], []
    for r in main:
        bb = r["body"]
        sysv = bb.get("system")
        if isinstance(sysv, list):
            for i, x in enumerate(sysv):
                if isinstance(x, dict) and "cache_control" in x:
                    markers.append(x["cache_control"]); locs.append(f"system[{i}]")
        for mi, m in enumerate(bb.get("messages", []) or []):
            c = m.get("content")
            if isinstance(c, list):
                for ci, x in enumerate(c):
                    if isinstance(x, dict) and "cache_control" in x:
                        markers.append(x["cache_control"]); locs.append(f"messages[{mi}].content[{ci}]")
    if markers:
        uniq = {json.dumps(m, sort_keys=True) for m in markers}
        s["cache_control"] = {
            "layout": "native(content-level)" if any(".content[" in l for l in locs) else "envelope",
            "marker_forms": [json.loads(u) for u in uniq],
            "max_breakpoints_per_request": max(len([1 for x in (r["body"].get("messages") or []) for c in [x.get("content")] if isinstance(c, list) for bl in c if isinstance(bl, dict) and "cache_control" in bl]) for r in main) if main else 0,
        }
    # 多轮 msgs
    s["main_n_messages_sequence"] = [r["n_messages"] for r in main]
    # summary
    summ = [r for r in reqs if r.get("kind") == "summary"]
    if summ:
        s["compression_summary"] = {"present": True, "stream": summ[0]["stream"], "n_messages": summ[0]["n_messages"]}
    return s


def main():
    fixtures = []
    seen = []
    for r in ROWS:
        sc = r.get("scenario")
        if sc and sc not in seen:
            seen.append(sc)
    # 也纳入 host-gated (无捕获)
    for sc in ["RC-01", "RC-02", "RC-03", "RC-04a", "RC-04b", "RC-04c", "RC-05",
               "RC-06", "RC-07", "RC-08", "RC-09", "RC-10"]:
        reqs = [r for r in ROWS if r.get("scenario") == sc]
        host_gated = sc in ("RC-06", "RC-07")
        fx = {
            "id": sc,
            "intent": INTENT.get(sc, ""),
            "status": "host_gated_deferred" if host_gated else ("captured" if reqs else "no_capture"),
            "driver_ref": "cli_platform/driver_cli.py (SCENARIOS[id=%s]: config_yaml + hermes chat 命令)" % sc,
            "expected_signature": sig(reqs) if reqs else "见源码确认 — 真实后端阶段验证 (openrouter profile 需 host==openrouter.ai)",
            "tolerances": [
                "body.session_id / prompt_cache_key / x-grok-conv-id: 动态值, 用 pattern " + SID_PAT + " 匹配, 校验跨轮恒定",
                "headers.x-stainless-* / user-agent / content-length: 忽略",
                "system prompt 文本: 校验长度量级与跨轮字节稳定, 不逐字比对 (含日期等)",
                "tools 集合: 校验 count 与关键工具子集, 不锁定完整 28/29 (版本会变)",
            ],
        }
        fixtures.append(fx)
    out = {
        "platform": "hermes real-CLI + mock probe",
        "hermes_version": "0.15.1",
        "purpose": "真实后端测试台输入: 同一 hermes chat 命令指向真实后端时, 用这些 agent-hint 签名+容差对照实际请求",
        "fixtures": fixtures,
    }
    path = os.path.join(HERE, "fixtures.json")
    open(path, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"写出 {len(fixtures)} 个 fixtures -> {path}")
    for fx in fixtures:
        print(f"  {fx['id']:7s} [{fx['status']:18s}] {fx['intent'][:54]}")


if __name__ == "__main__":
    main()
