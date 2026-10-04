"""Dangerous-command approval (Hermes DANGEROUS_PATTERNS pattern, minimal port).

Modes via FORMY_APPROVAL:
  ask  – CLI: prompt on TTY, deny when non-interactive (safe default).
  deny – web/server: never prompt, block with clear error (default for server).
  allow – bypass (explicit opt-in, e.g. sandbox).

Behavior contract (never breaks normal commands):
  - Safe commands pass through untouched, zero latency.
  - Dangerous commands in `deny`/non-TTY `ask` return an error string
    (tool isolation turns it into a normal tool result, not a crash).
  - Only a TTY CLI in `ask` mode prompts the user.
"""
import os
import re
import sys
import threading
import time

# (regex, human-readable reason). Keep tight: rm -rf on /tmp or node_modules
# still matches – user can allow explicitly. False positive > wiped disk.
DANGEROUS_PATTERNS = [
    (r"\brm\s+.*-[a-z]*r[a-z]*f", "recursive delete (rm -rf)"),
    (r"\b(mkfs|dd\s+[^|]*of=|:\(\)\s*\{\s*:\|\:&\s*\}\s*;)", "disk wipe / fork bomb"),
    (r"\bDROP\s+TABLE\b", "destructive SQL (DROP TABLE)"),
    (r"\bDELETE\s+FROM\b(?![\s\S]*\bWHERE\b)", "destructive SQL (DELETE without WHERE)"),
    (r">\s*/etc/", "system config overwrite (> /etc/)"),
    (r"\b(systemctl\s+(stop|disable|mask)|service\s+\S+\s+stop)\b", "service manipulation"),
    (r"\b(chmod\s+-R\s+777\s+/|chown\s+-R\s+\S+\s+/)\b", "recursive permission change on /"),
    (r"(curl|wget)\s+[^|]*\|\s*(sh|bash)", "remote code execution (curl|sh)"),
    (r"\b(pkill\s+-9\s+-1|kill\s+-9\s+-1|shutdown|reboot|halt)\b", "process kill / shutdown"),
]


def detect(command: str):
    """Return reason string if dangerous, else None."""
    pat, reason = match(command)
    return reason


def match(command: str):
    """Return (pattern, reason) of the first dangerous match, else (None, None)."""
    cmd = command or ""
    for rx, reason in DANGEROUS_PATTERNS:
        try:
            if re.search(rx, cmd, re.IGNORECASE):
                return rx, reason
        except Exception:
            continue
    return None, None


def _mode() -> str:
    return (os.getenv("FORMY_APPROVAL", "") or "").strip().lower()


def check(command: str, default_ask: bool = False) -> tuple:
    """Decide whether `command` may run.

    Returns (allowed: bool, message: str). message is "" when allowed.
    Never raises, never prompts unless TTY + ask mode.
    """
    reason = detect(command)
    if not reason:
        return True, ""
    mode = _mode() or ("ask" if default_ask else "deny")
    if mode == "allow":
        return True, ""
    if mode == "ask" and sys.stdin.isatty():
        try:
            ans = input(f"[approval] dangerous command ({reason}):\n  $ {command}\nAllow once? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            return True, ""
        return False, f"approval denied ({reason}): command blocked by user"
    return False, (
        f"approval denied ({reason}): blocked by policy. "
        f"Run in CLI to approve interactively, or set FORMY_APPROVAL=allow in a sandbox."
    )


# ---------- web approval cards (Hermes SSE approval pattern, minimal) ----------
# bash in a web run with run_id parks a pending card instead of denying
# outright. Browser approves via POST /api/approval/respond (once/session/
# always/deny); the tool thread blocks until decision or timeout (deny).
APPROVAL_TIMEOUT = float(os.getenv("FORMY_APPROVAL_TIMEOUT", "180") or 180)
_APPROVAL_LOCK = threading.Lock()
_APPROVAL_PENDING = {}    # run_id -> {command, reason, pattern, ts, event, decision}
_APPROVAL_SESSION = set()  # (sid, pattern): allow for the rest of the session
_APPROVAL_ALWAYS = set()   # {pattern}: allow forever (persisted)
_EMIT = None               # server-registered SSE emitter fn(run_id, key, value)


def _always_path():
    from pathlib import Path
    return Path(__file__).parent / ".sessions" / "approvals.json"


def _load_always():
    if _APPROVAL_ALWAYS:
        return _APPROVAL_ALWAYS
    try:
        import json
        p = _always_path()
        if p.exists():
            for pat in (json.loads(p.read_text() or "[]") or []):
                if isinstance(pat, str) and pat:
                    _APPROVAL_ALWAYS.add(pat)
    except Exception:
        pass
    return _APPROVAL_ALWAYS


def _save_always():
    try:
        import json
        p = _always_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(_APPROVAL_ALWAYS)))
    except Exception:
        pass


def set_emitter(fn):
    """Server registers SSE fan-out (fn(run_id, key, value)). None disables."""
    global _EMIT
    _EMIT = fn


def pending_for(run_id: str):
    """Copy of the pending card for run_id, or None. Never raises."""
    try:
        with _APPROVAL_LOCK:
            e = _APPROVAL_PENDING.get(run_id)
            if not e:
                return None
            return {"command": e["command"], "reason": e["reason"], "ts": e["ts"]}
    except Exception:
        return None


def respond(run_id: str, choice: str):
    """Resolve a pending card. Returns (ok, msg). Never raises."""
    choice = (choice or "").strip().lower()
    if choice not in ("once", "session", "always", "deny"):
        return False, "need choice once|session|always|deny"
    try:
        with _APPROVAL_LOCK:
            e = _APPROVAL_PENDING.get(run_id)
            if e is None:
                return False, "no pending approval for run"
            e["decision"] = choice
            ev = e["event"]
        ev.set()
        return True, choice
    except Exception as ex:
        return False, str(ex)


def check_web(command: str, run_id: str = "", sid: str = ""):
    """Web-mode gate: allowlists first, else park a card and wait.

    Returns (allowed, message). Safe default on every doubt: deny.
    No RUNS entry watching this run_id (cron, evicted runs) -> deny
    immediately instead of parking a card nobody will answer.
    """
    pat, reason = match(command)
    if not pat:
        return True, ""
    if _mode() == "allow":
        return True, ""
    with _APPROVAL_LOCK:
        if ((sid, pat) in _APPROVAL_SESSION) or (pat in _load_always()):
            return True, ""
    if not run_id:
        return False, (
            f"approval denied ({reason}): blocked by policy. "
            f"Run in CLI to approve interactively, or set FORMY_APPROVAL=allow in a sandbox."
        )
    ev = threading.Event()
    with _APPROVAL_LOCK:
        _APPROVAL_PENDING[run_id] = {"command": (command or "")[:500], "reason": reason,
                                     "pattern": pat, "ts": time.time(),
                                     "event": ev, "decision": None}
    emit = _EMIT
    if emit is not None:
        try:
            emit(run_id, "approval", {"command": (command or "")[:500], "reason": reason})
        except Exception:
            pass
    else:
        # No SSE consumer (server never registered) — nobody can answer.
        with _APPROVAL_LOCK:
            _APPROVAL_PENDING.pop(run_id, None)
        return False, f"approval denied ({reason}): blocked by policy (no approval channel)"
    ev.wait(APPROVAL_TIMEOUT)
    with _APPROVAL_LOCK:
        entry = _APPROVAL_PENDING.pop(run_id, None)
    decision = (entry or {}).get("decision") or "deny"
    if decision in ("once", "session", "always"):
        if decision in ("session", "always") and sid:
            with _APPROVAL_LOCK:
                _APPROVAL_SESSION.add((sid, pat))
        if decision == "always":
            with _APPROVAL_LOCK:
                _APPROVAL_ALWAYS.add(pat)
            _save_always()
        return True, ""
    if entry is None:
        return False, f"approval denied ({reason}): timed out waiting for approval"
    return False, f"approval denied ({reason}): command blocked by user"
