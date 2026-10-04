"""Cron: scheduled agent jobs (Hermes cron pattern, laptop-minimal).

Store: .cron/jobs.json (git-ignored). Runs: .cron/runs/<job>-<ts>.md.
Schedules: `every <N>s|m|h` (interval) or `daily@HH:MM` (local time).
Execution lives in server.py `_cron_tick()` (fresh run_agent, no history).
This file holds store + parsing only — no LLM imports, safe for the tool.
"""
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).parent
CRON_DIR = ROOT / ".cron"
JOBS_FILE = CRON_DIR / "jobs.json"
RUNS_DIR = CRON_DIR / "runs"

MAX_JOBS = 20
PROMPT_CAP = 2000


def _load():
    try:
        if JOBS_FILE.exists():
            data = json.loads(JOBS_FILE.read_text() or "[]")
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save(jobs) -> None:
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


def parse_schedule(spec: str, now: float = None):
    """Return next_run epoch for spec, or (None, error). Never raises."""
    now = now if now is not None else time.time()
    s = (spec or "").strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*([smh])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            return None, "interval must be > 0"
        step = n * {"s": 1, "m": 60, "h": 3600}[unit]
        if step < 60:
            return None, "minimum interval is every 60s"
        return now + step, ""
    m = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return None, "daily@HH:MM needs HH 00-23, MM 00-59"
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0,
                              lt.tm_wday, lt.tm_yday, lt.tm_isdst))
        if target <= now:
            target += 86400
        return target, ""
    return None, "bad schedule (use `every <N>s|m|h` or `daily@HH:MM`)"


def next_after(spec: str, now: float):
    """Next run after `now` for a stored spec. (None, err) on invalid."""
    return parse_schedule(spec, now)


def add_job(schedule: str, prompt: str, model: str = "") -> str:
    jobs = _load()
    if len(jobs) >= MAX_JOBS:
        return f"cron error: max {MAX_JOBS} jobs"
    prompt = (prompt or "").strip()
    if not prompt:
        return "cron error: empty prompt"
    if len(prompt) > PROMPT_CAP:
        return f"cron error: prompt {len(prompt)} chars > cap {PROMPT_CAP}"
    nxt, err = parse_schedule(schedule)
    if err:
        return f"cron error: {err}"
    import uuid
    jid = uuid.uuid4().hex[:8]
    jobs.append({"id": jid, "schedule": schedule.strip().lower(),
                 "prompt": prompt, "model": (model or "").strip(),
                 "created": time.time(), "next_run": nxt,
                 "last_run": None, "last_status": None})
    try:
        _save(jobs)
    except Exception as e:
        return f"cron save error: {e}"
    return f"cron job {jid} added ({schedule.strip().lower()}, next {time.strftime('%Y-%m-%d %H:%M', time.localtime(nxt))})"


def list_jobs() -> str:
    jobs = _load()
    if not jobs:
        return "cron: no jobs (add with action=add schedule=`every 1h` prompt=`...`)"
    lines = []
    for j in sorted(jobs, key=lambda x: x.get("next_run") or 0):
        nxt = j.get("next_run")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else "?"
        paused = " PAUSED" if j.get("paused") else ""
        lines.append(f"- {j['id']} [{j.get('schedule')}]{paused} next {when} "
                     f"last={j.get('last_status') or '-'} :: {j.get('prompt','')[:100]}")
    return "\n".join(lines)


def remove_job(jid: str) -> str:
    jobs = _load()
    kept = [j for j in jobs if j.get("id") != (jid or "").strip()]
    if len(kept) == len(jobs):
        return f"cron: no job {jid}"
    try:
        _save(kept)
    except Exception as e:
        return f"cron save error: {e}"
    return f"cron job {jid} removed"


def due_jobs(now: float = None):
    now = now if now is not None else time.time()
    return [j for j in _load() if (j.get("next_run") or 0) <= now and not j.get("paused")]


def pause_job(jid: str) -> str:
    jobs = _load()
    for j in jobs:
        if j.get("id") == (jid or "").strip():
            if j.get("paused"):
                return f"cron job {jid} already paused"
            j["paused"] = True
            try:
                _save(jobs)
            except Exception as e:
                return f"cron save error: {e}"
            return f"cron job {jid} paused"
    return f"cron: no job {jid}"


def resume_job(jid: str) -> str:
    jobs = _load()
    for j in jobs:
        if j.get("id") == (jid or "").strip():
            j["paused"] = False
            nxt, err = next_after(j.get("schedule", ""), time.time())
            if nxt:
                j["next_run"] = nxt
            try:
                _save(jobs)
            except Exception as e:
                return f"cron save error: {e}"
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["next_run"])) if j.get("next_run") else "?"
            return f"cron job {jid} resumed (next {when})"
    return f"cron: no job {jid}"


def update_job(jid: str, schedule: str = "", prompt: str = "", model: str = "") -> str:
    """Edit schedule and/or prompt of a job. Empty args keep current values."""
    jobs = _load()
    for j in jobs:
        if j.get("id") == (jid or "").strip():
            if schedule:
                nxt, err = parse_schedule(schedule)
                if err:
                    return f"cron error: {err}"
                j["schedule"] = schedule.strip().lower()
                j["next_run"] = nxt
            if prompt:
                if len(prompt) > PROMPT_CAP:
                    return f"cron error: prompt {len(prompt)} chars > cap {PROMPT_CAP}"
                j["prompt"] = prompt.strip()
            if model is not None and str(model).strip():
                j["model"] = str(model).strip()
            try:
                _save(jobs)
            except Exception as e:
                return f"cron save error: {e}"
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["next_run"])) if j.get("next_run") else "?"
            return f"cron job {jid} updated (next {when})"
    return f"cron: no job {jid}"


def get_job(jid: str):
    for j in _load():
        if j.get("id") == (jid or "").strip():
            return j
    return None


def run_history(limit: int = 20):
    """Newest transcript runs: [{job, ts, file, size}]. Never raises."""
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(RUNS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[: max(1, min(limit, 100))]:
            try:
                st = f.stat()
                out.append({"job": f.name.split("-")[0] if "-" in f.name else "?",
                            "file": f.name, "ts": st.st_mtime, "size": st.st_size})
            except Exception:
                continue
        return out
    except Exception:
        return []


def mark_ran(jid: str, status: str) -> None:
    jobs = _load()
    now = time.time()
    for j in jobs:
        if j.get("id") == jid:
            j["last_run"] = now
            j["last_status"] = status
            nxt, err = next_after(j.get("schedule", ""), now)
            j["next_run"] = nxt if nxt else now + 3600
            break
    try:
        _save(jobs)
    except Exception:
        pass


def cron_tool(action: str = "list", schedule: str = "", prompt: str = "",
               model: str = "", job_id: str = "") -> str:
    """Agent-facing cron. action=list|add|remove|pause|resume|update. Read-only list is free."""
    a = (action or "list").strip().lower()
    if a == "list":
        return list_jobs()
    if a == "add":
        return add_job(schedule, prompt, model)
    if a in ("remove", "rm", "delete"):
        return remove_job(job_id or schedule)
    if a == "pause":
        return pause_job(job_id or schedule)
    if a in ("resume", "unpause"):
        return resume_job(job_id or schedule)
    if a in ("update", "edit", "set"):
        return update_job(job_id, schedule, prompt, model)
    return f"cron: unknown action {action} (use list|add|remove|pause|resume|update)"


if __name__ == "__main__":
    import sys
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "list").lower()
    if cmd == "list":
        print(list_jobs())
    elif cmd == "add" and len(sys.argv) >= 4:
        print(add_job(sys.argv[2], " ".join(sys.argv[3:])))
    elif cmd in ("remove", "rm") and len(sys.argv) >= 3:
        print(remove_job(sys.argv[2]))
    elif cmd == "pause" and len(sys.argv) >= 3:
        print(pause_job(sys.argv[2]))
    elif cmd in ("resume", "unpause") and len(sys.argv) >= 3:
        print(resume_job(sys.argv[2]))
    elif cmd in ("update", "edit") and len(sys.argv) >= 3:
        # update <id> <schedule|-> <prompt...|->  ('-' keeps current value)
        sched = sys.argv[3] if len(sys.argv) >= 4 else ""
        pr = " ".join(sys.argv[4:]) if len(sys.argv) >= 5 else ""
        print(update_job(sys.argv[2],
                         "" if sched == "-" else sched,
                         "" if pr == "-" else pr))
    else:
        print("usage: cron.py list | add <schedule> <prompt...> | remove <id> | pause <id> | resume <id> | update <id> <schedule|-> <prompt...|->")
