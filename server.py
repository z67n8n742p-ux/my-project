"""Localhost backend for formyproject agent. Stdlib only (+openai pkg).
Run:  export MAXPLUS_API_KEY="ccsk-..."; python3 server.py
Open: http://localhost:8000
Provider: MaxPlus AI, pool chinese-specials (OpenAI-compatible).
Docs: https://maxplus-ai.cc/docs/api
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("run: pip install -r requirements.txt")
    raise

from tools import TOOLS_SPECS, SUMMARY_SYSTEM, compact_messages, execute_tool

ROOT = Path(__file__).parent
LOG_FILE = ROOT / "server.log"


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# auto-load .env (no dep needed)
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE_URL = os.getenv("MAXPLUS_BASE_URL", "https://api.maxplus-ai.cc/chinese-specials/v1")
API_KEY = os.getenv("MAXPLUS_API_KEY", os.getenv("OPENCODE_API_KEY", ""))
DEFAULT_MODEL = os.getenv("MAXPLUS_MODEL", os.getenv("OPENCODE_MODEL", "glm-5.3-flash"))
try:
    PORT = int(os.getenv("PORT", "8000"))
except ValueError:
    print(f"warning: invalid PORT={os.getenv('PORT')!r}, falling back to 8000")
    PORT = 8000

CHAT_MODELS = [
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro-0813",
    "mimo-v2.5",
    "mimo-v2.6-flash",
]


def get_live_models():
    """Fetch real-time model list from MaxPlus pool. Falls back to CHAT_MODELS.
    Cached for 120s so every /api/chat doesn't pay a 10s /models round-trip."""
    now = time.time()
    if get_live_models._cache and now - get_live_models._ts < 120:
        return get_live_models._cache
    if not API_KEY:
        return CHAT_MODELS
    try:
        req = urllib.request.Request(
            BASE_URL.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        get_live_models._cache = ids or CHAT_MODELS
        get_live_models._ts = now
        return get_live_models._cache
    except Exception:
        # serve stale cache if we have it, else static fallback
        return get_live_models._cache or CHAT_MODELS


get_live_models._cache = []
get_live_models._ts = 0.0

# question tool needs interactive stdin -> must not hang the HTTP handler
SERVER_TOOLS_SPECS = [t for t in TOOLS_SPECS if t["function"]["name"] != "question"]


# live run progress: run_id -> {"logs": [...], "done": bool, "reply": str, "ts": float, "steer": [str]}
# frontend polls /api/progress while /api/chat is still working so it can
# show background tool activity instead of only a thinking spinner.
# P0-1: run_id is the idempotency key. Retried POST with same run returns cached reply, no new LLM call.
# P0-2: steer list holds mid-run follow-ups, drained only between tool steps (Safe Step Boundary).
RUNS = {}
RUNS_LOCK = threading.Lock()


def _is_retryable(exc: Exception) -> bool:
    """P0-3: retry only rate-limit / 5xx / transport. Never 4xx (bad key, unknown model, bad request)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None) or getattr(exc, "http_status", None)
    try:
        if status is not None and int(status) == 429:
            return True
        if status is not None and int(status) >= 500:
            return True
    except Exception:
        pass
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(k in name for k in ("ratelimit", "internalserver", "apiconnection", "apitimeout", "timeout", "connection")):
        return True
    if any(k in msg for k in ("rate limit", "429", "500", "502", "503", "504", "connection", "timeout", "temporarily", "overloaded", "incomplete stream", "truncated")):
        # avoid retrying auth/validation 4xx phrased in message
        if any(k in msg for k in ("401", "403", "404", "unknown model", "invalid api key", "incorrect api key")):
            return False
        return True
    return False


def _chat_create_with_retry(client, model, messages, tools=None, run_id=None, live_logs=None):
    """P0-3: initial request + max 4 retries, jittered exp backoff 1/2/4/8s. Logs retry.scheduled."""
    import random
    last = None
    for attempt in range(5):
        try:
            kwargs = {"model": model, "messages": messages}
            if tools is not None:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            last = e
            if attempt >= 4 or not _is_retryable(e):
                raise
            delay = (2 ** attempt) + random.random() * 0.5
            log(f"/api/chat retry.scheduled run={run_id} attempt={attempt + 1}/4 delay={delay:.1f}s err={str(e)[:200]}")
            if live_logs is not None:
                with RUNS_LOCK:
                    live_logs.append({"tool": "retry", "args": {"attempt": attempt + 1}, "result": f"retry.scheduled in {delay:.1f}s: {str(e)[:300]}"})
                    if run_id and run_id in RUNS:
                        RUNS[run_id]["ts"] = time.time()
            time.sleep(delay)
    raise last


SESSIONS_DIR = ROOT / ".sessions"


def _safe_sid(s: str) -> str:
    return "".join(c for c in str(s or "") if c.isalnum() or c in ("-", "_"))[:64]


def _save_turn(sid: str, user_msg: str, reply: str, model: str, tool_logs) -> None:
    """Minimal session persistence: append user+assistant lines to .sessions/<id>.jsonl."""
    if not sid:
        return
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        with open(SESSIONS_DIR / f"{sid}.jsonl", "a") as f:
            f.write(json.dumps({"role": "user", "content": user_msg, "ts": ts}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": reply, "model": model,
                                "tools": [t.get("tool") for t in (tool_logs or [])], "ts": time.time()}) + "\n")
    except Exception as e:
        log(f"session save error {sid}: {e}")


STORE_DB = SESSIONS_DIR / "store.db"
DB_LOCK = threading.Lock()


def _db():
    """Per-call SQLite connection (stdlib only). DB lives in git-ignored .sessions/."""
    import sqlite3
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(STORE_DB), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS runs(
      run_id TEXT PRIMARY KEY, reply TEXT, tools TEXT, done INTEGER,
      status TEXT, ts REAL)""")
    return c


def _store_init():
    """Boot recovery: stale 'running' claims (crash/kill) -> 'interrupted'. Prune rows older than 24h."""
    try:
        with DB_LOCK:
            c = _db()
            n = c.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]
            if n:
                c.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
                c.commit()
            c.execute("DELETE FROM runs WHERE ts < ?", (time.time() - 86400,))
            c.commit()
            c.close()
        if n:
            log(f"store: recovered {n} stale running claim(s) -> interrupted")
        return n
    except Exception as e:
        log(f"store init error: {e}")
        return 0


def _claim_start(run_id):
    """Write-ahead claim: this process owns run_id as of now."""
    try:
        with DB_LOCK:
            c = _db()
            c.execute("INSERT OR REPLACE INTO runs(run_id,reply,tools,done,status,ts) VALUES(?,?,?,?,?,?)",
                      (run_id, None, None, 0, "running", time.time()))
            c.commit()
            c.close()
    except Exception as e:
        log(f"store claim error {run_id}: {e}")


def _claim_finish(run_id, reply, tool_logs, status="done"):
    try:
        with DB_LOCK:
            c = _db()
            c.execute("UPDATE runs SET reply=?, tools=?, done=1, status=?, ts=? WHERE run_id=?",
                      (reply, json.dumps(tool_logs or [])[:200000], status, time.time(), run_id))
            c.commit()
            c.close()
    except Exception as e:
        log(f"store finish error {run_id}: {e}")


def _claim_fail(run_id):
    try:
        with DB_LOCK:
            c = _db()
            c.execute("UPDATE runs SET done=1, status='failed', ts=? WHERE run_id=?", (time.time(), run_id))
            c.commit()
            c.close()
    except Exception as e:
        log(f"store fail error {run_id}: {e}")


def _store_get(run_id):
    """Completed run from disk (cross-restart idempotency + event replay). None if unknown/unfinished."""
    try:
        with DB_LOCK:
            c = _db()
            row = c.execute("SELECT reply, tools, status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            c.close()
        if row and row[2] in ("done", "interrupted"):
            return {"reply": row[0], "tools": json.loads(row[1] or "[]")}
        return None
    except Exception:
        return None


def _bound_for_model(text: str, name: str = "tool", tc_id: str = "") -> str:
    """P0-4: retain full text to .jobs/, send head+tail preview to model (no silent loss)."""
    text = str(text or "")
    if len(text) <= 8000:
        return text
    try:
        jobs = ROOT / ".jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        fname = f"tool-{int(time.time() * 1000)}-{name}.txt"
        # keep tc_id filesystem-safe and short
        safe = "".join(c for c in (tc_id or "") if c.isalnum() or c in ("-", "_"))[:24]
        if safe:
            fname = f"tool-{int(time.time() * 1000)}-{name}-{safe}.txt"
        (jobs / fname).write_text(text)
        preview = text[:4000] + f"\n...[truncated {len(text)} chars total, full in .jobs/{fname} — read it with the read tool]...\n" + text[-4000:]
        return preview
    except Exception:
        # fallback to plain head+tail without spill
        return text[:4000] + f"\n...[truncated {len(text)} chars]...\n" + text[-4000:]


def _is_interrupted(run_id):
    """Esc interrupt flag: checked at Safe Step Boundaries, never mid-tool."""
    if not run_id:
        return False
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        return bool(run and run.get("interrupt"))


def _drain_steer(run_id, messages):
    """P0-2: drain mid-run follow-ups at Safe Step Boundary (between steps, never mid-tool)."""
    drained = []
    if not run_id:
        return drained
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run:
            return drained
        steer = run.get("steer") or []
        run["steer"] = []
        drained = list(steer)
        if drained:
            run["ts"] = time.time()
    for msg in drained:
        messages.append({"role": "user", "content": msg})
        log(f"/api/chat steer delivered run={run_id} msg_len={len(msg)}")
    return drained


def _maybe_compact(client, model, messages, run_id=None, live_logs=None, tool_logs=None):
    """Rolling compaction (v2 §7 minimal): summarize oldest, keep newest 20. No-op when under budget."""

    def _summarize(transcript):
        resp = _chat_create_with_retry(
            client, model,
            [{"role": "system", "content": SUMMARY_SYSTEM},
             {"role": "user", "content": transcript}],
            None, run_id=run_id)
        return (resp.choices[0].message.content or "").strip()

    new_msgs, info = compact_messages(messages, _summarize, keep_recent=20, max_msgs=60, max_chars=80000)
    if info.get("compacted") or info.get("error"):
        messages[:] = new_msgs
        log(f"/api/chat compact run={run_id} {info['old_n']}->{info['new_n']} compacted={info['compacted']} {info.get('error', '')}")
        entry = {"tool": "compact", "args": {"from": info["old_n"], "to": info["new_n"]},
                 "result": f"compacted {info['old_n']}->{info['new_n']}" + (f" ({info['error']})" if info.get("error") else "")}
        if tool_logs is not None:
            tool_logs.append(entry)
        if live_logs is not None:
            with RUNS_LOCK:
                live_logs.append(entry)
                if run_id and run_id in RUNS:
                    RUNS[run_id]["ts"] = time.time()
    return info


def run_agent(messages, model, max_steps=12, live_logs=None, run_id=None):
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    tool_logs = []
    for _ in range(max_steps):
        if _is_interrupted(run_id):
            return ("(interrupted by user)", tool_logs, messages, True)
        _maybe_compact(client, model, messages, run_id, live_logs, tool_logs)
        resp = _chat_create_with_retry(client, model, messages, SERVER_TOOLS_SPECS, run_id=run_id, live_logs=live_logs)
        msg = resp.choices[0].message
        m = {"role": "assistant", "content": msg.content or ""}
        if getattr(msg, "tool_calls", None):
            m["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        messages.append(m)
        if not getattr(msg, "tool_calls", None):
            # P0-2: steered follow-ups extend the same run instead of starting a racy new one
            if _drain_steer(run_id, messages):
                continue
            return (msg.content or "(empty)", tool_logs, messages, False)
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception as e:
                # P0-5: bad JSON is a per-call error, not a run abort
                err = f"tool {name} error: invalid JSON arguments: {e}"
                entry = {"tool": name, "args": {}, "result": err[:2000]}
                tool_logs.append(entry)
                if live_logs is not None:
                    with RUNS_LOCK:
                        live_logs.append(entry)
                        if run_id and run_id in RUNS:
                            RUNS[run_id]["ts"] = time.time()
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": err})
                continue
            if name == "question":
                # never block the HTTP handler on input()
                result = "question tool is disabled in web mode; make your best guess and continue."
            else:
                try:
                    # P0-5: one bad tool must not kill sibling calls
                    result = execute_tool(name, args)
                except Exception as e:
                    result = f"tool {name} error: {e}"
            entry = {"tool": name, "args": args, "result": str(result)[:2000]}
            tool_logs.append(entry)
            if live_logs is not None:
                with RUNS_LOCK:
                    live_logs.append(entry)
                    if run_id and run_id in RUNS:
                        RUNS[run_id]["ts"] = time.time()
            # P0-4: model gets bounded preview, full text retained under .jobs/
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": _bound_for_model(result, name, tc.id)})
        # P0-2: Safe Step Boundary — deliver steers here, before next LLM step
        _drain_steer(run_id, messages)
        if _is_interrupted(run_id):
            return ("(interrupted by user)", tool_logs, messages, True)
    return ("(max tool steps reached)", tool_logs, messages, False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {self.command} {self.path} :: {fmt % args}")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        log(f"-> {self.path} {code} ({len(body)}b)")

    def _serve_event(self, rid):
        """SSE push for one run: replay past logs, stream new ones, close with 'done'. Keeps /api/progress for compat."""
        import time as _t
        with RUNS_LOCK:
            run = RUNS.get(rid)
            snap = list(run["logs"]) if run else None
            done = run.get("done") if run else None
        stored = None
        if run is None:
            stored = _store_get(rid)
        if run is None and stored is None:
            self.send_json({"ok": False, "error": "unknown run"}, 404)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
        except Exception:
            return
        idx = 0
        deadline = _t.time() + 600
        try:
            for entry in (snap if snap is not None else (stored.get("tools") or [])):
                self.wfile.write(f"data: {json.dumps(entry)}\n\n".encode())
                idx += 1
            self.wfile.flush()
            if done or snap is None:
                self.wfile.write(f"event: done\ndata: {json.dumps({'done': True, 'n': idx})}\n\n".encode())
                self.wfile.flush()
                return
            last_beat = _t.time()
            while _t.time() < deadline:
                with RUNS_LOCK:
                    run = RUNS.get(rid)
                    if run is None:
                        break
                    logs = list(run["logs"])
                    is_done = run.get("done")
                while idx < len(logs):
                    self.wfile.write(f"data: {json.dumps(logs[idx])}\n\n".encode())
                    idx += 1
                self.wfile.flush()
                if is_done:
                    break
                if _t.time() - last_beat > 15:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_beat = _t.time()
                _t.sleep(0.2)
            self.wfile.write(f"event: done\ndata: {json.dumps({'done': True, 'n': idx})}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"/api/event error run={rid}: {e}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_text()
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/models":
            live = get_live_models()
            default = DEFAULT_MODEL if DEFAULT_MODEL in live else (live[0] if live else DEFAULT_MODEL)
            self.send_json({"models": live, "default": default, "has_key": bool(API_KEY)})
        elif parsed.path == "/api/system":
            p = ROOT / "system_prompt.md"
            self.send_json({"system": p.read_text() if p.exists() else ""})
        elif parsed.path == "/api/progress":
            q = urllib.parse.parse_qs(parsed.query or "")
            rid = (q.get("run") or [""])[0]
            with RUNS_LOCK:
                # prune runs older than 10 min (keep dict small)
                now = time.time()
                for k in [k for k, v in RUNS.items() if now - v.get("ts", now) > 600]:
                    del RUNS[k]
                run = RUNS.get(rid)
                if not run:
                    self.send_json({"ok": False, "error": "unknown run"}, 404)
                else:
                    self.send_json({"ok": True, "logs": list(run["logs"]),
                                    "done": run["done"], "n": len(run["logs"])})
        elif parsed.path == "/api/event":
            q = urllib.parse.parse_qs(parsed.query or "")
            self._serve_event((q.get("run") or [""])[0])
        elif parsed.path == "/api/session":
            q = urllib.parse.parse_qs(parsed.query or "")
            sid = _safe_sid((q.get("id") or [""])[0])
            if not sid:
                self.send_json({"ok": False, "error": "need id"}, 400)
            else:
                p = SESSIONS_DIR / f"{sid}.jsonl"
                if not p.exists():
                    self.send_json({"ok": True, "id": sid, "history": []})
                else:
                    try:
                        hist = []
                        for line in p.read_text().splitlines()[-120:]:
                            line = line.strip()
                            if line:
                                hist.append(json.loads(line))
                        self.send_json({"ok": True, "id": sid, "history": hist})
                    except Exception as e:
                        self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        if parsed.path == "/api/system":
            try:
                (ROOT / "system_prompt.md").write_text(data.get("system", ""))
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif parsed.path == "/api/chat":
            if not API_KEY:
                self.send_json({"ok": False, "error": "MAXPLUS_API_KEY not set. export MAXPLUS_API_KEY=ccsk-... then restart server.py"}, 400)
                return
            model = data.get("model") or DEFAULT_MODEL
            live = get_live_models()
            if model not in live:
                self.send_json({"ok": False, "error": f"unknown model '{model}'. Valid: {', '.join(live)}"}, 400)
                return
            system = data.get("system") or ""
            history = data.get("history") or []
            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                self.send_json({"ok": False, "error": "empty message"}, 400)
                return
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            # history: [{role, content}] from frontend (user/assistant text only)
            for h in history[-60:]:
                if h.get("role") in ("user", "assistant") and h.get("content"):
                    messages.append({"role": h["role"], "content": h["content"]})
            messages.append({"role": "user", "content": user_msg})
            # P0-1: run/item_id is the idempotency key. Same key resent = cached reply, no new LLM call.
            # Frontend sends both run and item_id as the same rid; accept either.
            run_id = str(data.get("item_id") or data.get("run") or f"run-{int(time.time() * 1000)}")
            delivery = str(data.get("delivery") or "steer")
            with RUNS_LOCK:
                existing = RUNS.get(run_id)
                if existing is not None and existing.get("done") and existing.get("reply") is not None:
                    log(f"/api/chat idempotent hit run={run_id}")
                    # read logs/tools outside lock copy already stored
                    self.send_json({"ok": True, "reply": existing.get("reply"), "tools": list(existing.get("logs", [])), "run": run_id, "deduped": True})
                    return
                if existing is not None and not existing.get("done"):
                    # network retry of in-flight run: don't fork a second LLM loop
                    self.send_json({"ok": False, "error": f"already running run={run_id}", "run": run_id, "running": True}, 409)
                    return
                # cross-restart idempotency: finished runs survive in SQLite
                stored = _store_get(run_id)
                if stored is not None:
                    log(f"/api/chat idempotent hit (store) run={run_id}")
                    self.send_json({"ok": True, "reply": stored.get("reply"), "tools": stored.get("tools", []), "run": run_id, "deduped": True})
                    return
                # P0-2 queue mode: if any run is active, park this message instead of racing
                if delivery == "queue":
                    active = [k for k, v in RUNS.items() if not v.get("done")]
                    if active:
                        RUNS[run_id] = {"logs": [], "done": True, "reply": None, "queued": True, "ts": time.time(),
                                        "message": user_msg, "model": model, "history": history[-60:], "system": system}
                        log(f"/api/chat queued run={run_id} behind={active}")
                        self.send_json({"ok": True, "queued": True, "run": run_id, "behind": active})
                        return
                RUNS[run_id] = {"logs": [], "done": False, "reply": None, "ts": time.time(), "steer": []}
                live = RUNS[run_id]["logs"]
            _claim_start(run_id)
            log(f"/api/chat model={model} run={run_id} msg_len={len(user_msg)} hist={len(history)} sys_len={len(system)}")
            try:
                reply, tool_logs, _, interrupted = run_agent(messages, model, live_logs=live, run_id=run_id)
                with RUNS_LOCK:
                    if run_id in RUNS:
                        RUNS[run_id].update({"done": True, "reply": reply, "ts": time.time()})
                _save_turn(_safe_sid(data.get("session")), user_msg, reply, model, tool_logs)
                _claim_finish(run_id, reply, tool_logs, status="interrupted" if interrupted else "done")
                log(f"/api/chat OK reply_len={len(reply)} tools={[t.get('tool') for t in tool_logs]} interrupted={interrupted}")
                self.send_json({"ok": True, "reply": reply, "tools": tool_logs, "run": run_id, "interrupted": interrupted})
            except Exception as e:
                with RUNS_LOCK:
                    if run_id in RUNS:
                        RUNS[run_id].update({"done": True, "ts": time.time()})
                _claim_fail(run_id)
                log(f"/api/chat ERROR: {e}")
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif parsed.path == "/api/interrupt":
            # Esc interrupt: cooperative flag, run_agent stops at next Safe Step Boundary
            rid = str(data.get("run") or data.get("item_id") or "")
            if not rid:
                self.send_json({"ok": False, "error": "need run"}, 400)
                return
            with RUNS_LOCK:
                run = RUNS.get(rid)
                if not run:
                    self.send_json({"ok": False, "error": "unknown run"}, 404)
                    return
                if run.get("done"):
                    self.send_json({"ok": False, "error": "run already done", "done": True}, 409)
                    return
                run["interrupt"] = True
                run["ts"] = time.time()
            log(f"/api/interrupt run={rid}")
            self.send_json({"ok": True, "run": rid})
        elif parsed.path == "/api/steer":
            # P0-2: mid-run follow-up. Delivered only at Safe Step Boundary inside run_agent.
            rid = str(data.get("run") or data.get("item_id") or "")
            msg = (data.get("message") or "").strip()
            if not rid or not msg:
                self.send_json({"ok": False, "error": "need run + message"}, 400)
                return
            with RUNS_LOCK:
                run = RUNS.get(rid)
                if not run:
                    self.send_json({"ok": False, "error": "unknown run, send as new chat"}, 404)
                    return
                if run.get("done"):
                    self.send_json({"ok": False, "error": "run already done, send as new chat", "done": True}, 409)
                    return
                run.setdefault("steer", []).append(msg)
                run["ts"] = time.time()
                n = len(run["steer"])
            log(f"/api/steer run={rid} queued={n} msg_len={len(msg)}")
            self.send_json({"ok": True, "run": rid, "pending": n})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    _store_init()
    log(f"serving http://localhost:{PORT} (log: {LOG_FILE})")
    log(f"model default={DEFAULT_MODEL} key={'set' if API_KEY else 'MISSING export MAXPLUS_API_KEY=ccsk-...'}")
    # open chatbot automatically (set NO_BROWSER=1 to skip)
    if os.getenv("NO_BROWSER", "") not in ("1", "true"):
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{PORT}")
        except Exception:
            pass
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
