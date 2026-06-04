#!/usr/bin/env python3
"""
Hermes 上下文管理深度测试平台 — 增强版 Mock Anthropic Messages 后端

相比 validation_platform/mock_backend.py 的基础 Anthropic SSE, 本 mock 把
usage / cache / stop_reason / content-block / 错误注入 全部做成"按场景可编程",
由 driver 通过 *控制端点* 在每个场景前下发配置。

核心能力:
  - 协议正确的 Anthropic SSE (message_start/content_block_*/message_delta/message_stop),
    真实 anthropic SDK 0.87.0 可经 stream.get_final_message() 聚合。
  - 可编程 usage: message_start.usage 给 input/cache_read/cache_creation,
    message_delta.usage 给 output。prompt_tokens = 三者之和 (Hermes normalize_usage)。
  - 可编程 content blocks: text / thinking(+signature) / redacted_thinking(+data) / tool_use。
  - 可编程 stop_reason: end_turn(默认) / max_tokens(触发 length 续写)。
  - 内容门控的错误注入: 命中 error_classifier 精确子串
      * thinking 签名 400  (body 含 thinking 块时触发, message 含 'signature'+'thinking')
      * OAuth 1M beta 400  (anthropic-beta 含 context-1m 时触发, 'long context beta'+'not yet available')
      * long-context 429   (下一请求, 'extra usage'+'long context')
      * 通用 error_once     (status + message 直配)
  - 完整请求录制 (headers 脱敏 + 最终 body) 到 anthropic_requests.jsonl, 供断点/字节稳定断言。

控制端点:
  POST /__mock/control   {... 见 DEFAULT_CONFIG ...}   # 设置后续请求行为, reset=True 清状态
  GET  /__mock/snapshots                                # 返回每请求摘要 (seq/kind/beta/cache_control 索引)
  POST /__mock/reset                                    # 清空日志与状态

业务端点:
  POST /v1/messages             原生 Anthropic 主端点 (流式 stream + 非流式 create 摘要)
  POST /anthropic/v1/messages   第三方 anthropic-wire
  POST /v1/chat/completions     OpenAI-wire 基线 (x-anthropic-beta 旁路键名验证)
  GET  /v1/models  POST /api/show

用法:
  python3 mock_anthropic.py [port]     # 默认 8910
"""
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
# 日志路径可由 MOCK_LOGFILE 覆盖 (cli_platform 复用本 mock 时指向自己的目录, 不污染 anthropic_platform)
LOGFILE = os.environ.get("MOCK_LOGFILE") or os.path.join(HERE, "anthropic_requests.jsonl")
_SENSITIVE = {"authorization", "api-key", "x-api-key", "cookie", "x-goog-api-key"}

# ── 全局可编程状态 ───────────────────────────────────────────
DEFAULT_CONFIG = {
    "scenario": "",            # 标签, 录进每条请求
    "content": ["text"],       # 响应 content 块序列: text/thinking/redacted_thinking/tool_use
    "stop_reason": "end_turn",  # end_turn | max_tokens(=length 续写) | tool_use
    "stop_reason_once": None,   # 若设, 仅下一个主请求用它, 之后回落 stop_reason
    "usage_mode": "auto",      # auto(按 body 大小) | fixed
    "usage_fixed": None,        # {input, cache_read, cache_creation, output}
    "cache_turns": False,       # True: 首请求 creation>0/read=0, 后续 read 增长(模拟命中)
    "force_prompt_tokens": None,  # 设为整数则 input_tokens 直接用它 (触发压缩用)
    "omit_cache_fields": False,  # True: 不发 cache_read/creation 字段 (测 None 兜底)
    "inject_signature_400_once": False,  # body 含 thinking 块 -> 400 签名错误一次
    "inject_1m_400_once": False,         # anthropic-beta 含 context-1m -> 400 一次
    "inject_tier_429_once": False,       # 下一主请求 -> 429 long-context 一次
    "error_once": None,         # {status, message} 通用注入一次
}
CONFIG = dict(DEFAULT_CONFIG)
STATE = {"seq": 0, "main_seq": 0, "cache_round": 0}
SNAPSHOTS = []


def reset_state(clear_log=True):
    STATE.update({"seq": 0, "main_seq": 0, "cache_round": 0})
    SNAPSHOTS.clear()
    if clear_log:
        open(LOGFILE, "w").close()
        try:
            os.chmod(LOGFILE, 0o600)
        except OSError:
            pass


# ── 断点枚举 (在最终 Anthropic body 上递归扫 cache_control) ──
def enumerate_cache_control(body):
    """返回携带 cache_control 的位置列表 + 每个 marker 的 ttl 形态。"""
    hits = []
    if not isinstance(body, dict):
        return hits
    sysv = body.get("system")
    if isinstance(sysv, list):
        for i, blk in enumerate(sysv):
            if isinstance(blk, dict) and "cache_control" in blk:
                hits.append({"loc": f"system[{i}]", "marker": blk["cache_control"]})
    for mi, m in enumerate(body.get("messages", []) or []):
        if not isinstance(m, dict):
            continue
        if "cache_control" in m:  # 信封级 (envelope)
            hits.append({"loc": f"messages[{mi}].envelope", "marker": m["cache_control"]})
        c = m.get("content")
        if isinstance(c, list):
            for ci, blk in enumerate(c):
                if isinstance(blk, dict) and "cache_control" in blk:
                    hits.append({"loc": f"messages[{mi}].content[{ci}]({blk.get('type')})",
                                 "marker": blk["cache_control"]})
    for ti, t in enumerate(body.get("tools", []) or []):
        if isinstance(t, dict) and "cache_control" in t:
            hits.append({"loc": f"tools[{ti}]", "marker": t["cache_control"]})
    return hits


def system_fingerprint(body):
    sysv = body.get("system") if isinstance(body, dict) else None
    if isinstance(sysv, str):
        return {"form": "str", "len": len(sysv)}
    if isinstance(sysv, list):
        txt = "".join(b.get("text", "") for b in sysv if isinstance(b, dict))
        return {"form": "list", "blocks": len(sysv), "len": len(txt)}
    return {"form": "none", "len": 0}


def has_thinking_block(body):
    for m in (body.get("messages", []) or []) if isinstance(body, dict) else []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") in ("thinking", "redacted_thinking"):
                    return True
    return False


def classify(body, path, stream):
    """主请求 vs 压缩摘要辅助请求 (单 user 消息 + 无 tools + 非流式)。"""
    if not isinstance(body, dict):
        return "unknown"
    msgs = body.get("messages") or []
    if len(msgs) == 1 and msgs[0].get("role") == "user" and not body.get("tools") and not stream:
        return "summary"
    return "main"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ── 录制 ──
    def _redact(self, k, v):
        if k.lower() in _SENSITIVE:
            return (v[:14] + "***REDACTED***") if len(v) > 14 else "***REDACTED***"
        return v

    def _record(self, body, stream, path):
        STATE["seq"] += 1
        seq = STATE["seq"]
        headers = {k: self._redact(k, v) for k, v in self.headers.items()}
        kind = classify(body, path, stream)
        cc = enumerate_cache_control(body)
        beta = self.headers.get("anthropic-beta", "")
        snap = {
            "seq": seq, "kind": kind, "scenario": CONFIG.get("scenario", ""),
            "path": path, "host_hdr": self.headers.get("Host", ""),
            "stream": stream,
            "model": body.get("model") if isinstance(body, dict) else None,
            "anthropic_beta": beta,
            "anthropic_beta_list": [b for b in beta.split(",") if b] if beta else [],
            "x_anthropic_beta": self.headers.get("x-anthropic-beta", ""),
            "user_agent": self.headers.get("User-Agent", ""),
            "x_app": self.headers.get("x-app", ""),
            "auth_kind": "x-api-key" if "x-api-key" in {k.lower() for k in self.headers}
                         else ("bearer" if (self.headers.get("Authorization", "").startswith("Bearer")) else "none"),
            "cache_control_hits": cc,
            "cache_control_count": len(cc),
            "ttl_forms": sorted({("1h" if isinstance(h["marker"], dict) and h["marker"].get("ttl") == "1h" else "5m") for h in cc}) if cc else [],
            "system": system_fingerprint(body) if isinstance(body, dict) else None,
            "has_thinking_history": has_thinking_block(body),
            "n_messages": len(body.get("messages", [])) if isinstance(body, dict) else 0,
            "has_tools": bool(body.get("tools")) if isinstance(body, dict) else False,
            "thinking_field": body.get("thinking") if isinstance(body, dict) else None,
            "output_config": body.get("output_config") if isinstance(body, dict) else None,
            "ts": time.strftime("%H:%M:%S"),
        }
        SNAPSHOTS.append(snap)
        entry = dict(snap)
        entry["headers"] = headers
        entry["body"] = body
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[{seq}] {kind:7s} {CONFIG.get('scenario','')[:28]:28s} {path:22s} "
              f"msgs={snap['n_messages']} cc={len(cc)} ttl={snap['ttl_forms']} "
              f"beta={len(snap['anthropic_beta_list'])} think_hist={snap['has_thinking_history']}",
              flush=True)
        return seq, kind

    # ── usage 计算 ──
    def _usage(self, body_len):
        if CONFIG.get("usage_mode") == "fixed" and CONFIG.get("usage_fixed"):
            u = CONFIG["usage_fixed"]
            return (int(u.get("input", 1000)), int(u.get("cache_read", 0)),
                    int(u.get("cache_creation", 0)), int(u.get("output", 12)))
        # auto
        fpt = CONFIG.get("force_prompt_tokens")
        inp = int(fpt) if fpt is not None else max(100, body_len // 4)
        cr = cc = 0
        if CONFIG.get("cache_turns"):
            rnd = STATE["cache_round"]
            STATE["cache_round"] += 1
            if rnd == 0:
                cc = max(1000, inp // 2)   # 首轮写缓存
            else:
                cr = max(1000, inp - 200)  # 后续命中
                cc = 50
        return inp, cr, cc, 12

    # ── HTTP ──
    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(cl) if cl else b""
        path = urlparse(self.path).path
        try:
            body = json.loads(raw.decode()) if raw else {}
        except Exception:
            body = {"_raw": raw.decode(errors="replace")[:4000]}

        if path == "/__mock/control":
            CONFIG.update(body or {})
            if body.get("reset"):
                reset_state(clear_log=bool(body.get("clear_log", False)))
            return self._json(200, {"ok": True, "config": {k: CONFIG[k] for k in DEFAULT_CONFIG}})
        if path == "/__mock/reset":
            CONFIG.clear(); CONFIG.update(DEFAULT_CONFIG)
            reset_state(clear_log=True)
            return self._json(200, {"ok": True})

        stream = bool(body.get("stream")) if isinstance(body, dict) else False

        if "/api/show" in path:
            return self._json(200, {"model_info": {"general.architecture": "llama",
                                    "llama.context_length": 200000}, "parameters": "num_ctx 200000"})
        if "messages" in path:
            return self._anthropic(body, stream, len(raw))
        if "responses" in path:
            return self._responses(body, stream, len(raw))
        if "chat/completions" in path:
            return self._chat(body, stream, len(raw))
        return self._json(200, {"status": "ok"})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/__mock/snapshots":
            return self._json(200, {"snapshots": SNAPSHOTS, "config": {k: CONFIG[k] for k in DEFAULT_CONFIG}})
        if path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": "claude-mock", "object": "model", "owned_by": "anthropic"}]})
        return self._json(200, {"status": "ok"})

    # ── error injection ──
    def _maybe_error(self, body):
        """返回 (status, err_json) 若需注入, 否则 None。content/beta 门控。"""
        beta = self.headers.get("anthropic-beta", "").lower()
        if CONFIG.get("inject_signature_400_once") and has_thinking_block(body):
            CONFIG["inject_signature_400_once"] = False
            return 400, {"type": "error", "error": {"type": "invalid_request_error",
                         "message": "Invalid signature in thinking block at index 0"}}
        if CONFIG.get("inject_1m_400_once") and "context-1m" in beta:
            CONFIG["inject_1m_400_once"] = False
            return 400, {"type": "error", "error": {"type": "invalid_request_error",
                         "message": "The long context beta is not yet available for this subscription"}}
        if CONFIG.get("inject_tier_429_once"):
            CONFIG["inject_tier_429_once"] = False
            return 429, {"type": "error", "error": {"type": "rate_limit_error",
                         "message": "This request would require extra usage on the long context tier"}}
        if CONFIG.get("error_once"):
            e = CONFIG["error_once"]; CONFIG["error_once"] = None
            return int(e.get("status", 500)), {"type": "error", "error": {"type": "api_error",
                         "message": e.get("message", "injected error")}}
        return None

    # ── Anthropic 响应 ──
    def _anthropic(self, body, stream, body_len):
        seq, kind = self._record(body, stream, urlparse(self.path).path)
        err = self._maybe_error(body)
        if err:
            status, ejson = err
            print(f"    -> inject error {status}: {ejson['error']['message'][:50]}", flush=True)
            return self._json(status, ejson)

        if kind == "main":
            STATE["main_seq"] += 1
        stop_reason = CONFIG.get("stop_reason", "end_turn")
        if kind == "main" and CONFIG.get("stop_reason_once"):
            stop_reason = CONFIG["stop_reason_once"]; CONFIG["stop_reason_once"] = None
        inp, cr, cc, outp = self._usage(body_len)
        blocks = CONFIG.get("content", ["text"]) if kind == "main" else ["text"]
        mid = "msg_" + uuid.uuid4().hex[:10]

        start_usage = {"input_tokens": inp, "output_tokens": 1}
        if not CONFIG.get("omit_cache_fields"):
            start_usage["cache_read_input_tokens"] = cr
            start_usage["cache_creation_input_tokens"] = cc

        if not stream:
            content = self._build_content_nonstream(blocks)
            return self._json(200, {"id": mid, "type": "message", "role": "assistant",
                "model": body.get("model", "claude-mock"), "stop_reason": stop_reason,
                "stop_sequence": None, "content": content,
                "usage": {**start_usage, "output_tokens": outp}})

        # streaming SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def sse(ev, data):
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        sse("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "content": [],
            "model": body.get("model", "claude-mock"), "stop_reason": None,
            "stop_sequence": None, "usage": start_usage}})
        idx = 0
        for spec in blocks:
            self._emit_block(sse, idx, spec)
            idx += 1
        sse("message_delta", {"type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": outp}})
        sse("message_stop", {"type": "message_stop"})

    def _emit_block(self, sse, idx, spec):
        if spec == "thinking":
            sse("content_block_start", {"type": "content_block_start", "index": idx,
                "content_block": {"type": "thinking", "thinking": ""}})
            sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                "delta": {"type": "thinking_delta", "thinking": "[Mock] internal reasoning."}})
            sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                "delta": {"type": "signature_delta", "signature": "SIG_PLACEHOLDER_MOCK"}})
            sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif spec == "redacted_thinking":
            sse("content_block_start", {"type": "content_block_start", "index": idx,
                "content_block": {"type": "redacted_thinking", "data": "REDACTED_DATA_PLACEHOLDER"}})
            sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif spec == "tool_use":
            sse("content_block_start", {"type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:8],
                                  "name": "mock_tool", "input": {}}})
            sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": "{}"}})
            sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        else:  # text
            sse("content_block_start", {"type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}})
            sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": "[Mock] Anthropic reply."}})
            sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    def _build_content_nonstream(self, blocks):
        out = []
        for spec in blocks:
            if spec == "thinking":
                out.append({"type": "thinking", "thinking": "[Mock] internal reasoning.",
                            "signature": "SIG_PLACEHOLDER_MOCK"})
            elif spec == "redacted_thinking":
                out.append({"type": "redacted_thinking", "data": "REDACTED_DATA_PLACEHOLDER"})
            elif spec == "tool_use":
                out.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:8],
                            "name": "mock_tool", "input": {}})
            else:
                out.append({"type": "text", "text": "[Mock] Anthropic reply."})
        return out or [{"type": "text", "text": "[Mock] Anthropic reply."}]

    # ── OpenAI Responses API (codex_responses 路径) ──
    def _responses(self, body, stream, body_len):
        self._record(body, stream, urlparse(self.path).path)
        err = self._maybe_error(body)
        if err:
            status, ejson = err
            return self._json(status, ejson)
        inp, cr, cc, outp = self._usage(body_len)
        rid = "resp_" + uuid.uuid4().hex[:10]
        mid = "msg_" + uuid.uuid4().hex[:10]
        text = "[Mock] Responses reply."
        usage = {"input_tokens": inp, "output_tokens": outp, "total_tokens": inp + outp}
        msg_item = {"id": mid, "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}

        def robj(status, output):
            return {"id": rid, "object": "response", "status": status, "model": body.get("model", "gpt-mock"),
                    "output": output, "usage": usage if status == "completed" else None}

        if not stream:
            return self._json(200, robj("completed", [msg_item]))
        # Responses API SSE: created -> output_item.added -> output_text.delta -> completed
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        seq = [0]

        def sse(ev, data):
            data = dict(data); data["sequence_number"] = seq[0]; seq[0] += 1
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()); self.wfile.flush()

        in_prog = {"id": mid, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
        sse("response.created", {"type": "response.created", "response": robj("in_progress", [])})
        sse("response.in_progress", {"type": "response.in_progress", "response": robj("in_progress", [])})
        sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": in_prog})
        sse("response.content_part.added", {"type": "response.content_part.added", "item_id": mid,
            "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
        sse("response.output_text.delta", {"type": "response.output_text.delta", "item_id": mid,
            "output_index": 0, "content_index": 0, "delta": text})
        sse("response.output_text.done", {"type": "response.output_text.done", "item_id": mid,
            "output_index": 0, "content_index": 0, "text": text})
        sse("response.content_part.done", {"type": "response.content_part.done", "item_id": mid,
            "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text, "annotations": []}})
        sse("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": msg_item})
        sse("response.completed", {"type": "response.completed", "response": robj("completed", [msg_item])})

    # ── OpenAI-wire 基线 (x-anthropic-beta 旁路键名) ──
    def _chat(self, body, stream, body_len):
        self._record(body, stream, urlparse(self.path).path)
        inp = max(100, body_len // 4)
        content = "[Mock] chat reply."
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            rid = "chatcmpl-" + uuid.uuid4().hex[:8]
            for ch in [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": inp, "completion_tokens": 12, "total_tokens": inp + 12}},
            ]:
                ch.update({"id": rid, "object": "chat.completion.chunk",
                           "created": int(time.time()), "model": "claude-mock"})
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        else:
            self._json(200, {"id": "chatcmpl-" + uuid.uuid4().hex[:8], "object": "chat.completion",
                "created": int(time.time()), "model": "claude-mock",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": inp, "completion_tokens": 12, "total_tokens": inp + 12}})

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8910
    reset_state(clear_log=True)
    print(f"Mock Anthropic backend: http://127.0.0.1:{port}", flush=True)
    print(f"  POST /v1/messages  /anthropic/v1/messages  /v1/chat/completions", flush=True)
    print(f"  POST /__mock/control   GET /__mock/snapshots   POST /__mock/reset", flush=True)
    print(f"  Log: {LOGFILE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
