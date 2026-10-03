# Handoff — formyproject agent

## What this is
Localhost coding-agent clone of opencode tools + desktop ideas, backed by **MaxPlus AI**
(OpenAI-compatible). Python backend, single-file web frontend, personal system prompt.
Ported opencode-v2 long-task loop: inbox idempotency, steer, retry, compaction, SSE, SQLite.
Plus Hermes-pattern glow-ups (2 rounds): registry, backends, approval, memory, fallback,
parallel tools, writable skills, cron, export-with-tools, highlight, skins.
Plus 2026-10 session: security hardening (7 fixes), `process` + `extract` tools (23 total),
live token + thinking streaming with interrupt-mid-generation.

## Layout
- `server.py` (1041 lines) — backend (`ThreadingHTTPServer` on `127.0.0.1:8000`).
  Endpoints: `GET /`, `GET /api/models`, `GET /api/system`, `POST /api/system`,
  `POST /api/chat`, `POST /api/steer`, `POST /api/interrupt` (Esc),
  `GET /api/progress?run=<id>`, `GET /api/event?run=<id>` (SSE),
  `GET /api/session?id=<session-id>`.
  P0: retry 1+4 (429/5xx), `_bound_for_model` (spill full to `.jobs/`),
  `_drain_steer`, run/item idempotency (+ SQLite cross-restart), per-call isolation.
  P1: `_maybe_compact` per step, write-ahead claims, boot recovery.
  Fix: `/api/event` waits 10s for run to exist (browser connects before chat POST).
  Glow-up: `_model_chain` + `_chat_create_with_fallback` (`FORMY_FALLBACK_MODELS`),
  parallel tools via `run_tool_calls` (order-restored), `_cron_tick` daemon (30s)
  + overlap guard (in-progress set, dup runs skipped).
  Streaming: `_chat_stream_with_retry` (`stream=True`, creation retry + mid-stream
  restart with `mark` event), `_consume_chat_stream` (token/reasoning deltas,
  tool-call index accumulation, per-chunk interrupt → close early, partial kept).
  Thinking persisted per step as `thinking` tool entry. POST cap 10MB → 413.
  SSE channels: `stream`/`stream_full` (tokens) + `think`/`think_full` (reasoning) +
  `mark` (`{step}`/`{restart}`) + `data` (tool logs) + `done`.
- `agent.py` (257) — CLI (`python3 agent.py [--model X]`). Same retry/compaction +
  fallback chain + parallel tools (question stays sequential, stdin). Non-streaming.
- `tools.py` (1636) — **23 tools** + `compact_messages()` rolling-summary helper.
  `websearch` = Exa first, DuckDuckGo fallback (bot-blocked, fails honestly).
  Added: `memory`, `session_search`, `skill_manage`, `cron`, `process`, `extract`.
  `process` = bg-job manager (`list|poll|wait|log|kill|write`, 64-receipt retention,
  log survives restarts, stdin=PIPE at spawn). `extract` = pdf (raw+flate, stdlib
  zlib) + docx/docm (stdlib zip); scanned PDFs refused honestly; else falls to `read`.
  Infra: `TOOLSETS` (6: files/shell/search/agent/mcp/all) + `get_tool_definitions()` +
  `_tool_available` (registry shim), `run_tool_calls()` (ThreadPoolExecutor, order-restored).
  `bash` routes via `backends.py`, gated by `approval.py`.
  Guards: webfetch loopback/metadata block; `_sensitive_path` on write/edit/apply_patch
  (`~/.ssh`, `~/.gnupg`, `/etc`, `/System`, keychains, bare key names;
  `FORMY_FILE_SCOPE=allow` bypasses).
- `backends.py` (95) — terminal interface: local/docker/ssh, resolved **per call**
  (no restart), best-effort `cd <workdir>` inside docker/ssh, `shlex` quoting,
  no-shell exec for remote. Default local, silent fallback.
- `approval.py` (72) — `DANGEROUS_PATTERNS` (rm -rf, mkfs, DROP, curl|sh…).
  Web = deny with reason; CLI TTY = prompt; `FORMY_APPROVAL=allow` bypasses.
- `prompt.py` (82) — assembles `SOUL.md` + `AGENTS.md` + UI text + memory snapshot
  (**live** re-read every turn, not frozen). Never raises; UI `/api/system` still
  edits `system_prompt.md` only.
- `cron.py` (163) — jobs store `.cron/jobs.json`, `every <N>s|m|h` + `daily@HH:MM`,
  CLI (`list|add|remove`), transcripts in `.cron/runs/`, sessions as `cron-<id>`.
  Max 20 jobs, prompt cap 2000, min interval `every 60s`.
- `index.html` (671) — whole frontend. Tools render **inline in chat** (desktop-style),
  inspector keeps system prompt only. Live token bubbles per turn (rAF-throttled,
  `▍` cursor, md-rendered on done, intermediate turns kept), thinking cards
  (live collapsible gold → persisted card on step end), ● LIVE pill (tooltip shows
  latest thought, click jumps + flashes bubble), Esc interrupts mid-generation.
  7 skins (default + ares/mono/slate/poseidon/sisyphus/charizard, picker in
  titlebar, persisted `formy.skin`), keyword+number highlight in code fences,
  tool logs persisted per session (`cur().tools`, cap 200) + included in export.
  SessSearch XSS fixed (`textContent`, verified all other `innerHTML` static/escaped).
- `SOUL.md` / `AGENTS.md` — identity + conventions (were one `system_prompt.md`).
- `system_prompt.md` — legacy layer, still UI-editable, wrapped by builder.
- `memories/MEMORY.md` (6 entries, 39% of 2200) + `memories/USER.md` (4 entries,
  20% of 1375) — seeded 2026-10 (env facts + ADHD/macOS prefs). Tracked in git.
- `skills/` — empty (agent-writable via `skill_manage`, traversal-guarded, 24k cap).
- `.env` — keys (git-ignored). See `.env.example` (all `FORMY_*` knobs incl.
  `FORMY_FILE_SCOPE=allow` sandbox bypass).
- `opencode-v2-explained.md` (222) — refreshed 2026-10: v2 guide + Hermes-agent +
  Hermes-webui comparisons, all current (23 tools, streaming, guards, file map).
- `todo-app/` — `todo.py` + `test_todo.py` + `AUDIT.md`. 13/13 green (unittest).
- GitHub: `https://github.com/z67n8n742p-ux/my-project.git` (branch `main`).

## Key facts
- Server RUNNING (started 2026-10-03 with streaming code, PID varies).
  Start: `NO_BROWSER=1 PORT=8000 nohup python3 server.py > server.restart.log 2>&1 &`,
  stop: `pkill -f server.py`. Restart needed for `server.py`/`tools.py` changes.
- Frontend served from disk per request → hard-refresh (`Cmd+Shift+R`) after UI edits.
- SQLite `.sessions/store.db` — runs + claims + `messages` + `messages_fts` (FTS5).
  Stale `running` → `interrupted` on boot.
- Interrupt is cooperative between steps AND mid-generation (stream closes early,
  partial text kept + `*(interrupted by user)*`); never mid-tool.
- `question` excluded from web; CLI blocks on `input()`.
- `subagent` bounded 6 steps, no recursion, no `question`.
- Commits on `main` unpushed: `7aea49f`, `8888354`, `02f080b` + working tree
  modifies 10 files (hardening + process/extract + streaming + memory + docs).
- Deleted earlier: `loop.py`, `p0-test-report.md` (junk, `git rm`).
- Git-ignored: `.env`, `server.log`, `server.restart.log`, `.todos.json`,
  `.jobs/`, `.sessions/`, `.cron/`, `.venv/`, `__pycache__/`.
- hermes-webui referenced remotely for skins/streaming patterns — NOT in repo.

## Run
```
python3 -m pip install -r requirements.txt
NO_BROWSER=1 PORT=8000 python3 server.py   # open http://localhost:8000
```

## Gotchas
- macOS zsh: no `pip` → `python3 -m pip` / `pip3`. Python 3.9, `openai>=1.0.0` unpinned.
- `/api/models` falls back to static list if MaxPlus fetch fails.
- No `EXA_API_KEY` → websearch honestly says blocked.
- History cap 60 in UI; compaction at 60 msgs / 80k chars, keeps newest 20.
- EventSource 404s permanently on error — hence the 10s server-side wait.
- Steer/interrupt on a run known only to SQLite (restarted server) → 404, send as new chat.
- Cron tick is a daemon thread: jobs run only while server is up. Min interval `every 60s`.
- No `node` on this machine — JS parsed with system `jsc`
  (`/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc /tmp/main.js`;
  expect only a runtime `document` ReferenceError). Extract script first:
  `python3 -c "open('/tmp/main.js','w').write(open('index.html').read().rsplit('<script>',1)[1].rsplit('</script>',1)[0])"`.
  Found 2026-10-03: caught a real stray-`}` that naive bracket-counting missed.
- Stream chunk granularity is provider-side: short replies may arrive as 1 chunk.
- Thinking is model-driven (`reasoning_content`): GLM emits it, other models may not.
- User has ADHD → keep reports short, tables over prose.

## Suggested next steps
- Background review (post-turn learning into memory/skills) — last big Hermes gap.
- Real MCP server config to make `mcp_*` tools real.
- Auth/token if server ever binds beyond localhost.
- `cron` pause/resume verbs (only lifecycle gap vs Hermes `cronjob`).
- `/skin` + `/theme` slash commands (picker exists in titlebar).
- Approval cards allow-once/session/always (deny-only on web today).
- Context usage ring in composer footer.
- Decided against: `bootstrap.py` (2 commands work fine), gateway/Telegram, plugins, ACP,
  cloud backends, training, voice, workspace browser, Tasks panel, imagegen/TTS/X-search.
