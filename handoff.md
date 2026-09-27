# Handoff — formyproject agent

## What this is
Localhost coding-agent clone of opencode tools + desktop ideas, backed by **MaxPlus AI**
(OpenAI-compatible). Python backend, single-file web frontend, personal system prompt.
This session ported 5 opencode-v2 algorithms (P0), added Exa websearch, and added
minimal backend session history.

## Layout
- `server.py` — localhost backend (`ThreadingHTTPServer` on `127.0.0.1:8000`).
  Endpoints: `GET /`, `GET /api/models`, `GET /api/system`, `POST /api/system`,
   `POST /api/chat`, `POST /api/steer`, `POST /api/interrupt` (Esc),
   `GET /api/progress?run=<id>`, `GET /api/event?run=<id>` (SSE),
  `GET /api/session?id=<session-id>`.
  Logs every request to console + `server.log`. Model list cached 120s.
  P0 logic: `_is_retryable` + `_chat_create_with_retry` (429/5xx only, 1+4 backoff),
  `_bound_for_model` (spill full to `.jobs/`, preview to model), `_drain_steer`
  (Safe Step Boundary), run/item idempotency, per-call tool isolation,
  `_save_turn` → `.sessions/<id>.jsonl`.
- `agent.py` — CLI (`python3 agent.py [--model X]`). Same retry/bound/isolation as server.
- `tools.py` — **17 tools**: bash, edit, write, read, grep, glob, lsp, apply_patch,
  skill, todowrite, webfetch, websearch, question, subagent, models,
  mcp_list_resources, mcp_read_resource. Schemas in `TOOLS_SPECS`,
  executed via `execute_tool()`. `websearch` = Exa first (`_exa_search`,
  `POST api.exa.ai/search`, highlights), DuckDuckGo fallback (currently bot-blocked,
  fails honestly). `_bound_text` in bash/grep/webfetch.
- `index.html` — whole frontend (chat, sessions sidebar, model picker, inspector,
  markdown renderer, animations, live tool progress). Sends `run+item_id+session`
  per chat; follow-ups while running go to `/api/steer` (`steerBuffer`).
- `system_prompt.md` — agent system prompt (editable in UI, saved via API).
- `.env` — `MAXPLUS_API_KEY`, `MAXPLUS_BASE_URL`, `MAXPLUS_MODEL`, `PORT`,
  `EXA_API_KEY` (Exa key from user). Git-ignored. See `.env.example`.
- `opencode-v2-explained.md` — v2 logic guide (§1-13) + how-each-part-works-RN (§14)
  + P0 implemented notes (§15).
- `p0-test-report.md` — short test report: 44/44 incl. all-tool error sweep,
  websearch evaluation, live subagent/question/steer-mid-run results.
- `requirements.txt` — just `openai>=1.0.0` (stdlib otherwise).

## Key facts for next session
- Server left **running** on `:8000` (PID 9346 at write time; `pkill -f server.py` to stop).
- Restart cmd: `NO_BROWSER=1 PORT=8000 nohup python3 server.py > server.restart.log 2>&1 &`
- Frontend reads `index.html` from disk per request → no restart for UI edits,
  but server restart needed for `server.py`/`tools.py` changes (imports at startup).
- `question` excluded from web mode; CLI blocks on `input()` (tested via piped stdin).
- `subagent` bounded 6 steps, no recursion, no `question`. Live-tested (`pineapple` OK).
- Steer-mid-run live-tested: `bash sleep 6` + steer `banana` → reply `done banana`.
- Idempotency live-tested: resend same RID → `deduped:true`, no 2nd LLM call.
- Sessions: browser `localStorage` is source of truth; backend `.sessions/*.jsonl`
  mirrors finished turns (2 lines/turn). `.sessions/` currently empty (probe cleaned).
- Exa key is in `.env` (user pasted in chat — rotate at dashboard.exa.ai if log shared).
- `apply_patch` Move-truncation bug: **fixed**. `system` vs `sys` ReferenceError: **fixed**.

## Run
```
python3 -m pip install -r requirements.txt
NO_BROWSER=1 PORT=8000 python3 server.py   # open http://localhost:8000
```

## Gotchas
- macOS zsh: no `pip` → use `python3 -m pip` / `pip3`.
- Hard-refresh (`Cmd+Shift+R`) after frontend edits (browser caches `index.html`).
- `/api/models` falls back to static list if MaxPlus fetch fails.
- DuckDuckGo fallback is bot-blocked (returns 14KB check page, 0 results) — Exa is
  the real search; without `EXA_API_KEY`, websearch honestly says blocked.
- Runtime files (git-ignored): `server.log`, `server.restart.log`, `.todos.json`,
  `.jobs/`, `.sessions/`, `.env`.
- User has ADHD → keep reports short, tables over prose.

## Suggested next steps
- Auto-continue for long tasks (re-wake while todos open) — offered, awaiting user.
- SSE to replace 800ms progress polling.
- Syntax highlighting in markdown renderer.
- Session export includes tool logs (currently transcript only).
- MCP server config to make `mcp_*` tools real.
- Auth/token if server ever binds beyond localhost.
