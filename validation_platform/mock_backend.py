#!/usr/bin/env python3
"""
Hermes 上下文管理验证平台 — Mock LLM 后端

一个支持 OpenAI Chat Completions / Anthropic Messages / OpenAI Responses 三种协议的
mock 后端，记录所有请求的完整 headers + body 到 JSONL，并可配置返回的 token usage
（用于触发上下文压缩）。

特性:
  - 完整记录每个请求 (headers 脱敏 Authorization, body 原样)
  - 可通过 X-Mock-Prompt-Tokens header 或环境变量控制返回的 usage.prompt_tokens
    （让 Hermes 的 context tracker 认为上下文接近满，从而触发压缩）
  - 区分主请求 / 辅助请求 (通过 model 名 + body 特征启发式标注)
  - 支持 SSE streaming 和非 streaming
  - 默认返回纯文本响应 (无 tool_calls) → agent 单轮结束

用法:
  python3 mock_backend.py [port]            # 默认 8900
  日志: validation_platform/requests.jsonl  (每行一个 JSON)
"""
import json, os, sys, time, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
LOGFILE = os.path.join(HERE, "requests.jsonl")
_SENSITIVE = {"authorization", "api-key", "x-api-key", "cookie", "x-goog-api-key"}

# 全局请求计数器
_SEQ = [0]


def _classify_request(model, body):
    """启发式标注请求类型: main(主对话) / summary(压缩摘要) / title(标题) / aux(其他辅助)."""
    if not isinstance(body, dict):
        return "unknown"
    msgs = body.get("messages") or []
    # 只看对话文本 (messages/system/instructions), 排除 reasoning.summary 等
    # 参数字段误判 (codex 的 reasoning={"summary":"auto"} 不应被当成压缩摘要请求)。
    parts = [json.dumps(msgs, ensure_ascii=False)]
    sys = body.get("system")
    parts.append(sys if isinstance(sys, str) else json.dumps(sys, ensure_ascii=False))
    instr = body.get("instructions")
    if isinstance(instr, str):
        parts.append(instr)
    inp = body.get("input")
    if inp is not None:
        parts.append(json.dumps(inp, ensure_ascii=False))
    text = " ".join(p for p in parts if p).lower()
    # 摘要请求: 对话内容含 summariz/compact 关键词
    if "summar" in text or "compact" in text or "condense" in text:
        return "summary"
    if "title" in text and len(msgs) <= 3:
        return "title"
    # 主请求通常带 tools 或多轮 messages
    return "main"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _redact(self, k, v):
        if k.lower() in _SENSITIVE:
            return (v[:12] + "***REDACTED***") if len(v) > 12 else "***REDACTED***"
        return v

    def _record(self, body_bytes):
        _SEQ[0] += 1
        seq = _SEQ[0]
        headers = {k: self._redact(k, v) for k, v in self.headers.items()}
        try:
            body = json.loads(body_bytes.decode()) if body_bytes else {}
        except Exception:
            body = {"_raw": body_bytes.decode(errors="replace")[:8000]}
        model = body.get("model", "") if isinstance(body, dict) else ""
        kind = _classify_request(model, body)
        entry = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": self.command,
            "path": self.path,
            "kind": kind,
            "model": model,
            "headers": headers,
            "body": body,
        }
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # 简短 stdout
        nmsg = len(body.get("messages", [])) if isinstance(body, dict) else 0
        has_cc = "cache_control" in json.dumps(body, ensure_ascii=False)
        has_tools = "tools" in body if isinstance(body, dict) else False
        print(f"[{seq}] {kind:8s} {self.command} {urlparse(self.path).path}  "
              f"model={model!r} msgs={nmsg} cache_control={has_cc} tools={has_tools}",
              flush=True)
        return seq, body

    def _prompt_tokens(self, body_bytes=b""):
        """返回要报告的 prompt_tokens.

        优先级: X-Mock-Prompt-Tokens header > MOCK_PROMPT_TOKENS env >
        按请求 body 实际字节大小估算 (len//4, 模拟真实 token 计数).
        按 body 大小估算让 big_content 场景能自然触发 real-time 压缩。
        """
        hdr = self.headers.get("X-Mock-Prompt-Tokens")
        if hdr and hdr.isdigit():
            return int(hdr)
        env = os.environ.get("MOCK_PROMPT_TOKENS", "")
        if env.isdigit():
            return int(env)
        return max(100, len(body_bytes) // 4)

    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(cl) if cl else b""
        seq, body = self._record(body_bytes)
        path = urlparse(self.path).path
        stream = bool(body.get("stream")) if isinstance(body, dict) else False
        ptoks = self._prompt_tokens(body_bytes)

        if "/api/show" in path:
            # Ollama /api/show — 返回大 context length, 让辅助 client 认为可用
            return self._json(200, {
                "model_info": {"general.architecture": "llama",
                               "llama.context_length": 200000},
                "parameters": "num_ctx 200000",
            })
        if "generateContent" in path:
            return self._gemini(ptoks)
        if "/v1/messages" in path:
            return self._anthropic(stream, ptoks)
        if "responses" in path:
            return self._responses(stream, ptoks)
        return self._chat(stream, ptoks)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "mock-model", "object": "model", "owned_by": "mock"}]})
        else:
            self._json(200, {"status": "ok"})

    # ── response builders ──────────────────────────────
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _chat(self, stream, ptoks):
        content = "[Mock] Acknowledged. Reply text only, no tool calls."
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            rid = "chatcmpl-" + uuid.uuid4().hex[:8]
            for chunk in [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": ptoks, "completion_tokens": 12, "total_tokens": ptoks + 12}},
            ]:
                chunk.update({"id": rid, "object": "chat.completion.chunk",
                              "created": int(time.time()), "model": "mock-model"})
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._json(200, {
                "id": "chatcmpl-" + uuid.uuid4().hex[:8], "object": "chat.completion",
                "created": int(time.time()), "model": "mock-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": ptoks, "completion_tokens": 12, "total_tokens": ptoks + 12},
            })

    def _anthropic(self, stream, ptoks):
        mid = "msg_" + uuid.uuid4().hex[:8]
        if stream:
            # 完整的 Anthropic SSE 事件序列, 让 anthropic SDK 能正确解析
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def sse(event, data):
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()

            sse("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "content": [],
                "model": "claude-mock", "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": ptoks, "output_tokens": 1,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
            sse("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}})
            sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "[Mock] Anthropic reply."}})
            sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            sse("message_delta", {"type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 12}})
            sse("message_stop", {"type": "message_stop"})
        else:
            self._json(200, {
                "id": mid, "type": "message", "role": "assistant",
                "model": "claude-mock", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "[Mock] Anthropic reply."}],
                "usage": {"input_tokens": ptoks, "output_tokens": 12,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            })

    def _responses(self, stream, ptoks):
        rid = "resp_" + uuid.uuid4().hex[:8]
        mid = "msg_" + uuid.uuid4().hex[:8]
        text = "[Mock] Responses reply."
        msg_item = {"id": mid, "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
        usage = {"input_tokens": ptoks, "output_tokens": 12, "total_tokens": ptoks + 12}

        def resp_obj(status, output):
            return {"id": rid, "object": "response", "status": status, "model": "gpt-mock",
                    "output": output, "usage": usage if status == "completed" else None}

        if not stream:
            return self._json(200, resp_obj("completed", [msg_item]))

        # Responses API SSE 事件序列, 让 OpenAI SDK 的 responses stream 能正确解析到 terminal。
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        seq = [0]

        def sse(ev, data):
            data = dict(data)
            data["sequence_number"] = seq[0]
            seq[0] += 1
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        in_progress_item = {"id": mid, "type": "message", "role": "assistant",
                            "status": "in_progress", "content": []}
        sse("response.created", {"type": "response.created", "response": resp_obj("in_progress", [])})
        sse("response.in_progress", {"type": "response.in_progress", "response": resp_obj("in_progress", [])})
        sse("response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": in_progress_item})
        sse("response.content_part.added",
            {"type": "response.content_part.added", "item_id": mid, "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
        sse("response.output_text.delta",
            {"type": "response.output_text.delta", "item_id": mid, "output_index": 0,
             "content_index": 0, "delta": text})
        sse("response.output_text.done",
            {"type": "response.output_text.done", "item_id": mid, "output_index": 0,
             "content_index": 0, "text": text})
        sse("response.content_part.done",
            {"type": "response.content_part.done", "item_id": mid, "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": text, "annotations": []}})
        sse("response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": msg_item})
        sse("response.completed",
            {"type": "response.completed", "response": resp_obj("completed", [msg_item])})

    def _gemini(self, ptoks):
        self._json(200, {
            "candidates": [{"content": {"parts": [{"text": "[Mock] Gemini reply."}], "role": "model"},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": ptoks, "candidatesTokenCount": 12,
                              "totalTokenCount": ptoks + 12},
        })


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    # 清空日志
    open(LOGFILE, "w").close()
    try:
        os.chmod(LOGFILE, 0o600)
    except OSError:
        pass
    print(f"Mock backend: http://127.0.0.1:{port}", flush=True)
    print(f"  Chat:      POST /v1/chat/completions", flush=True)
    print(f"  Anthropic: POST /v1/messages", flush=True)
    print(f"  Responses: POST /v1/responses", flush=True)
    print(f"  Log:       {LOGFILE}", flush=True)
    print(f"  Tune usage via MOCK_PROMPT_TOKENS env or X-Mock-Prompt-Tokens header", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
