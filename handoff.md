# Handoff — formyproject agent

## What this is
Localhost coding-agent clone of opencode tools + desktop ideas, backed by **MaxPlus AI**
(OpenAI-compatible). Python backend, single-file web frontend, personal system prompt.
Ported opencode-v2 long-task loop: inbox idempotency, steer, retry, compaction, SSE, SQLite.

## Layout
- `server.py` — backend (`ThreadingHTTPServer` on `127.0.0.1:8000`).
  Endpoints: `GET /`, `GET /api/models`, `GET /api/system`, `POST /api/system`,
  `POST /api/chat`, `POST /api/steer`, `POST /api/interrupt` (Esc),
  `GET /api/progress?run=<id>`, `GET /api/event?run=<id>` (SSE),
  `GET /api/session?id=<session-id>`.
  P0: retry 1+4 (429/5xx), `_bound_for_model` (spill full to `.jobs/`),
  `_drain_steer`, run/item idempotency (+ SQLite cross-restart), per-call isolation.
  P1: `_maybe_compact` per step, write-ahead claims, boot recovery.
- `agent.py` — CLI (`python3 agent.py [--model X]`). Same retry/compaction.
- `tools.py` — **17 tools** + `compact_messages()` rolling-summary helper.
  `websearch` = Exa first, DuckDuckGo fallback (bot-blocked, fails honestly).
- `index.html` — whole frontend. Tools render **inline in chat** (desktop-style),
  inspector keeps system prompt only. Live updates via EventSource, Esc interrupts.
- `system_prompt.md` — agent system prompt (editable in UI).
- `.env` — keys (git-ignored). See `.env.example`.
- `opencode-v2-explained.md` — short v2 guide (50 lines). All 3 next-steps done.
- `p0-test-report.md` — older test report (P0 era).
- GitHub: `https://github.com/z67n8n742p-ux/my-project.git` (branch `main`).

## Key facts
- Server running `:8000` (PID 10841). `pkill -f server.py` to stop.
- Restart: `NO_BROWSER=1 PORT=8000 nohup python3 server.py > server.restart.log 2>&1 &`
- Frontend served from disk per request → hard-refresh (`Cmd+Shift+R`) after UI edits.
- Server restart needed for `server.py`/`tools.py` changes.
- SQLite `.sessions/store.db` — runs + claims. Stale `running` → `interrupted` on boot.
- Interrupt is cooperative: stops between steps, never mid-tool.
- `question` excluded from web; CLI blocks on `input()`.
- `subagent` bounded 6 steps, no recursion, no `question`.
- Git-ignored: `.env`, `server.log`, `server.restart.log`, `.todos.json`,
  `.jobs/`, `.sessions/`, `.venv/`, `__pycache__/`.

## Run
```
python3 -m pip install -r requirements.txt
NO_BROWSER=1 PORT=8000 python3 server.py   # open http://localhost:8000
```

## Gotchas
- macOS zsh: no `pip` → `python3 -m pip` / `pip3`.
- `/api/models` falls back to static list if MaxPlus fetch fails.
- No `EXA_API_KEY` → websearch honestly says blocked.
- History cap 60 in UI; compaction at 60 msgs / 80k chars, keeps newest 20.
- User has ADHD → keep reports short, tables over prose.

## Suggested next steps
- Syntax highlighting in markdown renderer.
- Session export includes tool logs (currently transcript only).
- MCP server config to make `mcp_*` tools real.
- Auth/token if server ever binds beyond localhost.
- Auto-continue for long tasks (re-wake while todos open).
