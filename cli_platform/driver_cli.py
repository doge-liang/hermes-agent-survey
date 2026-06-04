#!/usr/bin/env python3
"""
Hermes 真实 CLI + mock-server 探测测试台 — Driver

用**真实 hermes CLI**(`hermes chat` 子命令, 走完整 config 解析 + 全量 system prompt + 28 tools 入口)
对 mock 跑, 捕获 agent hints + 验证触发场景。补 import-driver 的盲区(无 tools / system 缩水 /
绕过 config→状态映射)。

关键契约 (Workflow 设计 + 实测确认):
  - 受控多轮必须走 `hermes chat`(NOT `-z` one-shot: oneshot 分支在 resume 短路之前, 忽略续接)。
    每轮一个子进程, 共享 isolated HERMES_HOME 里的 SQLite SessionDB; turn1 建会话并从 stderr 抓
    session_id, 后续轮 `--resume <SID>` 还原历史。
  - `chat` 路径**不能加 --ignore-user-config**(它把 {HERMES_HOME}/config.yaml 当 user config,
    --ignore-user-config 会丢弃场景 config)。
  - security.allow_lazy_installs:false + `-t ''` 避免可选 toolset 懒安装卡住。
  - 压缩场景必须用第三方 anthropic-wire(custom + /anthropic), native 路径摘要会逃逸真实 Anthropic。

用法:
  <python> driver_cli.py [PORT] [scenario_id ...]    # 不带 scenario 跑全部; 自动起停 mock
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SURVEY = os.path.dirname(HERE)
MOCK_SCRIPT = os.path.join(SURVEY, "anthropic_platform", "mock_anthropic.py")
HERMES = "/home/niaowuuu/.local/bin/hermes"
VENV_PY = "/home/niaowuuu/.hermes/hermes-agent/venv/bin/python3"
LOGFILE = os.path.join(HERE, "cli_requests.jsonl")
WORK = "/home/niaowuuu/.claude/jobs/15bd174c/tmp/cli-run"
SID_RE = re.compile(r"\b(\d{8}_\d{6}_[0-9a-f]{6})\b")
# 压缩阈值有 64000 token 硬地板 (threshold_tokens=max(ctx*0.5, 64000), 无法用 config 绕过)。
# CLI 下只能让消息内容真累积过 64000 token。单个 -q 参数有 ~128KB (MAX_ARG_STRLEN) 限制,
# 故每轮 filler ~119000 字符 (≈29000 token), 历史经 SessionDB 累积 (不占 argv), 3-4 轮过阈值触发 preflight。
FILLER = " lorem ipsum dolor sit amet" * 4400

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8920
MOCK = f"http://127.0.0.1:{PORT}"


# ── 场景定义 ────────────────────────────────────────────────
def cfg(**kw):
    """生成 config.yaml 文本; 占位 {MOCK}/{PORT} 由 runner 替换。"""
    return kw


SCENARIOS = [
    {
        "id": "RC-01", "maps_to": "A", "control": {"content": ["text"], "usage_mode": "auto"},
        "provider": "mockcustom", "model": "mock-model",
        "config": """model: mock-model
provider: mockcustom
custom_providers:
  - name: mockcustom
    base_url: "{MOCK}/v1"
    api_key: "sk-mock"
    model: mock-model
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
""",
        "turns": ["Turn 1: please acknowledge."],
    },
    {
        "id": "RC-02", "maps_to": "E,S3,B,S5", "control": {"content": ["text"], "cache_turns": True, "usage_mode": "auto"},
        "provider": "thirdparty", "model": "claude-sonnet-4-20250514",
        "config": """model: claude-sonnet-4-20250514
provider: thirdparty
custom_providers:
  - name: thirdparty
    base_url: "{MOCK}/anthropic"
    api_key: "sk-mock"
    model: claude-sonnet-4-20250514
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
""",
        "turns": ["Turn 1: please acknowledge.", "Turn 2: continue.", "Turn 3: continue again."],
    },
    {
        "id": "RC-03", "maps_to": "S2", "control": {"content": ["text"], "usage_mode": "auto"},
        "provider": "thirdparty", "model": "claude-sonnet-4-20250514",
        "config": """model: claude-sonnet-4-20250514
provider: thirdparty
custom_providers:
  - name: thirdparty
    base_url: "{MOCK}/anthropic"
    api_key: "sk-mock"
    model: claude-sonnet-4-20250514
    context_length: 200000
toolsets: []
prompt_caching:
  cache_ttl: 1h
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
""",
        "turns": ["Turn 1: please acknowledge."],
    },
    {
        "id": "RC-04a", "maps_to": "D,S7(adaptive xhigh)", "control": {"content": ["text"]},
        "provider": "thirdparty", "model": "claude-opus-4-7", "reasoning_effort": "xhigh",
        "config": "{ANTHROPIC_WIRE_REASONING}", "turns": ["Turn 1: think then acknowledge."],
    },
    {
        "id": "RC-04b", "maps_to": "S7(xhigh->max downgrade)", "control": {"content": ["text"]},
        "provider": "thirdparty", "model": "claude-opus-4-6", "reasoning_effort": "xhigh",
        "config": "{ANTHROPIC_WIRE_REASONING}", "turns": ["Turn 1: downgrade xhigh to max."],
    },
    {
        "id": "RC-04c", "maps_to": "S7(manual budget old)", "control": {"content": ["text"]},
        "provider": "thirdparty", "model": "claude-3-7-sonnet-20250219", "reasoning_effort": "high",
        "config": "{ANTHROPIC_WIRE_REASONING}", "turns": ["Turn 1: old model manual budget."],
    },
    {
        "id": "RC-05", "maps_to": "H", "control": {"content": ["text"], "usage_mode": "auto"},
        "provider": "codexmock", "model": "gpt-5", "pass_session_id": True,
        "config": """model: gpt-5
provider: codexmock
custom_providers:
  - name: codexmock
    base_url: "{MOCK}/v1"
    api_key: "sk-codex-mock"
    model: gpt-5
    api_mode: codex_responses
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
""",
        "turns": ["Turn 1: please acknowledge."],
    },
    {
        "id": "RC-06", "maps_to": "G", "control": {"content": ["text"]}, "host_gated": True,
        "provider": "openrouter", "model": "x-ai/grok-4", "pass_session_id": True,
        "env_set": {"OPENROUTER_API_KEY": "sk-or-mock"},
        "config": """model: x-ai/grok-4
provider: openrouter
custom_providers:
  - name: openrouter
    base_url: "http://openrouter.localtest.me:{PORT}/v1"
    api_key: "sk-or-mock"
    model: x-ai/grok-4
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
""",
        "turns": ["Turn 1: please acknowledge."],
    },
    {
        "id": "RC-07", "maps_to": "J,S4", "control": {"content": ["text"]}, "host_gated": True,
        "provider": "openrouter", "model": "anthropic/claude-sonnet-4", "reasoning_effort": "high",
        "pass_session_id": True, "env_set": {"OPENROUTER_API_KEY": "sk-or-mock"},
        "config": """model: anthropic/claude-sonnet-4
provider: openrouter
custom_providers:
  - name: openrouter
    base_url: "http://openrouter.localtest.me:{PORT}/v1"
    api_key: "sk-or-mock"
    model: anthropic/claude-sonnet-4
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
  reasoning_effort: high
""",
        "turns": ["Turn 1: reason then acknowledge."],
    },
    {
        "id": "RC-08", "maps_to": "F,S9", "control": {"content": ["text"], "usage_mode": "auto", "force_prompt_tokens": 2000000},
        "provider": "thirdparty", "model": "claude-sonnet-4-20250514", "filler": True,
        "config": """model: claude-sonnet-4-20250514
provider: thirdparty
custom_providers:
  - name: thirdparty
    base_url: "{MOCK}/anthropic"
    api_key: "sk-mock"
    model: claude-sonnet-4-20250514
    context_length: 8000
toolsets: []
compression:
  enabled: true
  threshold: 0.5
  protect_first_n: 1
  protect_last_n: 2
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
""",
        "turns": ["Turn 1: please acknowledge.", "Turn 2: continue.", "Turn 3: continue.",
                  "Turn 4: continue.", "Turn 5: continue."],
    },
    {
        "id": "RC-09", "maps_to": "S13", "control": {"content": ["text"], "inject_tier_429_once": True, "usage_mode": "auto"},
        "provider": "thirdparty", "model": "claude-sonnet-4-20250514",
        "config": """model: claude-sonnet-4-20250514
provider: thirdparty
custom_providers:
  - name: thirdparty
    base_url: "{MOCK}/anthropic"
    api_key: "sk-mock"
    model: claude-sonnet-4-20250514
    context_length: 200000
toolsets: []
compression:
  enabled: true
  threshold: 0.5
security:
  allow_lazy_installs: false
agent:
  max_turns: 3
""",
        "turns": ["Turn 1: please acknowledge."],
    },
    {
        "id": "RC-10", "maps_to": "S10", "control": {"content": ["text"], "stop_reason_once": "max_tokens", "usage_mode": "auto"},
        "provider": "thirdparty", "model": "claude-sonnet-4-20250514",
        "config": """model: claude-sonnet-4-20250514
provider: thirdparty
custom_providers:
  - name: thirdparty
    base_url: "{MOCK}/anthropic"
    api_key: "sk-mock"
    model: claude-sonnet-4-20250514
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 5
""",
        "turns": ["Turn 1: produce output."],
    },
]

ANTHROPIC_WIRE_REASONING = """model: {MODEL}
provider: thirdparty
custom_providers:
  - name: thirdparty
    base_url: "{MOCK}/anthropic"
    api_key: "sk-mock"
    model: {MODEL}
    context_length: 200000
toolsets: []
security:
  allow_lazy_installs: false
agent:
  max_turns: 2
  reasoning_effort: {REASONING}
"""


# ── mock 控制 ──────────────────────────────────────────────
def mock_post(path, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(MOCK + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def mock_ready(timeout=20):
    for _ in range(timeout * 2):
        try:
            urllib.request.urlopen(MOCK + "/__mock/snapshots", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


# ── 运行一个 hermes chat 子进程 ─────────────────────────────
def run_chat(home, scen, query, sid=None):
    argv = [HERMES, "chat", "-q", query, "-Q", "--provider", scen["provider"],
            "-m", scen["model"], "-t", "", "--source", "rc-" + scen["id"], "--yolo"]
    if scen.get("pass_session_id"):
        argv.append("--pass-session-id")
    if sid:
        argv += ["--resume", sid]
    env = dict(os.environ)
    env["HERMES_HOME"] = home
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY"):
        env.pop(k, None)
    for k, v in (scen.get("env_set") or {}).items():
        env[k] = v
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=150)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, err, rc = (e.stdout or ""), (e.stderr or "") + "\n[TIMEOUT]", -9
    blob = (err or "") + "\n" + (out or "")
    # 抓 session_id: 优先含 session 关键词的行
    sid_found = None
    for line in blob.splitlines():
        if "session" in line.lower():
            m = SID_RE.search(line)
            if m:
                sid_found = m.group(1)
    if not sid_found:
        m = SID_RE.findall(blob)
        sid_found = m[-1] if m else None
    return rc, sid_found, blob


def run_scenario(scen):
    sid_input = scen["id"]
    home = os.path.join(WORK, "home-" + sid_input)
    os.makedirs(home, exist_ok=True)
    # config
    conf = scen["config"]
    if conf == "{ANTHROPIC_WIRE_REASONING}":
        conf = ANTHROPIC_WIRE_REASONING.replace("{MODEL}", scen["model"]).replace("{REASONING}", scen["reasoning_effort"])
    conf = conf.replace("{MOCK}", MOCK).replace("{PORT}", str(PORT))
    with open(os.path.join(home, "config.yaml"), "w") as f:
        f.write(conf)
    # mock 控制: 设场景标签 + 行为, reset 状态但保留日志
    ctrl = {"scenario": scen["id"], "reset": True, "clear_log": False}
    ctrl.update(scen.get("control", {}))
    # 每场景把未涉及的注入开关复位, 避免上一场景残留
    for k in ("cache_turns", "inject_tier_429_once", "inject_signature_400_once",
              "inject_1m_400_once", "force_prompt_tokens", "stop_reason_once", "omit_cache_fields"):
        ctrl.setdefault(k, False if k.startswith(("cache_turns", "inject", "omit")) else None)
    ctrl.update(scen.get("control", {}))
    mock_post("/__mock/control", ctrl)

    print(f"\n{'='*78}\n[{scen['id']}] maps_to {scen['maps_to']}  provider={scen['provider']} model={scen['model']}"
          + (" [HOST-GATED]" if scen.get("host_gated") else ""), flush=True)
    sid = None
    filler = FILLER if scen.get("filler") else ""
    for i, q in enumerate(scen["turns"]):
        rc, new_sid, blob = run_chat(home, scen, q + filler, sid=sid)
        if i == 0 and new_sid:
            sid = new_sid
        tag = "ok" if rc == 0 else f"rc={rc}"
        print(f"  turn{i+1}: {tag} sid={sid}", flush=True)
        if rc != 0:
            errtail = "\n".join(l for l in blob.splitlines() if l.strip())[-300:]
            print(f"    stderr尾: ...{errtail[-260:]}", flush=True)


def main():
    os.makedirs(WORK, exist_ok=True)
    open(LOGFILE, "w").close()
    # 起 mock
    env = dict(os.environ)
    env["MOCK_LOGFILE"] = LOGFILE
    mock = subprocess.Popen([VENV_PY, MOCK_SCRIPT, str(PORT)], env=env,
                            stdout=open(os.path.join(WORK, "mock.log"), "w"), stderr=subprocess.STDOUT)
    try:
        if not mock_ready():
            print("mock 未就绪, 退出", flush=True)
            return
        print(f"mock 就绪 (port {PORT}, log {LOGFILE})", flush=True)
        which = [a for a in sys.argv[2:] if not a.isdigit()]
        run_gated = "--run-host-gated" in sys.argv
        for scen in SCENARIOS:
            if which and scen["id"] not in which and scen["id"].rstrip("abc") not in which:
                continue
            if scen.get("host_gated") and not run_gated:
                # host 门控 (openrouter profile 要求 provider=openrouter 但 base_url 锁死 openrouter.ai,
                # localhost 无法激活 profile, 实跑会打到真实 openrouter.ai 401)。默认跳过, 标 INFO;
                # 这些场景作为"真实后端测试台"的 fixture, 留到接真实 openrouter 阶段验证。
                print(f"\n{'='*78}\n[{scen['id']}] maps_to {scen['maps_to']}  [HOST-GATED — 跳过执行]"
                      f"\n  原因: openrouter profile 要求 host==openrouter.ai, localhost mock 无法激活;"
                      f" 作为真实后端测试台 fixture 保留。", flush=True)
                continue
            try:
                run_scenario(scen)
            except Exception as e:
                print(f"  [场景 {scen['id']} 异常] {type(e).__name__}: {str(e)[:160]}", flush=True)
        time.sleep(1)
        n = sum(1 for _ in open(LOGFILE)) if os.path.exists(LOGFILE) else 0
        print(f"\n{'='*78}\n完成. 捕获 {n} 个请求 -> {LOGFILE}\n{'='*78}", flush=True)
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except Exception:
            mock.kill()


if __name__ == "__main__":
    main()
