"""Localhost backend for formyproject agent. Stdlib only (+openai pkg).
Run:  export MAXPLUS_API_KEY="ccsk-..."; python3 server.py
Open: http://localhost:8000
Provider: MaxPlus AI, pool chinese-specials (OpenAI-compatible).
Docs: https://maxplus-ai.cc/docs/api
"""
import json
import hmac
import os
import shutil
import subprocess
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

from tools import TOOLS_SPECS, SUMMARY_SYSTEM, compact_messages, execute_tool, run_tool_calls

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
# auth: off by default (localhost, zero friction). Set FORMY_TOKEN to require it
# on every /api/* route — via `Authorization: Bearer <token>` or `?token=`
# (query form exists because EventSource cannot send headers).
FORMY_TOKEN = os.getenv("FORMY_TOKEN", "")
# bind: loopback by default. Non-loopback bind without FORMY_TOKEN refuses to start.
HOST = os.getenv("FORMY_HOST", "127.0.0.1")
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


def _model_chain(primary):
    """Fallback chain (Hermes fallback_providers pattern, minimal).

    FORMY_FALLBACK_MODELS="model-a,model-b" — tried in order when the current
    model fails (429/5xx after retries, or 401/403/404/unknown-model).
    Primary always first, deduped. Empty env = primary only (old behavior).
    """
    extra = [m.strip() for m in os.getenv("FORMY_FALLBACK_MODELS", "").split(",") if m.strip()]
    chain = [primary] + [m for m in extra if m != primary]
    return chain


def _emit_run(run_id, key, value):
    """Append a live SSE payload to a run (token deltas, thinking, step marks). Never raises."""
    if not run_id:
        return
    try:
        with RUNS_LOCK:
            run = RUNS.get(run_id)
            if run is None:
                return
            run.setdefault(key, []).append(value)
            run["ts"] = time.time()
    except Exception:
        pass


try:
    # Web approval cards: dangerous bash in a live run parks a pending card
    # (approval.py) and fans out here for SSE. Never breaks boot.
    import approval as _approval_mod
    _approval_mod.set_emitter(_emit_run)
except Exception:
    pass


def _append_thinking(tool_logs, live_logs, run_id, thinking):
    """Persist one step's reasoning as a thinking tool entry (UI card + export + history)."""
    entry = {"tool": "thinking", "args": {}, "result": (thinking or "")[:2000]}
    tool_logs.append(entry)
    if live_logs is not None:
        try:
            with RUNS_LOCK:
                live_logs.append(entry)
                if run_id and run_id in RUNS:
                    RUNS[run_id]["ts"] = time.time()
        except Exception:
            pass


def _consume_chat_stream(stream, run_id):
    """Consume one OpenAI stream. Returns {content, thinking, tool_calls, interrupted}.

    Token + reasoning deltas are emitted live for SSE. Tool calls accumulate by
    index (id/name/args may arrive across chunks; missing ids are synthesized).
    Interrupt flag is checked per chunk: breaks and closes early. Chunk-shape
    quirks never raise; transport errors propagate for retry handling.
    """
    parts, thinks = [], []
    tc_accum = {}
    interrupted = False
    try:
        for chunk in stream:
            if _is_interrupted(run_id):
                interrupted = True
                break
            try:
                choices = getattr(chunk, "choices", None) or []
            except Exception:
                continue
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            r = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if r:
                thinks.append(r)
                _emit_run(run_id, "thinking", r)
            c = getattr(delta, "content", None)
            if c:
                parts.append(c)
                _emit_run(run_id, "stream", c)
            for tc in (getattr(delta, "tool_calls", None) or []):
                try:
                    idx = int(getattr(tc, "index", 0) or 0)
                except Exception:
                    idx = 0
                e = tc_accum.setdefault(idx, {"id": "", "name": "", "args": []})
                fn = getattr(tc, "function", None)
                if fn is not None:
                    fid = getattr(fn, "id", None)
                    if fid and not e["id"]:
                        e["id"] = fid
                    nm = getattr(fn, "name", None)
                    if nm:
                        e["name"] = nm
                    ag = getattr(fn, "arguments", None)
                    if ag:
                        e["args"].append(ag)
                tid = getattr(tc, "id", None)
                if tid and not e["id"]:
                    e["id"] = tid
    finally:
        try:
            stream.close()
        except Exception:
            pass
    calls = []
    for i in sorted(tc_accum):
        e = tc_accum[i]
        calls.append({"id": e["id"] or f"tc-{run_id}-{i}", "type": "function",
                      "function": {"name": e["name"], "arguments": "".join(e["args"])}})
    return {"content": "".join(parts), "thinking": "".join(thinks),
            "tool_calls": calls, "interrupted": interrupted}


def _chat_stream_with_retry(client, model, messages, tools, run_id=None, live_logs=None):
    """Streaming chat call: initial request + max 4 restarts, jittered 1/2/4/8s backoff.

    Creation failures retry like the non-streaming path. A mid-stream transport
    failure emits a restart mark (frontend clears the live bubble) and rebuilds
    the stream — safe because tools only run after a step fully streams.
    Returns _consume_chat_stream dict. Raises last error for fallback handling.
    """
    import random
    last = None
    for attempt in range(5):
        try:
            kwargs = {"model": model, "messages": messages, "stream": True}
            if tools is not None:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            stream = client.chat.completions.create(**kwargs)
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
            continue
        try:
            return _consume_chat_stream(stream, run_id)
        except Exception as e:
            last = e
            if attempt >= 4 or not _is_retryable(e):
                raise
            delay = (2 ** attempt) + random.random() * 0.5
            log(f"/api/chat stream.restart run={run_id} attempt={attempt + 1}/4 delay={delay:.1f}s err={str(e)[:200]}")
            _emit_run(run_id, "marks", {"restart": attempt + 1})
            time.sleep(delay)
            continue
    raise last


def _chat_create_with_fallback(client, primary, messages, tools=None, run_id=None, live_logs=None):
    """Try each model in _model_chain with token streaming; return (result, used_model).

    result = {content, thinking, tool_calls, interrupted}. A model switch emits
    a restart mark (frontend clears the partial bubble). Raises last error.
    Non-streaming callers (_maybe_compact summarizer) still use
    _chat_create_with_retry directly.
    """
    last = None
    chain = _model_chain(primary)
    for i, model in enumerate(chain):
        try:
            return _chat_stream_with_retry(client, model, messages, tools, run_id=run_id, live_logs=live_logs), model
        except Exception as e:
            last = e
            if i >= len(chain) - 1:
                raise
            nxt = chain[i + 1]
            log(f"/api/chat fallback.switch run={run_id} {model} -> {nxt} err={str(e)[:150]}")
            if live_logs is not None:
                with RUNS_LOCK:
                    live_logs.append({"tool": "fallback", "args": {"from": model, "to": nxt},
                                      "result": f"switched model after error: {str(e)[:300]}"})
                    if run_id and run_id in RUNS:
                        RUNS[run_id]["ts"] = time.time()
            _emit_run(run_id, "marks", {"restart": 0, "fallback": nxt})
    raise last


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
    # Glow-up: mirror into SQLite messages for session_search. Best-effort —
    # jsonl above is source of truth; DB failure must never break the turn.
    try:
        with DB_LOCK:
            c = _db()
            ts2 = time.time()
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                      (sid, "user", user_msg, ts2))
            tool_names = ",".join(t.get("tool", "") for t in (tool_logs or []))[:500]
            c.execute("INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
                      (sid, "assistant", reply, tool_names, time.time()))
            # Bound the table: keep newest 20k rows (runs pruned at 24h in
            # _store_init; messages had no cap and grew unbounded).
            c.execute("DELETE FROM messages WHERE id NOT IN "
                      "(SELECT id FROM messages ORDER BY id DESC LIMIT 20000)")
            c.commit()
            c.close()
    except Exception as e:
        log(f"messages mirror error {sid}: {e}")
    # FTS orphan sweep (no-FTS5 builds: table missing — must never roll back
    # the inserts above, so this runs in its own transaction).
    try:
        with DB_LOCK:
            c = _db()
            c.execute("DELETE FROM messages_fts WHERE rowid NOT IN (SELECT id FROM messages)")
            c.commit()
            c.close()
    except Exception:
        pass


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
    # Glow-up: message history for session_search (Hermes messages pattern, minimal).
    # Additive only — runs table untouched. FTS5 optional (LIKE fallback in tool).
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
      role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp)")
    try:
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
          content, tool_name, content='messages', content_rowid='id')""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, content, tool_name)
          VALUES (new.id, new.content, new.tool_name); END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, content, tool_name)
          VALUES ('delete', old.id, old.content, old.tool_name); END""")
    except Exception:
        pass  # FTS5 missing on this build — session_search falls back to LIKE
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


def _strip_compact_echo(messages, reply):
    """Drop compacted-summary text the model echoed back into its reply.

    Compaction inserts a SUMMARY_PREFIX system message; models sometimes
    parrot it (prefix and/or summary body) into the final response, which
    then persists via _save_turn and gets re-fed as history — duplicates
    grow every turn. Strips the prefix block and a leading echo of any
    known summary body. Never raises; returns reply unchanged on doubt.
    """
    try:
        from tools import SUMMARY_PREFIX as _PREFIX
    except Exception:
        return reply
    try:
        if not reply:
            return reply
        out = str(reply)
        if _PREFIX in out:
            # cut the echoed prefix block (prefix line + following summary paragraph)
            parts = out.split(_PREFIX)
            head = parts[0]
            for tail in parts[1:]:
                nl = tail.find("\n\n")
                tail = tail[nl + 2:] if nl != -1 else ""
                head += tail
            out = head
        # leading echo of a summary body: compare against each summary in history
        bodies = []
        for m in (messages or []):
            c = str(m.get("content") or "")
            if c.startswith(_PREFIX) and len(c) > len(_PREFIX) + 100:
                bodies.append(c[len(_PREFIX):].strip())
        norm = lambda s: " ".join(str(s).split())
        needle = norm(out)[:400]
        for b in bodies:
            bn = norm(b)[:400]
            if bn and (needle.startswith(bn[:200]) or bn[:200] in needle[:400]):
                # strip first occurrence of the echoed body
                idx = out.find(b[:200])
                if idx != -1:
                    out = (out[:idx] + out[idx + len(b):])
                break
        out = out.strip()
        return out if out else reply
    except Exception:
        return reply


def _execute_chat(run_id, model, system, history, user_msg, session_id):
    """Run one chat turn to completion: agent loop + persist + claims.

    Shared core for blocking POST /api/chat and background /api/chat/start.
    Records outcome (reply/tools/interrupted/error) in RUNS for SSE replay
    and GET /api/chat/result. Never raises.
    """
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        # history: [{role, content}] from frontend (user/assistant text only)
        for h in history[-60:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_msg})
        with RUNS_LOCK:
            live = RUNS[run_id]["logs"] if run_id in RUNS else None
        _claim_start(run_id)
        log(f"/api/chat model={model} run={run_id} msg_len={len(user_msg)} hist={len(history)} sys_len={len(system)}")
        try:
            reply, tool_logs, _, interrupted = run_agent(messages, model, live_logs=live, run_id=run_id)
            with RUNS_LOCK:
                if run_id in RUNS:
                    RUNS[run_id].update({"done": True, "reply": reply, "tools": tool_logs,
                                         "interrupted": interrupted, "ts": time.time()})
            _save_turn(session_id, user_msg, reply, model, tool_logs)
            _claim_finish(run_id, reply, tool_logs, status="interrupted" if interrupted else "done")
            log(f"/api/chat OK reply_len={len(reply)} tools={[t.get('tool') for t in tool_logs]} interrupted={interrupted}")
        except Exception as e:
            with RUNS_LOCK:
                if run_id in RUNS:
                    RUNS[run_id].update({"done": True, "error": str(e), "ts": time.time()})
            _claim_fail(run_id)
            log(f"/api/chat ERROR: {e}")
    except Exception as e:
        try:
            with RUNS_LOCK:
                if run_id in RUNS:
                    RUNS[run_id].update({"done": True, "error": str(e), "ts": time.time()})
            _claim_fail(run_id)
        except Exception:
            pass
        log(f"/api/chat SETUP ERROR run={run_id}: {e}")


def _chat_admit(data):
    """Shared admission for /api/chat + /api/chat/start.

    Returns ("respond", (code, payload)) when the request is answered
    immediately (validation error, idempotent dedup hit, already-running,
    queued), else ("fresh", req-dict) for the caller to execute.
    """
    if not API_KEY:
        return ("respond", (400, {"ok": False, "error": "MAXPLUS_API_KEY not set. export MAXPLUS_API_KEY=ccsk-... then restart server.py"}))
    model = data.get("model") or DEFAULT_MODEL
    live = get_live_models()
    if model not in live:
        return ("respond", (400, {"ok": False, "error": f"unknown model '{model}'. Valid: {', '.join(live)}"}))
    system = data.get("system") or ""
    # Glow-up: assemble SOUL + AGENTS + memory snapshot around UI text.
    # Falls back to raw UI text if prompt.py missing — UI contract unchanged.
    try:
        from prompt import build_system_prompt as _build_sys
        system = _build_sys(system)
    except Exception:
        pass
    history = data.get("history") or []
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return ("respond", (400, {"ok": False, "error": "empty message"}))
    # P0-1: run/item_id is the idempotency key. Same key resent = cached reply, no new LLM call.
    # Frontend sends both run and item_id as the same rid; accept either.
    run_id = str(data.get("item_id") or data.get("run") or f"run-{int(time.time() * 1000)}")
    delivery = str(data.get("delivery") or "steer")
    session_id = _safe_sid(data.get("session"))
    with RUNS_LOCK:
        existing = RUNS.get(run_id)
        if existing is not None and existing.get("done") and existing.get("reply") is not None:
            log(f"/api/chat idempotent hit run={run_id}")
            return ("respond", (200, {"ok": True, "reply": existing.get("reply"),
                                      "tools": list(existing.get("tools", existing.get("logs", []))),
                                      "run": run_id, "deduped": True,
                                      "interrupted": existing.get("interrupted", False)}))
        if existing is not None and not existing.get("done"):
            # network retry of in-flight run: don't fork a second LLM loop
            return ("respond", (409, {"ok": False, "error": f"already running run={run_id}",
                                      "run": run_id, "running": True}))
        # cross-restart idempotency: finished runs survive in SQLite
        stored = _store_get(run_id)
        if stored is not None:
            log(f"/api/chat idempotent hit (store) run={run_id}")
            return ("respond", (200, {"ok": True, "reply": stored.get("reply"),
                                      "tools": stored.get("tools", []), "run": run_id, "deduped": True}))
        # P0-2 queue mode: if any run is active, park this message instead of racing
        if delivery == "queue":
            active = [k for k, v in RUNS.items() if not v.get("done")]
            if active:
                RUNS[run_id] = {"logs": [], "done": True, "reply": None, "queued": True, "ts": time.time(),
                                "message": user_msg, "model": model, "history": history[-60:], "system": system,
                                "tools": [], "interrupted": False, "sid": session_id}
                log(f"/api/chat queued run={run_id} behind={active}")
                return ("respond", (200, {"ok": True, "queued": True, "run": run_id, "behind": active}))
        RUNS[run_id] = {"logs": [], "done": False, "reply": None, "ts": time.time(), "steer": [],
                        "stream": [], "thinking": [], "marks": [], "tools": [],
                        "interrupted": False, "error": None, "sid": session_id}
    return ("fresh", {"run_id": run_id, "model": model, "system": system,
                      "history": history, "user_msg": user_msg, "session_id": session_id})


def _ws_state_file():
    return SESSIONS_DIR / "workspaces.json"


def _ws_state():
    """Spaces-lite state: {active, roots}. Server home files stay at ROOT."""
    st = {"active": str(ROOT), "roots": [str(ROOT)]}
    try:
        p = _ws_state_file()
        if p.exists():
            d = json.loads(p.read_text() or "{}")
            if isinstance(d, dict):
                if isinstance(d.get("roots"), list):
                    st["roots"] = [str(x) for x in d["roots"] if x]
                if d.get("active"):
                    st["active"] = str(d["active"])
    except Exception:
        pass
    if str(ROOT) not in st["roots"]:
        st["roots"].insert(0, str(ROOT))
    return st


def _ws_save(st):
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        _ws_state_file().write_text(json.dumps(st, indent=1))
    except Exception:
        pass


def _ws_active():
    """Active workspace root. Falls back to ROOT if missing. Never raises."""
    try:
        a = Path(_ws_state().get("active") or str(ROOT))
        if a.is_dir():
            return a
    except Exception:
        pass
    return ROOT


def _ws_git():
    """Branch + dirty count for the workspace header. Never raises."""
    try:
        wr = _ws_active()
        b = subprocess.run(["git", "-C", str(wr), "branch", "--show-current"],
                           capture_output=True, text=True, timeout=5)
        if b.returncode != 0:
            return None
        s = subprocess.run(["git", "-C", str(wr), "status", "--porcelain=v1"],
                           capture_output=True, text=True, timeout=5)
        dirty = len([l for l in s.stdout.splitlines() if l.strip()]) if s.returncode == 0 else 0
        return {"branch": b.stdout.strip() or "(detached)", "dirty": dirty}
    except Exception:
        return None


def _ws_path(rel):
    """Resolve rel under the active workspace. Returns (Path, error). Never raises."""
    try:
        rel = (rel or "").strip().lstrip("/")
        if rel in (".", "./"):
            rel = ""
        wr = _ws_active()
        p = (wr / rel).resolve()
        root = wr.resolve()
        if p != root and root not in p.parents:
            return None, "outside workspace"
        return p, None
    except Exception as e:
        return None, str(e)[:100]


def _ws_rel(p):
    """Path relative to the active workspace as posix string. Never raises."""
    try:
        return p.resolve().relative_to(_ws_active().resolve()).as_posix()
    except Exception:
        return ""


def _git(*args):
    """Run git -C <active workspace>. Returns CompletedProcess or None. Never raises."""
    try:
        return subprocess.run(["git", "-C", str(_ws_active()), *args],
                              capture_output=True, text=True, timeout=15)
    except Exception:
        return None


def _cp_file():
    from tools import JOBS_DIR as _jd
    return _jd / "checkpoints.json"


def _cp_load():
    try:
        p = _cp_file()
        if p.exists():
            data = json.loads(p.read_text() or "[]")
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _cp_save(entries):
    try:
        p = _cp_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entries[-20:], indent=1))
    except Exception:
        pass


TERMS = {}
TERMS_LOCK = threading.Lock()
TERM_MAX = 4
TERM_BUF_CAP = 200_000


def _term_kill(tid):
    """Close a terminal and reap its child. Never raises."""
    try:
        t = None
        with TERMS_LOCK:
            t = TERMS.pop(tid, None)
        if not t:
            return
        try:
            os.close(t["fd"])
        except Exception:
            pass
        try:
            os.kill(t["pid"], 9)
        except Exception:
            pass
        try:
            os.waitpid(t["pid"], os.WNOHANG)
        except Exception:
            pass
        log(f"/api/term kill {tid}")
    except Exception:
        pass


def _term_reader(tid, fd):
    """Pump pty master output into the term buffer. Ends with the child."""
    import select as _select
    try:
        while True:
            try:
                r, _, _ = _select.select([fd], [], [], 5)
            except Exception:
                break
            if not r:
                with TERMS_LOCK:
                    if tid not in TERMS:
                        break
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            with TERMS_LOCK:
                t = TERMS.get(tid)
                if not t:
                    break
                t["buf"] += chunk
                if len(t["buf"]) > TERM_BUF_CAP:
                    t["buf"] = t["buf"][-TERM_BUF_CAP:]
                    t["base"] = t.get("base", 0)
                t["active"] = time.time()
    finally:
        with TERMS_LOCK:
            t = TERMS.get(tid)
            if t:
                t["dead"] = True


def _term_create(cols=100, rows=30):
    """Fork a real shell on a pty, cwd=active workspace. Returns (tid, error)."""
    import pty as _pty
    import fcntl as _fcntl
    try:
        with TERMS_LOCK:
            now = time.time()
            for tid in [k for k, t in TERMS.items() if now - t.get("active", now) > 1800]:
                _term_kill(tid)
            if len(TERMS) >= TERM_MAX:
                return None, f"too many terminals (max {TERM_MAX})"
        pid, fd = _pty.fork()
        if pid == 0:
            try:
                os.chdir(str(_ws_active()))
                os.environ["TERM"] = "xterm-256color"
                os.environ["PS1"] = "$ "
                os.execvp("bash", ["bash", "--noprofile", "--norc", "-i"])
            except Exception:
                os._exit(1)
        try:
            fl = _fcntl.fcntl(fd, _fcntl.F_GETFL)
            _fcntl.fcntl(fd, _fcntl.F_SETFL, fl | os.O_NONBLOCK)
            import termios as _termios
            import struct as _struct
            _fcntl.ioctl(fd, _termios.TIOCSWINSZ,
                         _struct.pack("HHHH", max(5, rows), max(20, cols), 0, 0))
        except Exception:
            pass
        tid = f"t{int(time.time() * 1000)}"
        with TERMS_LOCK:
            TERMS[tid] = {"pid": pid, "fd": fd, "buf": b"", "base": 0,
                          "active": time.time(), "dead": False}
        th = threading.Thread(target=_term_reader, args=(tid, fd),
                              name=f"term-{tid}", daemon=True)
        th.start()
        log(f"/api/term create {tid} pid={pid}")
        return tid, None
    except Exception as e:
        return None, str(e)[:200]


def run_agent(messages, model, max_steps=12, live_logs=None, run_id=None):
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    tool_logs = []
    cur_model = model  # fallback may switch mid-run; rest of run stays on working model
    for step_n in range(max_steps):
        if _is_interrupted(run_id):
            return ("(interrupted by user)", tool_logs, messages, True)
        _maybe_compact(client, cur_model, messages, run_id, live_logs, tool_logs)
        if step_n > 0:
            _emit_run(run_id, "marks", {"step": step_n + 1})
        res, cur_model = _chat_create_with_fallback(client, cur_model, messages, SERVER_TOOLS_SPECS, run_id=run_id, live_logs=live_logs)
        if res.get("interrupted"):
            # Esc mid-generation: keep the partial text so the UI shows what
            # arrived before the stop instead of swallowing it.
            tail = (res.get("content") or "").strip()
            reply = (tail + "\n\n*(interrupted by user)*") if tail else "(interrupted by user)"
            if res.get("thinking"):
                _append_thinking(tool_logs, live_logs, run_id, res["thinking"])
            return (_strip_compact_echo(messages, reply), tool_logs, messages, True)
        tcalls = res.get("tool_calls") or []
        m = {"role": "assistant", "content": res.get("content") or ""}
        if tcalls:
            m["tool_calls"] = tcalls
        messages.append(m)
        if res.get("thinking"):
            _append_thinking(tool_logs, live_logs, run_id, res["thinking"])
        if not tcalls:
            # P0-2: steered follow-ups extend the same run instead of starting a racy new one
            if _drain_steer(run_id, messages):
                continue
            return (_strip_compact_echo(messages, m["content"] or "(empty)"), tool_logs, messages, False)
        # Parse args sequentially (cheap), execute concurrently (Hermes pattern).
        # Bad JSON stays a per-call error (P0-5); question stays disabled.
        # Responses appended in tool_calls order (providers validate sequence).
        inline = {}
        pending = []
        for tc in tcalls:
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "?")
            tc_id = tc.get("id", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception as e:
                # P0-5: bad JSON is a per-call error, not a run abort
                err = f"tool {name} error: invalid JSON arguments: {e}"
                inline[tc_id] = ({"tool": name, "args": {}, "result": err[:2000]}, err)
                continue
            if name == "question":
                # never block the HTTP handler on input()
                result = "question tool is disabled in web mode; make your best guess and continue."
                inline[tc_id] = ({"tool": name, "args": args, "result": result[:2000]}, result)
                continue
            pending.append((tc_id, name, args))
        # Web approval context: live runs park cards (approval.py), cron/subagent
        # runs have no RUNS entry and keep the old deny-only behavior.
        with RUNS_LOCK:
            _rr = RUNS.get(run_id) if run_id else None
        _ctx = {"run_id": run_id, "sid": (_rr.get("sid") or "")} if _rr is not None else None
        for (tc_id, name, _args), (entry, result) in zip(pending, run_tool_calls(pending, _ctx)):
            # P0-4: model gets bounded preview, full text retained under .jobs/
            inline[tc_id] = (entry, _bound_for_model(result, name, tc_id))
        for tc in tcalls:
            entry, content = inline[tc.get("id", "")]
            tool_logs.append(entry)
            if live_logs is not None:
                with RUNS_LOCK:
                    live_logs.append(entry)
                    if run_id and run_id in RUNS:
                        RUNS[run_id]["ts"] = time.time()
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": content})
        # P0-2: Safe Step Boundary — deliver steers here, before next LLM step
        _drain_steer(run_id, messages)
        if _is_interrupted(run_id):
            return ("(interrupted by user)", tool_logs, messages, True)
    return (_strip_compact_echo(messages, "(max tool steps reached)"), tool_logs, messages, False)


def _authorized(handler, parsed) -> bool:
    """True if the request may use /api/*. Open when FORMY_TOKEN is unset."""
    if not FORMY_TOKEN:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), FORMY_TOKEN):
        return True
    q = urllib.parse.parse_qs(parsed.query or "")
    tok = (q.get("token") or [""])[0]
    return bool(tok) and hmac.compare_digest(tok, FORMY_TOKEN)


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

    def _emit_result(self, rid, stored=None):
        """Write one `result` event (final reply/tools) when known.

        Lets two-phase clients resolve the turn from the stream alone;
        reattached browsers get it on replay even after the POST is gone.
        Best-effort: transport errors propagate to the caller's handler.
        """
        payload = None
        with RUNS_LOCK:
            run = RUNS.get(rid)
            if run is not None and run.get("done") and run.get("reply") is not None:
                payload = {"reply": run.get("reply"), "tools": run.get("tools", []),
                           "interrupted": run.get("interrupted", False)}
        if payload is None and stored is not None and stored.get("reply") is not None:
            payload = {"reply": stored.get("reply"), "tools": stored.get("tools", []),
                       "interrupted": False, "deduped": True}
        if payload is not None:
            self.wfile.write(f"event: result\ndata: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

    def _serve_event(self, rid):
        """SSE push for one run: replay past logs, stream new ones, close with 'done'. Keeps /api/progress for compat."""
        import time as _t
        with RUNS_LOCK:
            run = RUNS.get(rid)
            snap = list(run["logs"]) if run else None
            done = run.get("done") if run else None
        stored = None
        if run is None:
            # race: browser opens the stream just before POST /api/chat creates the run — wait for it
            deadline = _t.time() + 10
            while run is None and _t.time() < deadline:
                _t.sleep(0.2)
                with RUNS_LOCK:
                    run = RUNS.get(rid)
            if run is not None:
                snap = list(run["logs"])
                done = run.get("done")
            else:
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
        # live token/thinking channels: late joiners get one catch-up snapshot,
        # then deltas. Step marks are live-only (no replay — one bubble suffices).
        with RUNS_LOCK:
            _live = RUNS.get(rid)
            s_full = "".join(_live.get("stream", []) or []) if _live else ""
            t_full = "".join(_live.get("thinking", []) or []) if _live else ""
            s_idx = len(_live.get("stream", []) or []) if _live else 0
            t_idx = len(_live.get("thinking", []) or []) if _live else 0
            m_idx = len(_live.get("marks", []) or []) if _live else 0
            a_idx = len(_live.get("approval", []) or []) if _live else 0
        try:
            if s_full:
                self.wfile.write(f"event: stream_full\ndata: {json.dumps({'t': s_full})}\n\n".encode())
            if t_full:
                self.wfile.write(f"event: think_full\ndata: {json.dumps({'t': t_full})}\n\n".encode())
            try:
                # Reattach replay: a card parked before ES connected.
                from approval import pending_for as _pending_for
                _pa = _pending_for(rid)
                if _pa is not None:
                    self.wfile.write(f"event: approval\ndata: {json.dumps(_pa)}\n\n".encode())
            except Exception:
                pass
            for entry in (snap if snap is not None else (stored.get("tools") or [])):
                self.wfile.write(f"data: {json.dumps(entry)}\n\n".encode())
                idx += 1
            self.wfile.flush()
            if done or snap is None:
                try:
                    self._emit_result(rid, stored)
                except (BrokenPipeError, ConnectionResetError):
                    return
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
                    s_new = list(run.get("stream", []))
                    t_new = list(run.get("thinking", []))
                    m_new = list(run.get("marks", []))
                    a_new = list(run.get("approval", []))
                while s_idx < len(s_new):
                    self.wfile.write(f"event: stream\ndata: {json.dumps({'t': s_new[s_idx]})}\n\n".encode())
                    s_idx += 1
                while t_idx < len(t_new):
                    self.wfile.write(f"event: think\ndata: {json.dumps({'t': t_new[t_idx]})}\n\n".encode())
                    t_idx += 1
                while a_idx < len(a_new):
                    self.wfile.write(f"event: approval\ndata: {json.dumps(a_new[a_idx])}\n\n".encode())
                    a_idx += 1
                while m_idx < len(m_new):
                    self.wfile.write(f"event: mark\ndata: {json.dumps(m_new[m_idx])}\n\n".encode())
                    m_idx += 1
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
            try:
                self._emit_result(rid)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.wfile.write(f"event: done\ndata: {json.dumps({'done': True, 'n': idx})}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"/api/event error run={rid}: {e}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and not _authorized(self, parsed):
            self.send_json({"ok": False, "error": "unauthorized (server FORMY_TOKEN is set)"}, 401)
            return
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
        elif parsed.path == "/api/chat/result":
            # Outcome poll for two-phase runs (backstop behind the SSE result
            # event). Reattach-safe: falls back to SQLite after RUNS eviction.
            q = urllib.parse.parse_qs(parsed.query or "")
            rid = (q.get("run") or [""])[0]
            if not rid:
                self.send_json({"ok": False, "error": "need run"}, 400)
            else:
                with RUNS_LOCK:
                    run = dict(RUNS.get(rid) or {}) if rid in RUNS else None
                if run is not None:
                    if run.get("queued"):
                        self.send_json({"ok": True, "run": rid, "queued": True, "done": True})
                    elif not run.get("done"):
                        self.send_json({"ok": True, "run": rid, "done": False})
                    elif run.get("reply") is None:
                        self.send_json({"ok": True, "run": rid, "done": True,
                                        "error": run.get("error") or "run failed"})
                    else:
                        self.send_json({"ok": True, "run": rid, "done": True,
                                        "reply": run.get("reply"), "tools": run.get("tools", []),
                                        "interrupted": run.get("interrupted", False)})
                    return
                stored = _store_get(rid)
                if stored is not None:
                    self.send_json({"ok": True, "run": rid, "done": True,
                                    "reply": stored.get("reply"),
                                    "tools": stored.get("tools", []), "deduped": True})
                else:
                    self.send_json({"ok": False, "error": "unknown run"}, 404)
        elif parsed.path == "/api/approval/pending":
            q = urllib.parse.parse_qs(parsed.query or "")
            rid = (q.get("run") or [""])[0]
            if not rid:
                self.send_json({"ok": False, "error": "need run"}, 400)
            else:
                try:
                    from approval import pending_for as _pending_for
                    p = _pending_for(rid)
                except Exception:
                    p = None
                self.send_json({"ok": True, "run": rid, "pending": p})
        elif parsed.path == "/api/skills":
            # Slash-command autocomplete: agent-written skills (skills/<name>/SKILL.md).
            # Read-only, traversal-safe (iterate dirs only, no user input).
            out = []
            try:
                sdir = ROOT / "skills"
                if sdir.is_dir():
                    for d in sorted(sdir.iterdir()):
                        if not d.is_dir():
                            continue
                        f = d / "SKILL.md"
                        if not f.exists():
                            continue
                        desc = ""
                        try:
                            for line in f.read_text().splitlines():
                                line = line.strip().lstrip("# ").strip()
                                if line:
                                    desc = line[:120]
                                    break
                        except Exception:
                            pass
                        out.append({"name": d.name, "description": desc})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
                return
            self.send_json({"ok": True, "skills": out})
        elif parsed.path == "/api/files":
            # Workspace browser: list a directory under the active workspace (containment-checked).
            q = urllib.parse.parse_qs(parsed.query or "")
            rel = (q.get("path") or [""])[0]
            p, err = _ws_path(rel)
            if err or not p.is_dir():
                self.send_json({"ok": False, "error": err or "not a directory"}, 400)
                return
            entries = []
            try:
                items = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                for e in items[:2000]:
                    try:
                        st = e.stat()
                        entries.append({"name": e.name, "type": "dir" if e.is_dir() else "file",
                                        "size": st.st_size if e.is_file() else 0,
                                        "mtime": st.st_mtime})
                    except Exception:
                        continue
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)[:200]}, 500)
                return
            git = _ws_git() if not rel else None
            self.send_json({"ok": True, "path": _ws_rel(p), "entries": entries, "git": git})
        elif parsed.path == "/api/workspaces":
            # Spaces-lite: list known project roots + active one.
            st = _ws_state()
            self.send_json({"ok": True, "active": st["active"], "roots": st["roots"],
                            "home": str(ROOT)})
        elif parsed.path == "/api/file":
            # Workspace browser: read one file (text preview or binary flag).
            q = urllib.parse.parse_qs(parsed.query or "")
            rel = (q.get("path") or [""])[0]
            p, err = _ws_path(rel)
            if err or not p.is_file():
                self.send_json({"ok": False, "error": err or "not a file"}, 400)
                return
            try:
                size = p.stat().st_size
                raw = p.read_bytes()[:512_000]
                if b"\0" in raw[:8192]:
                    ext = p.suffix.lower()
                    self.send_json({"ok": True, "path": _ws_rel(p), "binary": True, "size": size,
                                    "image": ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")})
                else:
                    text = raw.decode("utf-8", errors="replace")
                    trunc = len(text) == 512_000 or size > 512_000
                    self.send_json({"ok": True, "path": _ws_rel(p), "text": text[:200_000],
                                    "truncated": trunc, "size": size})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)[:200]}, 500)
        elif parsed.path == "/api/raw":
            # Workspace browser: raw bytes for image preview / download.
            q = urllib.parse.parse_qs(parsed.query or "")
            rel = (q.get("path") or [""])[0]
            p, err = _ws_path(rel)
            if err or not p.is_file():
                self.send_response(404)
                self.end_headers()
                return
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
                     ".pdf": "application/pdf"}.get(p.suffix.lower(), "application/octet-stream")
            try:
                raw = p.read_bytes()
                if len(raw) > 20_000_000:
                    self.send_response(413)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if (q.get("download") or [""])[0]:
                    self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                try:
                    self.send_response(500)
                    self.end_headers()
                except Exception:
                    pass
        elif parsed.path == "/api/term":
            # Terminal poll: bytes since offset (client tracks total).
            q = urllib.parse.parse_qs(parsed.query or "")
            tid = (q.get("poll") or [""])[0]
            try:
                off = max(0, int((q.get("offset") or ["0"])[0]))
            except ValueError:
                off = 0
            with TERMS_LOCK:
                t = TERMS.get(tid)
                if not t:
                    self.send_json({"ok": False, "error": "no such terminal"}, 404)
                    return
                buf, dead = t["buf"], t.get("dead", False)
            if off > len(buf):
                off = 0
            chunk = buf[off:off + 100_000]
            self.send_json({"ok": True, "id": tid,
                            "text": chunk.decode("utf-8", errors="replace"),
                            "total": len(buf), "alive": not dead})
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
        if parsed.path.startswith("/api/") and not _authorized(self, parsed):
            self.send_json({"ok": False, "error": "unauthorized (server FORMY_TOKEN is set)"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > 10_000_000:
            self.send_json({"ok": False, "error": "body too large (max 10MB)"}, 413)
            return
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
            # Blocking compat path: same admission + core as /api/chat/start,
            # but waits for completion before responding.
            action, obj = _chat_admit(data)
            if action == "respond":
                code, payload = obj
                self.send_json(payload, code)
                return
            _execute_chat(**obj)
            with RUNS_LOCK:
                run = dict(RUNS.get(obj["run_id"]) or {})
            if run.get("reply") is None:
                self.send_json({"ok": False, "error": run.get("error") or "run failed",
                                "run": obj["run_id"]}, 500)
                return
            self.send_json({"ok": True, "reply": run.get("reply"), "tools": run.get("tools", []),
                            "run": obj["run_id"], "interrupted": run.get("interrupted", False)})
        elif parsed.path == "/api/chat/start":
            # Two-phase (Hermes pattern): admit, spawn the turn in background,
            # return immediately. Browser streams via /api/event (15s heartbeat,
            # replayable) and fetches the outcome via GET /api/chat/result —
            # reattach-safe on flaky wifi/sleep-wake, unlike the blocking POST.
            action, obj = _chat_admit(data)
            if action == "respond":
                code, payload = obj
                self.send_json(payload, code)
                return
            t = threading.Thread(target=_execute_chat, kwargs=dict(obj),
                                 name=f"chat-{obj['run_id']}", daemon=True)
            t.start()
            self.send_json({"ok": True, "run": obj["run_id"], "started": True}, 202)
        elif parsed.path == "/api/approval/respond":
            # Resolve a parked approval card (once/session/always/deny).
            rid = str(data.get("run") or data.get("item_id") or "")
            choice = str(data.get("choice") or "")
            if not rid:
                self.send_json({"ok": False, "error": "need run"}, 400)
                return
            try:
                from approval import respond as _approval_respond
                ok, msg = _approval_respond(rid, choice)
            except Exception as e:
                ok, msg = False, str(e)
            log(f"/api/approval/respond run={rid} choice={choice} ok={ok}")
            self.send_json({"ok": ok, "run": rid, "choice": choice,
                            "error": None if ok else msg}, 200 if ok else 404)
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
        elif parsed.path == "/api/compact":
            # /compact slash command: summarize this session's history into one
            # assistant message so the chat can continue on a small budget.
            if not API_KEY:
                self.send_json({"ok": False, "error": "MAXPLUS_API_KEY not set"}, 400)
                return
            hist = data.get("history") or []
            focus = str(data.get("focus") or "").strip()
            model = data.get("model") or DEFAULT_MODEL
            if model not in get_live_models():
                model = DEFAULT_MODEL
            lines = []
            for h in hist[-60:]:
                if h.get("role") in ("user", "assistant") and h.get("content"):
                    lines.append(f"{h['role']}: {h['content']}"[:2000])
            transcript = "\n".join(lines)[-30000:]
            if not transcript.strip():
                self.send_json({"ok": False, "error": "nothing to compact"}, 400)
                return
            prompt = "Summarize this conversation for continued work."
            if focus:
                prompt += f" Focus on: {focus[:500]}"
            prompt += "\n\n" + transcript
            try:
                client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
                resp = _chat_create_with_retry(
                    client, model,
                    [{"role": "system", "content": SUMMARY_SYSTEM},
                     {"role": "user", "content": prompt}])
                summary = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)[:300]}, 500)
                return
            log(f"/api/compact turns={len(lines)} summary_len={len(summary)}")
            self.send_json({"ok": True, "summary": summary, "turns": len(lines)})
        elif parsed.path in ("/api/file", "/api/mkdir", "/api/delete", "/api/rename"):
            # Workspace browser writes. Containment-checked + sensitive-path guard
            # (same rule as the agent's write/edit tools; FORMY_FILE_SCOPE=allow bypasses).
            try:
                from tools import _sensitive_path as _ws_sensitive
            except Exception:
                _ws_sensitive = lambda p: None
            if parsed.path == "/api/file":
                rel, content = str(data.get("path") or ""), data.get("content")
                if not isinstance(content, str) or len(content) > 1_000_000:
                    self.send_json({"ok": False, "error": "content must be text under 1MB"}, 400)
                    return
                p, err = _ws_path(rel)
                if err or not rel:
                    self.send_json({"ok": False, "error": err or "need path"}, 400)
                    return
                blocked = _ws_sensitive(str(p))
                if blocked:
                    self.send_json({"ok": False, "error": f"write blocked: {blocked}"}, 403)
                    return
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
                    log(f"/api/file write {_ws_rel(p)} {len(content)} chars")
                    self.send_json({"ok": True, "path": _ws_rel(p), "size": len(content)})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)[:200]}, 500)
            elif parsed.path == "/api/mkdir":
                p, err = _ws_path(str(data.get("path") or ""))
                if err or not str(data.get("path") or "").strip():
                    self.send_json({"ok": False, "error": err or "need path"}, 400)
                    return
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    self.send_json({"ok": True, "path": _ws_rel(p)})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)[:200]}, 500)
            elif parsed.path == "/api/delete":
                rel = str(data.get("path") or "")
                p, err = _ws_path(rel)
                if err or not rel:
                    self.send_json({"ok": False, "error": err or "need path"}, 400)
                    return
                blocked = _ws_sensitive(str(p))
                if blocked:
                    self.send_json({"ok": False, "error": f"delete blocked: {blocked}"}, 403)
                    return
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()
                    else:
                        self.send_json({"ok": False, "error": "not found"}, 404)
                        return
                    log(f"/api/delete {_ws_rel(p) if p != _ws_active().resolve() else '.'}")
                    self.send_json({"ok": True})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)[:200]}, 500)
            elif parsed.path == "/api/rename":
                src, err1 = _ws_path(str(data.get("from") or ""))
                dst, err2 = _ws_path(str(data.get("to") or ""))
                if err1 or err2 or not str(data.get("from") or "").strip() or not str(data.get("to") or "").strip():
                    self.send_json({"ok": False, "error": err1 or err2 or "need from + to"}, 400)
                    return
                blocked = _ws_sensitive(str(src)) or _ws_sensitive(str(dst))
                if blocked:
                    self.send_json({"ok": False, "error": f"rename blocked: {blocked}"}, 403)
                    return
                try:
                    if not src.exists():
                        self.send_json({"ok": False, "error": "not found"}, 404)
                        return
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dst)
                    self.send_json({"ok": True, "path": _ws_rel(dst)})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)[:200]}, 500)
        elif parsed.path == "/api/workspaces":
            # Spaces-lite: add|use|remove project roots. Server home stays at ROOT.
            action = str(data.get("action") or "list").strip().lower()
            st = _ws_state()
            if action == "list":
                self.send_json({"ok": True, "active": st["active"], "roots": st["roots"],
                                "home": str(ROOT)})
            elif action in ("add", "use"):
                raw = str(data.get("path") or "").strip()
                if not raw:
                    self.send_json({"ok": False, "error": "need path"}, 400)
                    return
                try:
                    cand = Path(raw).expanduser()
                    if not cand.is_absolute():
                        cand = (ROOT / cand)
                    cand = cand.resolve()
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)[:100]}, 400)
                    return
                if not cand.is_dir():
                    self.send_json({"ok": False, "error": "not a directory"}, 400)
                    return
                s = str(cand)
                if s not in st["roots"]:
                    st["roots"].append(s)
                if action == "use":
                    st["active"] = s
                _ws_save(st)
                log(f"/api/workspaces {action} {s}")
                self.send_json({"ok": True, "active": st["active"], "roots": st["roots"],
                                "home": str(ROOT)})
            elif action in ("remove", "rm", "delete"):
                raw = str(data.get("path") or "").strip()
                try:
                    s = str(Path(raw).expanduser().resolve())
                except Exception:
                    s = raw
                if s == str(ROOT.resolve()) or s == str(ROOT):
                    self.send_json({"ok": False, "error": "cannot remove server home"}, 400)
                    return
                st["roots"] = [r for r in st["roots"] if r != s]
                if st["active"] == s:
                    st["active"] = str(ROOT)
                _ws_save(st)
                self.send_json({"ok": True, "active": st["active"], "roots": st["roots"],
                                "home": str(ROOT)})
            else:
                self.send_json({"ok": False, "error": "use list|add|use|remove"}, 400)
        elif parsed.path == "/api/cron":
            # Tasks panel: list|add|remove|pause|resume|run cron jobs.
            try:
                import cron as _cron
            except Exception as e:
                self.send_json({"ok": False, "error": f"no cron.py: {e}"}, 500)
                return
            action = str(data.get("action") or "list").strip().lower()
            if action == "list":
                self.send_json({"ok": True, "jobs": _cron._load(), "runs": _cron.run_history()})
            elif action == "add":
                msg = _cron.add_job(str(data.get("schedule") or ""),
                                    str(data.get("prompt") or ""), str(data.get("model") or ""))
                self.send_json({"ok": not msg.startswith("cron error"), "message": msg},
                               200 if not msg.startswith("cron error") else 400)
            elif action in ("remove", "rm", "delete"):
                self.send_json({"ok": True, "message": _cron.remove_job(str(data.get("job_id") or data.get("id") or ""))})
            elif action == "pause":
                self.send_json({"ok": True, "message": _cron.pause_job(str(data.get("job_id") or data.get("id") or ""))})
            elif action in ("resume", "unpause"):
                self.send_json({"ok": True, "message": _cron.resume_job(str(data.get("job_id") or data.get("id") or ""))})
            elif action in ("update", "edit", "set"):
                msg = _cron.update_job(str(data.get("job_id") or data.get("id") or ""),
                                       str(data.get("schedule") or ""),
                                       str(data.get("prompt") or ""),
                                       str(data.get("model") or ""))
                self.send_json({"ok": not msg.startswith("cron error") and not msg.startswith("cron: no job"), "message": msg},
                               200 if not msg.startswith("cron error") else 400)
            elif action == "run":
                job = _cron.get_job(str(data.get("job_id") or data.get("id") or ""))
                if not job:
                    self.send_json({"ok": False, "error": "no such job"}, 404)
                    return
                t = threading.Thread(target=_cron_run_job, args=(dict(job),),
                                     name=f"cron-now-{job.get('id')}", daemon=True)
                t.start()
                log(f"/api/cron run-now {job.get('id')}")
                self.send_json({"ok": True, "job_id": job.get("id"), "started": True}, 202)
            else:
                self.send_json({"ok": False, "error": "use list|add|remove|pause|resume|run"}, 400)
        elif parsed.path == "/api/attach":
            # Chat attachments: vault under server home (NOT the workspace),
            # per session. Text is inlined client-side; this keeps the bytes.
            try:
                import base64 as _b64
                name = str(data.get("name") or "file").replace("/", "_").replace("\\", "_")[:120] or "file"
                b64 = str(data.get("data") or "")
                sid = "".join(c for c in str(data.get("session") or "misc") if c.isalnum() or c in "-_")[:40] or "misc"
                raw = _b64.b64decode(b64, validate=True) if b64 else b""
            except Exception as e:
                self.send_json({"ok": False, "error": f"bad base64: {e}"[:200]}, 400)
                return
            if len(raw) > 5_000_000:
                self.send_json({"ok": False, "error": "attachment > 5MB"}, 400)
                return
            att = SESSIONS_DIR / "attachments" / sid
            try:
                att.mkdir(parents=True, exist_ok=True)
                fn = f"{int(time.time())}-{name}"
                (att / fn).write_bytes(raw)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)[:200]}, 500)
                return
            log(f"/api/attach {sid}/{fn} {len(raw)} bytes")
            self.send_json({"ok": True, "file": fn, "size": len(raw)})
        elif parsed.path == "/api/upload":
            # Workspace browser: upload a file (base64 JSON, no multipart parsing).
            # Same containment + sensitive-path rules as /api/file.
            try:
                from tools import _sensitive_path as _ws_sensitive
            except Exception:
                _ws_sensitive = lambda p: None
            import base64 as _b64
            rel = str(data.get("path") or "")
            raw_b64 = str(data.get("data") or "")
            if not rel or not raw_b64 or len(raw_b64) > 7_000_000:
                self.send_json({"ok": False, "error": "need path + data (base64, max ~5MB)"}, 400)
                return
            p, err = _ws_path(rel)
            if err or not rel:
                self.send_json({"ok": False, "error": err or "need path"}, 400)
                return
            blocked = _ws_sensitive(str(p))
            if blocked:
                self.send_json({"ok": False, "error": f"upload blocked: {blocked}"}, 403)
                return
            try:
                raw = _b64.b64decode(raw_b64, validate=True)
            except Exception:
                self.send_json({"ok": False, "error": "bad base64"}, 400)
                return
            if len(raw) > 5_000_000:
                self.send_json({"ok": False, "error": "file too large (max 5MB)"}, 400)
                return
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                log(f"/api/upload {_ws_rel(p)} {len(raw)} bytes")
                self.send_json({"ok": True, "path": _ws_rel(p), "size": len(raw)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)[:200]}, 500)
        elif parsed.path == "/api/checkpoint":
            # Rollback checkpoints: git snapshot before risky turns + diff/restore.
            # create commits tracked-file changes only (git add -u: deliberately
            # untracked files stay out) or records HEAD if clean.
            # restore = `git checkout <sha> -- .` (tracked files; untracked stay).
            action = str(data.get("action") or "list").strip().lower()
            if action == "list":
                want = str(_ws_active().resolve())
                mine = [e for e in _cp_load()
                        if (e.get("root") or str(ROOT.resolve())) == want]
                self.send_json({"ok": True, "checkpoints": list(reversed(mine))})
                return
            if action == "create":
                msg = str(data.get("message") or "").strip()[:200] or "manual snapshot"
                h = _git("rev-parse", "HEAD")
                if not h or h.returncode != 0:
                    self.send_json({"ok": False, "error": "not a git repo"}, 500)
                    return
                st = _git("status", "--porcelain")
                dirty = bool(st and st.returncode == 0 and st.stdout.strip())
                if dirty:
                    _git("add", "-u")
                    c = _git("commit", "-m", f"checkpoint: {msg}")
                    if not c or c.returncode != 0:
                        self.send_json({"ok": False, "error": (c.stderr if c else "commit failed")[:200]}, 500)
                        return
                    h = _git("rev-parse", "HEAD")
                sha = (h.stdout.strip() if h else "")
                if not sha:
                    self.send_json({"ok": False, "error": "could not resolve HEAD"}, 500)
                    return
                entries = _cp_load()
                cid = f"cp-{int(time.time())}"
                entries.append({"id": cid, "sha": sha, "message": msg,
                                "committed": dirty, "ts": time.time(),
                                "root": str(_ws_active().resolve())})
                _cp_save(entries)
                log(f"/api/checkpoint create {cid} {sha[:8]} committed={dirty}")
                self.send_json({"ok": True, "id": cid, "sha": sha, "committed": dirty})
            elif action == "diff":
                cid = str(data.get("id") or "")
                entry = next((e for e in _cp_load() if e.get("id") == cid), None)
                if not entry:
                    self.send_json({"ok": False, "error": "no such checkpoint"}, 404)
                    return
                if (entry.get("root") or str(ROOT.resolve())) != str(_ws_active().resolve()):
                    self.send_json({"ok": False, "error": "checkpoint belongs to another workspace"}, 400)
                    return
                d = _git("diff", entry["sha"], "--stat")
                out = (d.stdout if d and d.returncode == 0 else "")[:4000]
                st = _git("status", "--porcelain")
                if st and st.returncode == 0 and st.stdout.strip():
                    out += "\n--- uncommitted ---\n" + st.stdout[:2000]
                self.send_json({"ok": True, "id": cid, "diff": out or "(no changes since checkpoint)"})
            elif action == "restore":
                cid = str(data.get("id") or "")
                entry = next((e for e in _cp_load() if e.get("id") == cid), None)
                if not entry:
                    self.send_json({"ok": False, "error": "no such checkpoint"}, 404)
                    return
                if (entry.get("root") or str(ROOT.resolve())) != str(_ws_active().resolve()):
                    self.send_json({"ok": False, "error": "checkpoint belongs to another workspace"}, 400)
                    return
                r = _git("checkout", entry["sha"], "--", ".")
                if not r or r.returncode != 0:
                    self.send_json({"ok": False, "error": (r.stderr if r else "checkout failed")[:200]}, 500)
                    return
                log(f"/api/checkpoint restore {cid} {entry['sha'][:8]}")
                self.send_json({"ok": True, "id": cid,
                                "note": "tracked files restored; untracked files stay"})
            else:
                self.send_json({"ok": False, "error": "use list|create|diff|restore"}, 400)
        elif parsed.path == "/api/term":
            # User's own shell (typed commands need no approval — approval is
            # for agent actions). Dies with the server; idle terms reaped at 30min.
            action = str(data.get("action") or "list").strip().lower()
            if action == "list":
                with TERMS_LOCK:
                    self.send_json({"ok": True, "terms": [
                        {"id": tid, "bytes": len(t["buf"]), "alive": not t.get("dead", False)}
                        for tid, t in TERMS.items()]})
            elif action == "create":
                try:
                    cols = max(20, min(300, int(data.get("cols") or 100)))
                    rows = max(5, min(100, int(data.get("rows") or 30)))
                except ValueError:
                    cols, rows = 100, 30
                tid, err = _term_create(cols, rows)
                if err:
                    self.send_json({"ok": False, "error": err}, 400
                                   if "too many" in err else 500)
                else:
                    self.send_json({"ok": True, "id": tid}, 202)
            elif action in ("input", "resize", "kill"):
                tid = str(data.get("id") or "")
                with TERMS_LOCK:
                    t = TERMS.get(tid)
                if not t:
                    self.send_json({"ok": False, "error": "no such terminal"}, 404)
                    return
                if action == "kill":
                    _term_kill(tid)
                    self.send_json({"ok": True, "id": tid})
                elif action == "resize":
                    try:
                        import fcntl as _fcntl
                        import termios as _termios
                        import struct as _struct
                        cols = max(20, min(300, int(data.get("cols") or 100)))
                        rows = max(5, min(100, int(data.get("rows") or 30)))
                        _fcntl.ioctl(t["fd"], _termios.TIOCSWINSZ,
                                     _struct.pack("HHHH", rows, cols, 0, 0))
                        self.send_json({"ok": True, "id": tid})
                    except Exception as e:
                        self.send_json({"ok": False, "error": str(e)[:100]}, 500)
                else:
                    try:
                        os.write(t["fd"], str(data.get("data") or "").encode("utf-8", errors="replace")[:65536])
                        with TERMS_LOCK:
                            if tid in TERMS:
                                TERMS[tid]["active"] = time.time()
                        self.send_json({"ok": True, "id": tid})
                    except OSError as e:
                        self.send_json({"ok": False, "error": f"terminal dead: {e}"}, 410)
            else:
                self.send_json({"ok": False, "error": "use list|create|input|resize|kill"}, 400)
        else:
            self.send_response(404)
            self.end_headers()


def _cron_tick():
    """Run due cron jobs once. Fresh agent per job (no history), max 6 steps.

    Best-effort: every failure is caught, marked, and logged. Never raises,
    never blocks chat traffic (runs in its own daemon thread).
    Overlap guard lives in _cron_run_job (shared with run-now).
    """
    try:
        import cron as _cron
    except Exception as e:
        log(f"cron tick skipped (no cron.py): {e}")
        return
    try:
        due = _cron.due_jobs()
    except Exception as e:
        log(f"cron tick load error: {e}")
        return
    for job in due:
        _cron_run_job(job)


def _cron_run_job(job):
    """Run one cron job now. Shared by the tick loop and run-now.

    Fresh agent per job (no history), max 6 steps. Best-effort: every
    failure is caught, marked, and logged. Overlap guard: a job already
    running is skipped. Returns a short status string.
    """
    if not hasattr(_cron_run_job, "_running"):
        _cron_run_job._running = set()
        _cron_run_job._lock = threading.Lock()
    try:
        import cron as _cron
    except Exception as e:
        log(f"cron run skipped (no cron.py): {e}")
        return f"error: {e}"
    jid = job.get("id", "?")
    with _cron_run_job._lock:
        if jid in _cron_run_job._running:
            log(f"cron job {jid} skipped (already running)")
            return "skipped (already running)"
        _cron_run_job._running.add(jid)
    try:
        try:
            from prompt import build_system_prompt as _build_sys
            sys_text = _build_sys("")
        except Exception:
            sys_text = ""
        model = job.get("model") or DEFAULT_MODEL
        try:
            live = get_live_models()
            if model not in live:
                model = live[0] if live else DEFAULT_MODEL
        except Exception:
            pass
        msgs = []
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.append({"role": "user", "content": job.get("prompt", "")})
        reply, tool_logs, _, _ = run_agent(msgs, model, max_steps=6,
                                           live_logs=None, run_id=f"cron-{jid}-{int(time.time())}")
        # deliver: transcript file + cron session (searchable via session_search)
        try:
            _cron.RUNS_DIR.mkdir(parents=True, exist_ok=True)
            body = f"# cron {jid} @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n## prompt\n\n{job.get('prompt','')}\n\n## reply\n\n{reply}\n\n## tools\n\n" + "\n".join(
                f"- {t.get('tool')}: {str(t.get('result',''))[:300]}" for t in (tool_logs or []))
            (_cron.RUNS_DIR / f"{jid}-{int(time.time())}.md").write_text(body[:50000])
        except Exception as e:
            log(f"cron run-save error {jid}: {e}")
        try:
            _save_turn(f"cron-{jid}", job.get("prompt", ""), reply, model, tool_logs)
        except Exception as e:
            log(f"cron session-save error {jid}: {e}")
        _cron.mark_ran(jid, "ok")
        log(f"cron job {jid} ok reply_len={len(reply)}")
        return "ok"
    except Exception as e:
        try:
            _cron.mark_ran(jid, f"failed: {str(e)[:100]}")
        except Exception:
            pass
        log(f"cron job {jid} ERROR: {e}")
        return f"error: {e}"
    finally:
        with _cron_run_job._lock:
            _cron_run_job._running.discard(jid)


def _cron_start(interval_s: int = 30):
    """Start daemon tick thread. No-op if already running in this process."""
    def _loop():
        while True:
            try:
                time.sleep(interval_s)
                _cron_tick()
            except Exception as e:
                try:
                    log(f"cron loop error: {e}")
                except Exception:
                    pass
    t = threading.Thread(target=_loop, name="cron-tick", daemon=True)
    t.start()
    log(f"cron scheduler on (tick {interval_s}s, store .cron/jobs.json)")


if __name__ == "__main__":
    _store_init()
    _cron_start()
    loopback = HOST in ("127.0.0.1", "::1", "localhost")
    if not loopback and not FORMY_TOKEN:
        raise SystemExit(
            f"refusing to bind {HOST} without FORMY_TOKEN "
            "(set FORMY_TOKEN to a long random value first)")
    log(f"serving http://{HOST}:{PORT} (log: {LOG_FILE})")
    log(f"model default={DEFAULT_MODEL} key={'set' if API_KEY else 'MISSING export MAXPLUS_API_KEY=ccsk-...'}")
    log(f"auth={'token required' if FORMY_TOKEN else 'off, loopback only'}")
    # open chatbot automatically (set NO_BROWSER=1 to skip)
    if os.getenv("NO_BROWSER", "") not in ("1", "true"):
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{PORT}")
        except Exception:
            pass
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
