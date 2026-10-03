"""
Built-in tools ported from opencode v2 (packages/core/src/tool/plugin).
https://github.com/anomalyco/opencode/tree/v2

Tools: bash(=v2 shell), edit, write, read, grep, glob, lsp (local extra),
       apply_patch(=v2 patch), skill, skill_manage, todowrite (local extra),
       cron (scheduled jobs), memory, session_search, webfetch, websearch, question, subagent,
       models(=v2 opencode_models),
       mcp_list_resources, mcp_read_resource (stubs: no MCP servers configured)
Registry: get_tool_definitions() + TOOLSETS + check_fn (Hermes pattern).
Backends: bash routes via backends.py (local/docker/ssh).
Approval: bash gated by approval.py DANGEROUS_PATTERNS.
"""
import glob as _globlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

TODO_FILE = Path(__file__).parent / ".todos.json"
JOBS_DIR = Path(__file__).parent / ".jobs"


def _bound_text(text: str, name: str = "tool") -> str:
    """P0-4: retain full text under .jobs/, return head+tail preview (no silent loss)."""
    text = str(text or "")
    if len(text) <= 8000:
        return text
    try:
        import time as _t
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"tool-{int(_t.time() * 1000)}-{name}.txt"
        (JOBS_DIR / fname).write_text(text)
        return text[:4000] + f"\n...[truncated {len(text)} chars total, full in .jobs/{fname} — read it with the read tool]...\n" + text[-4000:]
    except Exception:
        return text[:4000] + f"\n...[truncated {len(text)} chars]...\n" + text[-4000:]

# auto-load .env so standalone use (models/subagent) finds the key
_dotenv = Path(__file__).parent / ".env"
if _dotenv.exists():
    try:
        for _line in _dotenv.read_text().splitlines():
            _line = _line.strip()
            if _line.startswith("export "):
                _line = _line[len("export "):].strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass


# ---------- bash (= v2 shell) ----------
def bash(command: str, workdir: str = ".", timeout: int = 120, background: bool = False) -> str:
    """Execute shell commands. Mirrors v2 `shell` (workdir/timeout/background)."""
    if not command or not command.strip():
        return "bash error: empty command"
    # Approval gate (Hermes DANGEROUS_PATTERNS): safe cmds pass with zero overhead.
    # Web/server never prompts (deny); CLI prompts on TTY (ask). Never raises.
    try:
        from approval import check as _approval_check
        import sys as _sys
        _ask = _sys.stdin.isatty()
        _allowed, _msg = _approval_check(command, default_ask=_ask)
        if not _allowed:
            return f"bash blocked: {_msg}"
    except Exception:
        pass
    if background:
        # v2-style background job: detach, log to .jobs/<id>.log, agent reads it
        try:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            import time as _t
            jid = f"job-{int(_t.time() * 1000)}"
            logf = JOBS_DIR / f"{jid}.log"
            fh = open(logf, "w")
            fh.write(f"$ {command}\n")
            fh.flush()
            subprocess.Popen(
                command, shell=True, cwd=workdir or ".",
                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
            )
            return f"started background {jid}\nlog: {logf}\nread it with the read tool."
        except Exception as e:
            return f"bash background error: {e}"
    try:
        # Backend interface (Hermes environments pattern): local/docker/ssh.
        # Default local; unknown/unconfigured falls back to local. Same output shape.
        try:
            from backends import run as _backend_run
            _stdout, _stderr, _rc = _backend_run(command, workdir or ".", timeout)
        except Exception:
            p = subprocess.run(
                command, shell=True, cwd=workdir or ".",
                capture_output=True, text=True, timeout=timeout,
            )
            _stdout, _stderr, _rc = p.stdout or "", p.stderr or "", p.returncode
        out = f"$ {command}\n"
        if _stdout:
            out += _stdout
        if _stderr:
            out += f"\n[stderr]\n{_stderr}"
        out += f"\n[exit {_rc}]"
        # P0-4: spill full output, preview to caller
        return _bound_text(out, "bash")
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s: {command}"
    except Exception as e:
        return f"bash error: {e}"


# ---------- edit ----------
def edit(filePath: str, oldString: str, newString: str, replaceAll: bool = False) -> str:
    """Exact string replacement. Mirrors opencode `edit` tool."""
    p = Path(filePath)
    if not p.exists():
        return f"edit error: file not found: {filePath}"
    try:
        content = p.read_text()
    except Exception as e:
        return f"edit error reading file: {e}"
    if oldString not in content:
        return "edit error: oldString not found in content"
    if not replaceAll and content.count(oldString) > 1:
        return f"edit error: oldString found {content.count(oldString)} times, provide more context or use replaceAll=true"
    if replaceAll:
        content = content.replace(oldString, newString)
    else:
        content = content.replace(oldString, newString, 1)
    try:
        p.write_text(content)
        return f"edited {filePath} OK"
    except Exception as e:
        return f"edit error writing file: {e}"


# ---------- write ----------
def write(filePath: str, content: str) -> str:
    """Create or overwrite files. Mirrors opencode `write` tool."""
    p = Path(filePath)
    try:
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {filePath} OK ({len(content)} chars)"
    except Exception as e:
        return f"write error: {e}"


# ---------- read ----------
def read(filePath: str, offset: int = 1, limit: int = 2000) -> str:
    """Read files or list directories. Mirrors opencode `read` tool."""
    p = Path(filePath)
    if not p.exists():
        return f"read error: not found: {filePath}"
    if p.is_dir():
        try:
            entries = sorted(os.listdir(p))
            lines = []
            for e in entries:
                full = p / e
                suffix = "/" if full.is_dir() else ""
                lines.append(f"{e}{suffix}")
            return f"{filePath} (directory, {len(lines)} entries):\n" + "\n".join(lines[:500])
        except Exception as e:
            return f"read dir error: {e}"
    try:
        # binary guard
        raw = p.read_bytes()
        if b"\x00" in raw[:8000]:
            return f"read error: binary file, {len(raw)} bytes, skipped"
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, offset) - 1
        end = start + limit
        chunk = lines[start:end]
        numbered = [f"{i+1+start}: {l[:2000]}" for i, l in enumerate(chunk)]
        header = f"{filePath} ({len(lines)} lines, showing {start+1}-{min(end,len(lines))}):\n"
        return header + "\n".join(numbered)
    except Exception as e:
        return f"read error: {e}"


# ---------- grep (v2 parity: literal/caseSensitive/limit) ----------
def grep(pattern: str, path: str = ".", include: str = "*", literal: bool = False,
         caseSensitive: bool = True, limit: int = 200) -> str:
    """Content search. Mirrors v2 `grep` (ripgrep first, python fallback)."""
    if not pattern:
        return "grep error: empty pattern"
    results = []
    eff_pattern = pattern if not literal else None
    # try ripgrep first for speed + gitignore respect
    try:
        cmd = ["rg", "-n", "--no-heading", "-g", include]
        if literal:
            cmd.append("-F")
        if not caseSensitive:
            cmd.append("-i")
        cmd += ["--max-count", str(max(1, limit)), pattern, path]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if p.returncode in (0, 1):
            out = p.stdout.strip()
            if out:
                return _bound_text(out, "grep")
            # returncode 1 = no matches; fall through to python fallback
            # which will return "no matches" consistently
            if p.returncode == 1:
                pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # python fallback
    try:
        rx = re.compile(re.escape(pattern) if literal else pattern,
                         0 if caseSensitive else re.IGNORECASE)
    except Exception as e:
        return f"grep error: bad regex: {e}"
    base = Path(path) if path else Path(".")
    if base.is_file():
        files = [base]
    else:
        files = [f for f in base.rglob(include if include != "*" else "*") if f.is_file()]
    count = 0
    cap = max(1, int(limit or 200))
    for f in files[:2000]:
        # skip big/binary/hidden heavies
        if any(part in (".git", "node_modules", "__pycache__", ".venv", "dist", "build") for part in f.parts):
            continue
        try:
            if f.stat().st_size > 1_000_000:
                continue
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                results.append(f"{f}:{i}: {line[:500]}")
                count += 1
                if count >= cap:
                    break
        if count >= cap:
            break
    if not results:
        return "no matches"
    return _bound_text("\n".join(results), "grep")


# ---------- glob (v2 parity: hidden/limit) ----------
def glob(pattern: str, path: str = ".", hidden: bool = False, limit: int = 500) -> str:
    """Find files by glob. Mirrors v2 `glob` (hidden + limit)."""
    try:
        base = Path(path)
        if not base.exists():
            return f"glob error: path not found: {path}"
        # support ** patterns; alphabetical for deterministic agent output
        matches = sorted(str(p) for p in base.glob(pattern))
        if not hidden:
            matches = [m for m in matches
                       if not any(part.startswith(".") for part in Path(m).parts)]
        if not matches:
            return "no files matched"
        cap = max(1, int(limit or 500))
        out = matches[:cap]
        if len(matches) > cap:
            out.append(f"... truncated ({len(matches) - cap} more)")
        return "\n".join(out)
    except Exception as e:
        return f"glob error: {e}"


# ---------- lsp (experimental, lightweight) ----------
def lsp(operation: str = "workspaceSymbol", file: str = "", line: int = 1, col: int = 1, query: str = "") -> str:
    """Minimal LSP-like helper. Opencode needs OPENCODE_EXPERIMENTAL_LSP_TOOL; here we give basic static fallback."""
    if operation in ("workspaceSymbol", "documentSymbol"):
        if not query:
            return "lsp: provide query= symbol name to search"
        # naive: grep for def/class/function
        res = grep(f"(def|class|function|const|let|fn)\\s+{re.escape(query)}", path=".", include="*")
        return f"[lsp fallback:{operation} query={query}]\n{res}"
    if operation in ("goToDefinition", "findReferences", "hover"):
        if not file:
            return "lsp: provide file= path"
        content = read(file, offset=max(1, line - 5), limit=11)
        return f"[lsp fallback:{operation} {file}:{line}:{col}]\n{content}\n(note: full LSP server not wired; configure pyright/tsserver for real results)"
    return f"lsp: unsupported operation {operation}. Supported: workspaceSymbol, documentSymbol, goToDefinition, findReferences, hover"


# ---------- apply_patch ----------
def apply_patch(patchText: str) -> str:
    """Apply opencode-style patches. Mirrors opencode `apply_patch` tool.

    Supported markers (relative to cwd):
      *** Add File: path
      *** Update File: path
      *** Delete File: path
      *** Move to: newpath  (follows an Update File block = rename)
      content lines follow until next *** marker or *** End Patch
    Also handles *** Begin Patch / *** End Patch wrappers (ignored).
    """
    lines = patchText.splitlines()
    cur_op = None
    cur_path = None
    buf = []
    log = []

    def flush():
        nonlocal buf, cur_op, cur_path
        if not cur_op or not cur_path:
            buf = []
            return
        p = Path(cur_path)
        try:
            if cur_op == "add":
                if p.parent and str(p.parent) not in ("", "."):
                    p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("\n".join(buf) + ("\n" if buf else ""))
                log.append(f"added {cur_path}")
            elif cur_op == "update":
                if not p.exists():
                    log.append(f"update failed (not found): {cur_path}")
                else:
                    if p.parent and str(p.parent) not in ("", "."):
                        p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("\n".join(buf) + ("\n" if buf else ""))
                    log.append(f"updated {cur_path}")
            elif cur_op == "delete":
                if p.exists():
                    p.unlink()
                    log.append(f"deleted {cur_path}")
                else:
                    log.append(f"delete skipped (not found): {cur_path}")
            elif cur_op == "move":
                # buf[0] is source? Convention: *** Update File: old then *** Move to: new
                # we store source in cur_path_src attribute
                src = getattr(flush, "_src", None)
                if src and Path(src).exists():
                    dest = Path(cur_path)
                    if dest.parent and str(dest.parent) not in ("", "."):
                        dest.parent.mkdir(parents=True, exist_ok=True)
                    Path(src).rename(dest)
                    log.append(f"moved {src} -> {cur_path}")
                else:
                    log.append(f"move failed: src not found {src}")
        except Exception as e:
            log.append(f"error {cur_op} {cur_path}: {e}")
        buf = []
        # stale move-source must not leak into the next block
        flush._src = None

    for line in lines:
        s = line.strip()
        if s.startswith("*** Add File:"):
            flush()
            cur_op = "add"
            cur_path = s.split(":", 1)[1].strip()
            buf = []
        elif s.startswith("*** Update File:"):
            flush()
            cur_op = "update"
            cur_path = s.split(":", 1)[1].strip()
            flush._src = cur_path
            buf = []
        elif s.startswith("*** Delete File:"):
            flush()
            cur_op = "delete"
            cur_path = s.split(":", 1)[1].strip()
            buf = []
            flush()
            cur_op = None
            cur_path = None
        elif s.startswith("*** Move to:"):
            dest = s.split(":", 1)[1].strip()
            # rename from last update src. Do NOT flush pending update:
            # flush() with empty buf would truncate src. Instead move first,
            # then apply buffered content (if any) to dest.
            src = getattr(flush, "_src", None) or cur_path
            content = list(buf)
            buf = []
            cur_op = None
            cur_path = None
            flush._src = None
            try:
                if not src or not Path(src).exists():
                    log.append(f"move failed: src not found {src}")
                else:
                    d = Path(dest)
                    if d.parent and str(d.parent) not in ("", "."):
                        d.parent.mkdir(parents=True, exist_ok=True)
                    Path(src).rename(d)
                    if content:
                        d.write_text("\n".join(content) + "\n")
                        log.append(f"moved {src} -> {dest} (with updated content)")
                    else:
                        log.append(f"moved {src} -> {dest}")
            except Exception as e:
                log.append(f"error move {src} -> {dest}: {e}")
            cur_op = None
            cur_path = None
        elif s in ("*** Begin Patch", "*** End Patch", "*** End of Patch"):
            continue
        else:
            if cur_op in ("add", "update"):
                # strip leading +/-? keep raw for simplicity, drop diff prefixes
                if line.startswith("+") and not line.startswith("+++"):
                    buf.append(line[1:])
                elif line.startswith("-") and not line.startswith("---"):
                    continue
                else:
                    buf.append(line)
    flush()
    return "\n".join(log) if log else "apply_patch: no operations found"


# ---------- skill (v2 parity: id param) ----------
def skill(name: str = "", path: str = "", id: str = "") -> str:
    """Load a SKILL.md. Mirrors v2 `skill` (id = skill name)."""
    name = id or name
    candidates = []
    if path:
        candidates.append(Path(path))
    if name:
        candidates += [
            Path(f"skills/{name}/SKILL.md"),
            Path(f".opencode/skills/{name}/SKILL.md"),
            Path.home() / f".config/opencode/skills/{name}/SKILL.md",
            Path(name),
        ]
    for c in candidates:
        if c.exists() and c.is_file():
            try:
                return f"[skill:{c}]\n" + c.read_text()[:20000]
            except Exception as e:
                return f"skill read error: {e}"
    return f"skill not found: name={name} path={path}. Looked in skills/<name>/SKILL.md"


# ---------- todowrite ----------
def todowrite(action: str = "list", todos: str = "") -> str:
    """Manage todo list. Mirrors opencode `todowrite` tool.

    action: list|set|add|done|clear
    todos: JSON array string for set, or single task text for add/done
    """
    if TODO_FILE.exists():
        try:
            data = json.loads(TODO_FILE.read_text() or "[]")
        except Exception:
            data = []
    else:
        data = []

    if action == "list":
        if not data:
            return "todos: (empty)"
        return "\n".join([f"{i+1}. [{'x' if t.get('done') else ' '}] {t.get('content','')}" for i, t in enumerate(data)])
    if action == "set":
        try:
            arr = json.loads(todos)
            # normalize: allow ["task"] or [{content,...}]
            norm = []
            for t in arr:
                if isinstance(t, str):
                    norm.append({"content": t, "done": False})
                elif isinstance(t, dict):
                    norm.append({"content": t.get("content", str(t)), "done": bool(t.get("done", False))})
            TODO_FILE.write_text(json.dumps(norm, indent=2))
            return f"todos set: {len(norm)} items"
        except Exception as e:
            return f"todowrite set error: {e} (expect JSON array)"
    if action == "add":
        data.append({"content": todos, "done": False})
        TODO_FILE.write_text(json.dumps(data, indent=2))
        return f"added todo #{len(data)}"
    if action == "done":
        # todos = index (1-based) or text match
        try:
            idx = int(todos.strip()) - 1
            if 0 <= idx < len(data):
                data[idx]["done"] = True
                TODO_FILE.write_text(json.dumps(data, indent=2))
                return f"marked done #{idx+1}"
        except ValueError:
            pass
        for t in data:
            if todos.lower() in t.get("content", "").lower():
                t["done"] = True
                TODO_FILE.write_text(json.dumps(data, indent=2))
                return f"marked done: {t['content']}"
        return "todowrite done: not found"
    if action == "clear":
        TODO_FILE.write_text("[]")
        return "todos cleared"
    return f"todowrite: unknown action {action}"


# ---------- webfetch (v2 parity: format/timeout) ----------
def webfetch(url: str, fmt: str = "markdown", timeout: int = 30) -> str:
    """Fetch URL. Mirrors v2 `webfetch` (text/markdown/html, read-only)."""
    if not url or not url.startswith(("http://", "https://")):
        return "webfetch error: url must start with http:// or https://"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "formyproject-agent/1.0"})
        with urllib.request.urlopen(req, timeout=max(1, min(120, int(timeout or 30)))) as r:
            raw = r.read()[:2_000_000].decode("utf-8", errors="ignore")
        if fmt == "html":
            return _bound_text(raw, "webfetch")
        if fmt == "text":
            text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
            text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            return _bound_text(text.strip(), "webfetch")
        # markdown-ish: keep links/text
        text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return _bound_text(text.strip(), "webfetch")
    except Exception as e:
        return f"webfetch error: {e}"


# ---------- websearch (Exa first, DuckDuckGo fallback) ----------
def _exa_search(query: str, numResults: int):
    """Exa Search API per https://exa.ai/docs/search/quickstart.
    POST https://api.exa.ai/search {query, type:auto, numResults, contents:{highlights:true}}.
    Returns formatted string or raises to trigger DDG fallback."""
    key = os.getenv("EXA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no EXA_API_KEY")
    n = max(1, min(10, int(numResults or 8)))
    body = json.dumps({
        "query": query,
        "type": "auto",
        "numResults": n,
        "contents": {"highlights": True},
    }).encode()
    req = urllib.request.Request(
        "https://api.exa.ai/search", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))
    results = data.get("results") or []
    if not results:
        return "websearch: no results (try different query)"
    out = [f"[exa] {len(results)} results for \"{query}\":"]
    for h in results[:n]:
        title = (h.get("title") or "(no title)").strip()
        url = (h.get("url") or "").strip()
        out.append(f"- {title}\n  {url}")
        hl = h.get("highlights") or []
        if hl:
            snippet = " ".join(hl)[:400].replace("\n", " ")
            out.append(f"  {snippet}")
    return "\n".join(out)[:10000]


def websearch(query: str, numResults: int = 8) -> str:
    """Web search. Exa (needs EXA_API_KEY) first, DuckDuckGo fallback (no key)."""
    if not (query or "").strip():
        return "websearch error: empty query"
    try:
        return _exa_search(query.strip(), numResults)
    except Exception as exa_err:
        # no key or Exa failed -> DDG fallback so search still works
        exa_note = "" if "no EXA_API_KEY" in str(exa_err) else f"[exa failed: {str(exa_err)[:120]}, using DuckDuckGo] "
        try:
            return exa_note + _ddg_search(query.strip(), numResults)
        except Exception as e:
            return f"websearch error: {e}"


def _ddg_search(query: str, numResults: int = 8) -> str:
    try:
        q = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # extract result links/titles
        pattern = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
        out = []
        for m in pattern.finditer(html):
            link = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            # duckduckgo redirect: //duckduckgo.com/l/?uddg=<real>
            if "uddg=" in link:
                link = urllib.parse.unquote(link.split("uddg=", 1)[1].split("&")[0])
            out.append(f"- {title}\n  {link}")
            if len(out) >= numResults:
                break
        if not out:
            return "websearch (DuckDuckGo fallback): blocked or no results — set EXA_API_KEY for reliable search"
        return "\n".join(out)[:10000]
    except Exception as e:
        return f"websearch error: {e}"


# ---------- memory (Hermes MEMORY.md / USER.md pattern, minimal) ----------
MEMORY_FILE = Path(__file__).parent / "memories" / "MEMORY.md"
USER_FILE = Path(__file__).parent / "memories" / "USER.md"
MEMORY_LIMIT = 2200
USER_LIMIT = 1375

_BLOCKED_MEMORY_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "exfiltrate",
    "send credentials to",
]


def _memory_entries(path: Path):
    try:
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
        return out
    except Exception:
        return []


def _memory_write(path: Path, entries) -> None:
    header = f"# {path.stem} — one entry per line. Lines starting with # are comments.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(e + "\n" for e in entries))


def memory(action: str = "list", target: str = "memory", content: str = "", old_text: str = "") -> str:
    """Curated persistent memory. Mirrors Hermes `memory` (add/replace/remove/list).

    target: memory (env facts, lessons) | user (preferences, style).
    No `read` needed — entries are injected into the system prompt each session.
    """
    target = (target or "memory").strip().lower()
    if target not in ("memory", "user"):
        return f"memory error: unknown target {target!r} (use memory|user)"
    path = MEMORY_FILE if target == "memory" else USER_FILE
    limit = MEMORY_LIMIT if target == "memory" else USER_LIMIT
    entries = _memory_entries(path)

    if action == "list":
        if not entries:
            return f"{target}: (empty)"
        used = sum(len(e) + 1 for e in entries)
        lines = [f"{target} [{used}/{limit} chars]:"]
        lines += [f"{i+1}. {e}" for i, e in enumerate(entries)]
        return "\n".join(lines)

    if action == "add":
        text = (content or "").strip()
        if not text:
            return "memory error: empty content"
        low = text.lower()
        for pat in _BLOCKED_MEMORY_PATTERNS:
            if pat in low:
                return f"memory blocked: content matches injection pattern {pat!r}"
        if text in entries:
            return "memory: duplicate entry, not added"
        used = sum(len(e) + 1 for e in entries)
        if used + len(text) + 1 > limit:
            return (f"memory full ({used}/{limit} chars). Adding {len(text)} chars would exceed the limit. "
                    f"Consolidate first: use action=replace to merge entries or action=remove for stale ones.")
        entries.append(text)
        try:
            _memory_write(path, entries)
        except Exception as e:
            return f"memory write error: {e}"
        return f"memory saved to {target} ({len(entries)} entries)"

    if action == "remove":
        key = (old_text or content or "").strip()
        if not key:
            return "memory error: provide old_text (unique substring) to remove"
        hits = [e for e in entries if key in e]
        if not hits:
            return "memory remove: no match"
        if len(hits) > 1:
            return f"memory remove: {len(hits)} matches, be more specific"
        entries.remove(hits[0])
        try:
            _memory_write(path, entries)
        except Exception as e:
            return f"memory write error: {e}"
        return f"memory removed from {target}: {hits[0][:120]}"

    if action == "replace":
        key = (old_text or "").strip()
        text = (content or "").strip()
        if not key or not text:
            return "memory error: replace needs old_text (match) + content (new entry)"
        hits = [e for e in entries if key in e]
        if not hits:
            return "memory replace: no match"
        if len(hits) > 1:
            return f"memory replace: {len(hits)} matches, be more specific"
        trial = [text if e == hits[0] else e for e in entries]
        used = sum(len(e) + 1 for e in trial)
        if used > limit:
            return f"memory full ({used}/{limit} chars after replace). Shorten content first."
        entries = trial
        try:
            _memory_write(path, entries)
        except Exception as e:
            return f"memory write error: {e}"
        return f"memory replaced in {target}"
    return f"memory: unknown action {action} (use list|add|replace|remove)"


# ---------- session_search (Hermes FTS5 recall pattern, minimal) ----------
def session_search(query: str = "", limit: int = 10, role_filter: str = "") -> str:
    """Search past sessions (SQLite FTS5, LIKE fallback, then jsonl). Read-only, never raises."""
    q = (query or "").strip()
    if not q:
        return "session_search error: empty query"
    cap = max(1, min(50, int(limit or 10)))
    roles = [r.strip() for r in (role_filter or "").split(",") if r.strip()]
    sessions_dir = Path(__file__).parent / ".sessions"
    db_path = sessions_dir / "store.db"
    # 1) SQLite messages table (added by server upgrade; may not exist yet)
    try:
        if db_path.exists():
            import sqlite3
            c = sqlite3.connect(str(db_path), timeout=5)
            try:
                cols = [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
            except Exception:
                cols = []
            if cols:
                rows = []
                try:
                    # FTS5 path
                    fts = [r[0] for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'").fetchall()]
                    if fts:
                        sql = ("SELECT m.session_id, m.role, snippet(messages_fts, 0, '>>>', '<<<', '...', 20), m.timestamp "
                               "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                               "WHERE messages_fts MATCH ?")
                        params = [q]
                        if roles:
                            sql += " AND m.role IN (%s)" % ",".join("?" * len(roles))
                            params += roles
                        sql += " LIMIT ?"
                        params.append(cap)
                        rows = c.execute(sql, params).fetchall()
                    else:
                        raise RuntimeError("no fts")
                except Exception:
                    # LIKE fallback
                    like = f"%{q}%"
                    sql = "SELECT session_id, role, substr(content,1,300), timestamp FROM messages WHERE content LIKE ?"
                    params = [like]
                    if roles:
                        sql += " AND role IN (%s)" % ",".join("?" * len(roles))
                        params += roles
                    sql += " ORDER BY timestamp DESC LIMIT ?"
                    params.append(cap)
                    rows = c.execute(sql, params).fetchall()
                if rows:
                    out = [f"session_search: {len(rows)} hit(s) for {q!r}:"]
                    for sid, role, snip, ts in rows:
                        out.append(f"- [{sid}] {role}: {str(snip or '')[:300]}")
                    return "\n".join(out)[:10000]
            c.close()
    except Exception:
        pass
    # 2) jsonl fallback (pre-upgrade sessions)
    try:
        hits = []
        if sessions_dir.exists():
            for p in sorted(sessions_dir.glob("*.jsonl")):
                try:
                    for line in p.read_text().splitlines():
                        if q.lower() in line.lower():
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue
                            if roles and obj.get("role") not in roles:
                                continue
                            hits.append(f"- [{p.stem}] {obj.get('role')}: {str(obj.get('content') or '')[:200]}")
                            if len(hits) >= cap:
                                break
                except Exception:
                    continue
                if len(hits) >= cap:
                    break
        if hits:
            return f"session_search (jsonl fallback): {len(hits)} hit(s) for {q!r}:\n" + "\n".join(hits)[:10000]
    except Exception:
        pass
    return f"session_search: no hits for {q!r}"


# ---------- skill_manage (Hermes agent-writable skills pattern, minimal) ----------
# skills/<name>/SKILL.md — procedural memory. Agent saves reusable workflows here.
# skill() reads; skill_manage() writes. Name sanitized, traversal blocked, 24k cap.
SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_BODY_CAP = 24000


def _skill_name_ok(name: str) -> bool:
    n = (name or "").strip()
    if not n or len(n) > 64:
        return False
    return all(c.isalnum() or c in ("-", "_") for c in n)


def skill_manage(action: str = "list", name: str = "", content: str = "",
                 old_string: str = "", new_string: str = "") -> str:
    """Create/patch/delete agent skills. Mirrors Hermes `skill_manage` (no hub, local only).

    action: list|view|create|patch|delete
    - create: full SKILL.md body in content (preferred over patch for new skills).
    - patch: targeted old_string -> new_string (preferred for updates, token-efficient).
    - patch with content + empty old_string: full rewrite.
    """
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if action == "list":
        try:
            names = sorted(p.name for p in SKILLS_DIR.iterdir()
                           if p.is_dir() and (p / "SKILL.md").exists())
        except Exception as e:
            return f"skill_manage list error: {e}"
        return "skills:\n" + "\n".join(f"- {n}" for n in names) if names else "skills: (none yet — create one with action=create)"
    if action == "view":
        if not _skill_name_ok(name):
            return "skill_manage error: bad name (alnum, -, _ only)"
        p = SKILLS_DIR / name.strip() / "SKILL.md"
        if not p.exists():
            return f"skill_manage: not found: {name}"
        try:
            return f"[skill:{name}]\n" + p.read_text()[:SKILL_BODY_CAP]
        except Exception as e:
            return f"skill_manage read error: {e}"
    if action in ("create", "patch", "delete"):
        if not _skill_name_ok(name):
            return "skill_manage error: bad name (alnum, -, _ only, max 64)"
        d = SKILLS_DIR / name.strip()
        # traversal guard: skill dir must resolve to a direct child of skills/
        try:
            ok = (d.resolve().parent == SKILLS_DIR.resolve())
        except Exception:
            ok = False
        if not ok:
            return "skill_manage blocked: path traversal"
        if action == "create":
            body = (content or "").strip()
            if not body:
                return "skill_manage error: create needs content (full SKILL.md body)"
            if len(body) > SKILL_BODY_CAP:
                return f"skill_manage error: body {len(body)} chars > cap {SKILL_BODY_CAP}"
            if (d / "SKILL.md").exists():
                return f"skill_manage: {name} exists — use action=patch to update"
            try:
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(body + "\n")
                return f"skill created: skills/{name}/SKILL.md ({len(body)} chars)"
            except Exception as e:
                return f"skill_manage create error: {e}"
        if action == "delete":
            p = d / "SKILL.md"
            if not p.exists():
                return f"skill_manage: not found: {name}"
            try:
                p.unlink()
                try:
                    d.rmdir()  # only if empty (keeps references/ etc.)
                except OSError:
                    pass
                return f"skill deleted: {name}"
            except Exception as e:
                return f"skill_manage delete error: {e}"
        # patch
        p = d / "SKILL.md"
        if not p.exists():
            return f"skill_manage: not found: {name} — use action=create first"
        try:
            text = p.read_text()
        except Exception as e:
            return f"skill_manage read error: {e}"
        if old_string:
            if old_string not in text:
                return "skill_manage error: old_string not found"
            if text.count(old_string) > 1:
                return "skill_manage error: old_string matches multiple times, add context"
            text = text.replace(old_string, new_string, 1)
        elif content:
            if len(content) > SKILL_BODY_CAP:
                return f"skill_manage error: body {len(content)} chars > cap {SKILL_BODY_CAP}"
            text = content
        else:
            return "skill_manage error: patch needs old_string+new_string or content (full rewrite)"
        try:
            p.write_text(text if text.endswith("\n") else text + "\n")
            return f"skill patched: {name} ({len(text)} chars)"
        except Exception as e:
            return f"skill_manage write error: {e}"
    return f"skill_manage: unknown action {action} (use list|view|create|patch|delete)"


# ---------- parallel runner (Hermes ThreadPoolExecutor pattern, minimal) ----------
def run_tool_calls(calls):
    """Execute pre-parsed [(tc_id, name, args)] concurrently, order-restored.

    Returns [(entry, result_str)] in input order. Single call runs inline
    (no thread overhead). Never raises — per-call errors become result strings.
    """
    if not calls:
        return []
    if len(calls) == 1:
        tc_id, name, args = calls[0]
        try:
            result = execute_tool(name, args)
        except Exception as e:
            result = f"tool {name} error: {e}"
        result = str(result)
        return [({"tool": name, "args": args, "result": result[:2000]}, result)]
    from concurrent.futures import ThreadPoolExecutor

    def _one(item):
        tc_id, name, args = item
        try:
            result = execute_tool(name, args)
        except Exception as e:
            result = f"tool {name} error: {e}"
        result = str(result)
        return {"tool": name, "args": args, "result": result[:2000]}, result

    with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
        return list(ex.map(_one, calls))


# ---------- cron (Hermes scheduled-jobs pattern, laptop-minimal) ----------
def cron(action: str = "list", schedule: str = "", prompt: str = "",
         model: str = "", job_id: str = "") -> str:
    """Scheduled agent jobs. action=list|add|remove. Server tick runs due jobs."""
    try:
        import cron as _cron
        return _cron.cron_tool(action, schedule, prompt, model, job_id or schedule)
    except Exception as e:
        return f"cron error: {e}"


# ---------- question ----------
def question(questions: str = "") -> str:
    """Ask user questions via CLI. Mirrors opencode `question` tool.

    questions: JSON array [{header, question, options:[{label, description}]}]
               or plain string (single free-text question).
    """
    try:
        arr = json.loads(questions) if questions.strip().startswith("[") else None
    except Exception:
        arr = None
    if arr is None:
        # plain string mode
        try:
            ans = input(f"\n[agent question] {questions}\n> ")
            return ans
        except EOFError:
            return ""
    answers = []
    for q in arr:
        header = q.get("header", "question")
        text = q.get("question", "")
        opts = q.get("options", [])
        print(f"\n[{header}] {text}")
        for i, o in enumerate(opts, 1):
            label = o.get("label", "") if isinstance(o, dict) else str(o)
            desc = o.get("description", "") if isinstance(o, dict) else ""
            print(f"  {i}. {label} {('- ' + desc) if desc else ''}")
        print("  (type number or your own answer)")
        try:
            ans = input("> ").strip()
        except EOFError:
            ans = ""
        # map number -> label
        try:
            idx = int(ans) - 1
            if 0 <= idx < len(opts):
                o = opts[idx]
                ans = o.get("label", ans) if isinstance(o, dict) else str(o)
        except ValueError:
            pass
        answers.append(f"{header}: {ans}")
    return "\n".join(answers)


# ---------- subagent (v2 parity, bounded) ----------
def subagent(description: str = "", prompt: str = "", model: str = "") -> str:
    """Spawn a child agent with fresh context. Mirrors v2 `subagent`.

    Runs a bounded (max 6 steps) agent turn with all tools except subagent
    itself (no recursion) and question (no stdin in nested context).
    """
    if not prompt or not prompt.strip():
        return "subagent error: empty prompt"
    try:
        from openai import OpenAI
    except ImportError:
        return "subagent error: openai package not installed (pip install -r requirements.txt)"
    base_url = os.getenv("MAXPLUS_BASE_URL", "https://api.maxplus-ai.cc/chinese-specials/v1")
    api_key = os.getenv("MAXPLUS_API_KEY", os.getenv("OPENCODE_API_KEY", ""))
    if not api_key:
        return "subagent error: MAXPLUS_API_KEY not set"
    use_model = (model or "").strip() or os.getenv("MAXPLUS_MODEL", os.getenv("OPENCODE_MODEL", "glm-5.3-flash"))
    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        nested_specs = [t for t in TOOLS_SPECS
                        if t["function"]["name"] not in ("subagent", "question")]
        msgs = [{"role": "user", "content": prompt}]
        for _ in range(6):
            resp = client.chat.completions.create(
                model=use_model, messages=msgs,
                tools=nested_specs, tool_choice="auto",
            )
            msg = resp.choices[0].message
            m = {"role": "assistant", "content": msg.content or ""}
            calls = getattr(msg, "tool_calls", None) or []
            if calls:
                m["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in calls
                ]
            msgs.append(m)
            if not calls:
                out = msg.content or "(empty)"
                return f"[subagent:{description or 'task'}]\n{out}"[:8000]
            for tc in calls:
                try:
                    targs = json.loads(tc.function.arguments or "{}")
                except Exception:
                    targs = {}
                res = execute_tool(tc.function.name, targs)
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": str(res)[:8000]})
        return "[subagent: max steps reached]\n" + str(msgs[-1].get("content", ""))[:8000]
    except Exception as e:
        return f"subagent error: {e}"


# ---------- models (= v2 opencode_models, MaxPlus flavor) ----------
def models(query: str = "", limit: int = 20) -> str:
    """List models available to your MaxPlus key. Mirrors v2 `opencode_models`."""
    base_url = os.getenv("MAXPLUS_BASE_URL", "https://api.maxplus-ai.cc/chinese-specials/v1")
    api_key = os.getenv("MAXPLUS_API_KEY", os.getenv("OPENCODE_API_KEY", ""))
    if not api_key:
        return "models error: MAXPLUS_API_KEY not set"
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if query:
            q = query.lower()
            ids = [i for i in ids if q in i.lower()]
        cap = max(1, min(100, int(limit or 20)))
        ids = ids[:cap]
        return "models:\n" + "\n".join(f"- {i}" for i in ids) if ids else "models: none found"
    except Exception as e:
        return f"models error: {e}"


# ---------- mcp stubs (v2 parity: no MCP servers in this project) ----------
def mcp_list_resources(server: str = "") -> str:
    """List MCP resources. Stub: this project has no MCP servers configured."""
    return ("mcp_list_resources: no MCP servers configured in this project. "
            "Add an MCP server config first; until then use read/webfetch/grep.")


def mcp_read_resource(server: str = "", uri: str = "") -> str:
    """Read one MCP resource. Stub: this project has no MCP servers configured."""
    return ("mcp_read_resource: no MCP servers configured in this project. "
            f"(server={server} uri={uri})")


# ---------- compaction (opencode v2 §7, minimal rolling summary) ----------
SUMMARY_SYSTEM = (
    "You are a concise conversation summarizer for a coding agent. "
    "Summarize the transcript below in under 300 words. Keep: key facts, "
    "decisions, file paths, code changes, test results, open todos. "
    "Drop greetings and filler. Plain text only, no preamble."
)
SUMMARY_PREFIX = "[Auto-compacted summary of earlier history — use as context]\n"


def _compact_transcript(old, per_msg_cap=2000, total_cap=60000) -> str:
    """Render old messages as plain text for the summarizer."""
    parts = []
    for m in old:
        role = m.get("role", "?")
        content = str(m.get("content") or "")
        calls = m.get("tool_calls") or []
        if calls and not content:
            bits = []
            for c in calls:
                fn = c.get("function") or {}
                bits.append(f"{fn.get('name', '?')}({str(fn.get('arguments', ''))[:200]})")
            content = "[tool calls: " + ", ".join(bits) + "]"
        if len(content) > per_msg_cap:
            content = content[:per_msg_cap] + f"...[{len(content)} chars total]"
        parts.append(f"{role}: {content}")
    text = "\n".join(parts)
    if len(text) > total_cap:
        text = text[:30000] + f"\n...[{len(text)} chars total, middle omitted]...\n" + text[-30000:]
    return text


def compact_messages(messages, summarize_fn, keep_recent=20, max_msgs=60, max_chars=80000):
    """Rolling compaction: summarize oldest messages, keep newest verbatim.

    - Never drops the leading system message or the latest message.
    - Never splits assistant tool_calls from their tool responses.
    - summarize_fn(transcript) -> summary string (LLM call, may raise).
    - Never raises: summarizer failure falls back to a plain slice.
    Returns (new_messages, info dict).
    """
    info = {"compacted": False, "old_n": len(messages), "new_n": len(messages)}
    try:
        if len(messages) <= 1:
            return messages, info
        total = sum(len(str(m.get("content") or "")) for m in messages)
        if len(messages) <= max_msgs and total <= max_chars:
            return messages, info
        sys_n = 1 if messages[0].get("role") == "system" else 0
        if len(messages) - sys_n <= 1:
            info["error"] = "single message over budget, cannot compact"
            return messages, info
        keep = min(keep_recent, len(messages) - sys_n - 1)
        cut = len(messages) - keep
        while cut > sys_n and messages[cut].get("role") == "tool":
            cut -= 1
        old, recent = messages[sys_n:cut], messages[cut:]
        if not old:
            info["error"] = "no compactable prefix (tool-role edge)"
            return messages, info
        summary = (summarize_fn(_compact_transcript(old)) or "").strip()
        if not summary:
            raise RuntimeError("empty summary")
        new = messages[:sys_n] + [{"role": "system", "content": SUMMARY_PREFIX + summary}] + recent
        info.update({"compacted": True, "new_n": len(new), "summary_len": len(summary)})
        return new, info
    except Exception as e:
        try:
            sys_n = 1 if messages and messages[0].get("role") == "system" else 0
            keep = min(keep_recent, len(messages) - sys_n)
            new = messages[:sys_n] + messages[len(messages) - keep:]
            info.update({"new_n": len(new), "error": str(e)[:200]})
            return new, info
        except Exception:
            return messages, info


# ---------- dispatcher + OpenAI schemas ----------
def execute_tool(name: str, args: dict) -> str:
    args = args or {}
    try:
        if name == "bash":
            return bash(args.get("command", ""), args.get("workdir", "."),
                        int(args.get("timeout", 120)), bool(args.get("background", False)))
        if name == "edit":
            return edit(args.get("filePath", ""), args.get("oldString", ""), args.get("newString", ""), bool(args.get("replaceAll", False)))
        if name == "write":
            return write(args.get("filePath", ""), args.get("content", ""))
        if name == "read":
            return read(args.get("filePath", ""), int(args.get("offset", 1)), int(args.get("limit", 2000)))
        if name == "grep":
            return grep(args.get("pattern", ""), args.get("path", "."), args.get("include", "*"),
                        bool(args.get("literal", False)), bool(args.get("caseSensitive", True)),
                        int(args.get("limit", 200)))
        if name == "glob":
            return glob(args.get("pattern", ""), args.get("path", "."),
                        bool(args.get("hidden", False)), int(args.get("limit", 500)))
        if name == "lsp":
            return lsp(args.get("operation", "workspaceSymbol"), args.get("file", ""), int(args.get("line", 1)), int(args.get("col", 1)), args.get("query", ""))
        if name == "apply_patch":
            return apply_patch(args.get("patchText", ""))
        if name == "skill":
            return skill(args.get("name", ""), args.get("path", ""), args.get("id", ""))
        if name == "skill_manage":
            return skill_manage(args.get("action", "list"), args.get("name", ""),
                                args.get("content", ""), args.get("old_string", ""),
                                args.get("new_string", ""))
        if name == "todowrite":
            return todowrite(args.get("action", "list"), args.get("todos", ""))
        if name == "cron":
            return cron(args.get("action", "list"), args.get("schedule", ""),
                        args.get("prompt", ""), args.get("model", ""),
                        args.get("job_id", ""))
        if name == "memory":
            return memory(args.get("action", "list"), args.get("target", "memory"),
                          args.get("content", ""), args.get("old_text", ""))
        if name == "session_search":
            return session_search(args.get("query", ""), int(args.get("limit", 10)),
                                  args.get("role_filter", ""))
        if name == "webfetch":
            return webfetch(args.get("url", ""), args.get("format", "markdown"), int(args.get("timeout", 30)))
        if name == "websearch":
            return websearch(args.get("query", ""), int(args.get("numResults", 8)))
        if name == "question":
            q = args.get("questions", args.get("question", ""))
            if isinstance(q, list):
                q = json.dumps(q)
            return question(str(q))
        if name == "subagent":
            return subagent(args.get("description", ""), args.get("prompt", ""), args.get("model", ""))
        if name == "models":
            return models(args.get("query", ""), int(args.get("limit", 20)))
        if name == "mcp_list_resources":
            return mcp_list_resources(args.get("server", ""))
        if name == "mcp_read_resource":
            return mcp_read_resource(args.get("server", ""), args.get("uri", ""))
        return f"unknown tool: {name}"
    except Exception as e:
        return f"tool {name} error: {e}"


TOOLS_SPECS = [
    {"type": "function", "function": {"name": "bash", "description": "Execute shell commands (npm, git, pytest, etc). Set background=true for long jobs; read .jobs/<id>.log.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "workdir": {"type": "string"}, "timeout": {"type": "integer"}, "background": {"type": "boolean"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "edit", "description": "Exact string replacement in existing files.", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "oldString": {"type": "string"}, "newString": {"type": "string"}, "replaceAll": {"type": "boolean"}}, "required": ["filePath", "oldString", "newString"]}}},
    {"type": "function", "function": {"name": "write", "description": "Create or overwrite files.", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filePath", "content"]}}},
    {"type": "function", "function": {"name": "read", "description": "Read file contents or list directory. Supports offset/limit.", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["filePath"]}}},
    {"type": "function", "function": {"name": "grep", "description": "Search content (ripgrep). literal=true for fixed strings, caseSensitive=false to ignore case.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "literal": {"type": "boolean"}, "caseSensitive": {"type": "boolean"}, "limit": {"type": "integer"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "glob", "description": "Find files by glob pattern like **/*.py. hidden=true includes dotfiles.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "hidden": {"type": "boolean"}, "limit": {"type": "integer"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "lsp", "description": "Code intelligence (fallback grep-based). ops: workspaceSymbol, documentSymbol, goToDefinition, findReferences, hover.", "parameters": {"type": "object", "properties": {"operation": {"type": "string"}, "file": {"type": "string"}, "line": {"type": "integer"}, "col": {"type": "integer"}, "query": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "apply_patch", "description": "Apply *** Add File / Update File / Delete File / Move to patches.", "parameters": {"type": "object", "properties": {"patchText": {"type": "string"}}, "required": ["patchText"]}}},
    {"type": "function", "function": {"name": "skill", "description": "Load a SKILL.md file by id or name.", "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "skill_manage", "description": "Create/patch/delete agent skills (procedural memory). action=list|view|create|patch|delete. Prefer patch for updates.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "name": {"type": "string"}, "content": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "todowrite", "description": "Manage todos. action=list|set|add|done|clear.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "todos": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "cron", "description": "Scheduled agent jobs run by the server. action=list|add|remove. add needs schedule (`every 1h`, `daily@09:00`) + prompt.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "schedule": {"type": "string"}, "prompt": {"type": "string"}, "model": {"type": "string"}, "job_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "memory", "description": "Curated persistent memory. action=list|add|replace|remove. target=memory|user.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "target": {"type": "string"}, "content": {"type": "string"}, "old_text": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "session_search", "description": "Search past sessions (FTS5, no LLM cost). Returns snippets with session ids.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "role_filter": {"type": "string"}}}, "required": ["query"]}},
    {"type": "function", "function": {"name": "webfetch", "description": "Fetch URL content (text/markdown/html).", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "format": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "websearch", "description": "Search web (Exa with highlights, DuckDuckGo fallback).", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "numResults": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "question", "description": "Ask user a question via CLI.", "parameters": {"type": "object", "properties": {"questions": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "subagent", "description": "Spawn a child agent with fresh context for independent work (max 6 steps, no recursion).", "parameters": {"type": "object", "properties": {"description": {"type": "string"}, "prompt": {"type": "string"}, "model": {"type": "string"}}, "required": ["description", "prompt"]}}},
    {"type": "function", "function": {"name": "models", "description": "List models available to your MaxPlus key, optionally filtered by query.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "mcp_list_resources", "description": "List MCP server resources (stub: no MCP servers configured).", "parameters": {"type": "object", "properties": {"server": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "mcp_read_resource", "description": "Read one MCP resource (stub: no MCP servers configured).", "parameters": {"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}}, "required": ["server", "uri"]}}},
]


# ---------- registry shim (Hermes tools/registry.py pattern, minimal) ----------
# TOOLS_SPECS above stays the source of truth (zero migration risk).
# This layer adds Hermes-style availability gating + toolset filtering on top.
def _tool_available(name: str) -> bool:
    """check_fn per tool. Conservative: everything on except risky/web-blocked cases.

    - question: CLI-only (server strips it already; keep parity here too).
    - websearch/models: always listed; handlers fail honestly without keys.
    """
    if name == "question" and os.getenv("FORMY_NO_QUESTION"):
        return False
    return True


TOOLSETS = {
    "files": ["read", "write", "edit", "apply_patch", "glob", "grep"],
    "shell": ["bash"],
    "search": ["grep", "glob", "lsp", "session_search", "webfetch", "websearch"],
    "agent": ["skill", "skill_manage", "todowrite", "cron", "memory", "subagent", "question", "models"],
    "mcp": ["mcp_list_resources", "mcp_read_resource"],
    "all": [t["function"]["name"] for t in TOOLS_SPECS],
}


def get_tool_definitions(enabled_toolsets=None, disabled_toolsets=None, exclude=None):
    """Hermes get_tool_definitions() equivalent. Returns filtered TOOLS_SPECS copy."""
    names = set(t["function"]["name"] for t in TOOLS_SPECS)
    if enabled_toolsets:
        keep = set()
        for ts in enabled_toolsets:
            keep.update(TOOLSETS.get(ts, [ts]))
        names &= keep
    if disabled_toolsets:
        drop = set()
        for ts in disabled_toolsets:
            drop.update(TOOLSETS.get(ts, [ts]))
        names -= drop
    if exclude:
        names -= set(exclude)
    return [t for t in TOOLS_SPECS if t["function"]["name"] in names and _tool_available(t["function"]["name"])]
