#!/usr/bin/env python3
"""
Minimal proxy that logs ALL HTTP headers + body between Hermes and a real/simulated backend.
Starts a local server, runs Hermes against it, then exits.

Usage:
    uv run python mock_capture.py
"""
import asyncio
import json
import sys
import os
import time
from aiohttp import web

LOG = []


async def handler(request):
    """Capture everything, return a valid chat response."""
    entry = {
        "ts": time.time(),
        "method": request.method,
        "path": request.path,
        "headers": dict(request.headers),
        "body": None,
    }

    try:
        body = await request.read()
        entry["body"] = json.loads(body.decode()) if body else None
    except Exception:
        entry["body"] = (await request.text()) if request.can_read_body else "(binary)"

    LOG.append(entry)

    # Return a valid OpenAI chat response
    resp = {
        "id": f"chatcmpl-mock-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": entry.get("body", {}).get("model", "mock-model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "[Mock OK]"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return web.json_response(resp)


async def models_handler(request):
    return web.json_response({"object": "list", "data": [
        {"id": "mock-model", "object": "model", "created": int(time.time()), "owned_by": "mock"}]})


async def main():
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", models_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 19999)
    await site.start()
    print(f"MOCK: listening on http://127.0.0.1:19999", flush=True)

    # Now send a request from the Hermes client machinery
    import sys
    sys.path.insert(0, "/mnt/d/Workspace/project/hermes-agent")
    os.environ["HERMES_HOME"] = "/tmp/hermes-mock-capture"
    os.makedirs(os.environ["HERMES_HOME"], exist_ok=True)
    os.environ["OPENAI_BASE_URL"] = ""
    os.environ["OPENROUTER_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""

    # Use the low-level OpenAI client that Hermes would build
    from agent.process_bootstrap import OpenAI

    # Manually replicate what Hermes client init does — a plain OpenAI client
    client = OpenAI(
        api_key="mock-key",
        base_url="http://127.0.0.1:19999/v1",
    )

    # Make one chat completion request
    try:
        print("MOCK: sending request via OpenAI SDK...", flush=True)
        resp = client.chat.completions.create(
            model="mock-model",
            messages=[{"role": "user", "content": "Hello, world!"}],
            max_tokens=10,
        )
        print(f"MOCK: got response: {resp.choices[0].message.content}", flush=True)
    except Exception as e:
        print(f"MOCK: SDK error (expected for non-streaming ok): {e}", flush=True)

    # Print the captured request
    print("\n" + "=" * 100)
    print("CAPTURED REQUEST HEADERS:")
    print("=" * 100)
    for i, entry in enumerate(LOG):
        print(f"\n--- Request #{i+1} ---")
        print(f"  {entry['method']} {entry['path']}")
        print(f"  Headers ({len(entry['headers'])}):")
        for k, v in sorted(entry["headers"].items()):
            # Mask the Authorization header
            if k.lower() == "authorization":
                v = v[:30] + "..." if len(v) > 30 else v
            print(f"    {k}: {v}")
        if entry["body"]:
            # Summarize body
            body = entry["body"]
            keys = list(body.keys()) if isinstance(body, dict) else ["<raw>"]
            print(f"  Body keys: {keys}")
            if "model" in (body if isinstance(body, dict) else {}):
                print(f"    model: {body['model']}")
            if "messages" in (body if isinstance(body, dict) else {}):
                msgs = body["messages"]
                print(f"    messages: {len(msgs)} message(s)")
                for m in msgs:
                    role = m.get("role", "?")
                    content = str(m.get("content", ""))[:80]
                    print(f"      [{role}] {content}...")
            # Check for extra_body / metadata
            if isinstance(body, dict):
                for extra_key in ("extra_body", "metadata", "tags", "user"):
                    if extra_key in body:
                        print(f"    {extra_key}: {json.dumps(body[extra_key], indent=4)}")

    await asyncio.sleep(0.5)
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
