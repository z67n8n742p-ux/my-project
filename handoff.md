# Handoff — formyproject agent

## What this is
Localhost coding-agent clone of opencode tools + desktop ideas, backed by **MaxPlus AI**
(OpenAI-compatible). Python backend, single-file zero-dep web frontend, personal system prompt.
Ported opencode-v2 long-task loop: inbox idempotency, steer, retry, compaction, SSE, SQLite.
Plus Hermes-pattern glow-ups: registry, backends, approval, memory, fallback,
parallel tools, writable skills, cron, export-with-tools, highlight, skins.
Plus 2026-10 sessions: hardening, `process` + `extract` tools (23 total), live token +
thinking streaming with interrupt-mid-generation, chat UX overhaul (pinned scroll-follow,
per-turn Activity groups, Send/Stop), opt-in token auth, real MCP stdio client,
README in hermes-webui shape + UI screenshot.
Plus 2026-10-04 Hermes-gap pass (fixes 1–5): two-phase chat, approval cards,
session LIMITs, compact echo guard, streaming perf.
Plus 2026-10-04 late batch (all committed): Spaces-lite project switcher
(`GET+POST /api/workspaces`, per-project checkpoints, agent workspace note),
12 skins + font-size axis, 📎 chat attachments (`POST /api/attach` vault),
cron `update` verb + completion toasts, `/skin` + `/workspace` slash aliases,
emoji de-slop (mono badges, text buttons), mic wave card (Web Audio analyser),
stability pass (root delete/rename guards, TERMS_LOCK fix, threaded cron tick,
180s LLM timeouts, atomic state writes, RUNS prune + channel caps).

Upstreams (remote only, NOT vendored): `github.com/anomalyco/opencode/tree/v2`
(tools + loop + motion), `github.com/nousresearch/hermes-agent` (architecture patterns),
`github.com/nesquena/hermes-webui` (skins, streaming/thinking cards, README shape).
Local clone studied at `/tmp/.../hermes-webui` (master) — see bottom for borrowable leftovers.

## Layout
- `server.py` (2097) — backend (`ThreadingHTTPServer`, `FORMY_HOST` default `127.0.0.1:8000`).
  Endpoints: `GET /`, `GET /api/models`, `GET /api/system`, `POST /api/system`,
  `POST /api/chat` (blocking, compat), `POST /api/chat/start` (202, two-phase),
  `GET /api/chat/result?run=<id>`, `POST /api/steer`, `POST /api/interrupt` (Esc/Stop),
  `GET /api/approval/pending?run=<id>`, `POST /api/approval/respond`,
   `GET /api/progress?run=<id>`, `GET /api/event?run=<id>` (SSE),
   `GET /api/session?id=<session-id>`, `GET /api/skills`,
   `GET /api/workspaces`, `POST /api/workspaces` (list/add/use/remove),
   `POST /api/attach` (per-session vault under `.sessions/attachments/`),
   `POST /api/compact` (summary via SUMMARY_SYSTEM),
   workspace: `GET /api/files?path=` (+git at root), `GET /api/file?path=`,
   `GET /api/raw?path=` (+`&download=1`), `POST /api/file|/api/mkdir|
   /api/delete|/api/rename` (active-root containment + root delete/rename
   refusal + `_sensitive_path`).
  Retry 1+4 (429/5xx), `_bound_for_model` (spill full to `.jobs/`),
  `_drain_steer`, run/item idempotency (+ SQLite cross-restart), per-call isolation,
  `_maybe_compact` per step + `_strip_compact_echo` on replies, boot recovery
  (stale `running` → `interrupted`).
  `/api/event` waits 10s for run to exist, emits `stream`/`stream_full` +
  `think`/`think_full` + `mark` + `approval` + `result` + `data` + `done`,
  15s heartbeat, replays pens + pending approval on reattach.
  `_chat_admit` shared by blocking + start; `_execute_chat` core never raises.
  `_model_chain` + fallback (`FORMY_FALLBACK_MODELS`), OpenAI `timeout=180`,
  parallel `run_tool_calls`
  (order-restored, ctx-aware), `_cron_tick` daemon (30s, one thread per due job)
  + overlap guard.
  Helpers: `_ws_active()` (Spaces-lite root, default ROOT), `_atomic_write`
  (tmp+rename for workspaces/checkpoints), `_prune_runs` (10min, on insert +
  progress), `_emit_run` channel cap 2000. `send_json` swallows broken pipes;
  `serve_forever` reports port-in-use cleanly.
  Streaming: `_chat_stream_with_retry` + `_consume_chat_stream` (token/reasoning
  deltas, `mark` step/restart, per-chunk interrupt, partial kept). Thinking persisted
  per step as `thinking` tool entry. POST cap 10MB → 413. Messages table pruned to
  20k rows (was unbounded).
  Auth: `FORMY_TOKEN` guards `/api/*` (Bearer header or `?token=`, SSE uses query);
  `/` exempt so UI loads + prompts. Non-loopback bind without token refuses to start.
- `agent.py` (257) — CLI (`python3 agent.py [--model X]`). Same retry/compaction +
  fallback + parallel (question sequential, stdin). Non-streaming. Unaffected by
  web approval (ctx=None → old deny/ask).
- `tools.py` (1794) — **23 tools** + `compact_messages()` helper.
  `websearch` = Exa first, DuckDuckGo fallback (bot-blocked, fails honestly).
  `memory`, `session_search`, `skill_manage`, `cron`, `process`, `extract`.
  `session_search` jsonl fallback bounded (newest 200 files × 200 lines).
  `process` = bg jobs (`list|poll|wait|log|kill|write`, 64 receipts, logs survive restarts).
  `extract` = pdf (raw+flate, stdlib zlib) + docx/docm (stdlib zip); scanned PDFs refused.
  MCP: stdio JSON-RPC client (`initialize`/`resources/list`/`resources/read`, timeouts),
  config `.mcp.json` (git-ignored, read per call) + `.mcp.example.json`. Honest empties.
  Infra: `TOOLSETS` (6) + `get_tool_definitions()` + `run_tool_calls(calls, ctx)`
  (threads, order-restored; ctx={`run_id`,`sid`} only from live web runs).
  `bash(..., ctx)` → `approval.check_web` when ctx, else old `check()`.
  Guards: webfetch loopback/metadata block; `_sensitive_path` on write/edit/apply_patch
  (`FORMY_FILE_SCOPE=allow` bypasses).
- `approval.py` (212) — `DANGEROUS_PATTERNS` (rm -rf, mkfs, DROP, curl|sh…).
  Web = pending card + block (180s timeout → deny, `FORMY_APPROVAL_TIMEOUT`);
  `respond` = once/session/always(→`.sessions/approvals.json`)/deny.
  CLI TTY = prompt; `FORMY_APPROVAL=allow` bypasses. Emitter registered by server
  (`set_emitter`); unregistered → immediate deny (cron/subagent safe).
- `backends.py` (95) — local/docker/ssh per call (no restart), `shlex` quoting,
  no-shell remote. Default local, silent fallback.
- `prompt.py` (82) — `SOUL.md` + `AGENTS.md` + UI text + memory snapshot
  (**live** re-read every turn). Never raises; `/api/system` edits `system_prompt.md`.
- `cron.py` (267) — `.cron/jobs.json` (atomic tmp+rename writes), `every <N>s|m|h` + `daily@HH:MM`,
  CLI (`list|add|remove|pause|resume|update`), transcripts `.cron/runs/`, sessions `cron-<id>`.
  Max 20 jobs, prompt cap 2000, min `every 60s`. Daemon = server-up only.
- `index.html` (2243) — whole frontend, zero deps. No emoji: mono badges
  (`doc sh src web agt tool sub thk`), text buttons (Files/Prefs/Mic/File/Snaps).
  Tools inline in chat grouped per turn into collapsible **Activity** (collapsed by
  default, `Activity: N tools`). Pinned scroll-follow (reads mid-run never yank; `↓` pill fades in/out;
  `stableMutate` holds place on re-render). Live token bubbles (10fps incremental
  appends, caret span, md on done), thinking cards (3fps, skipped when collapsed),
  approval card (pop-in, allow once/session/always/deny + 1.5s pending poll),
  ● LIVE pill, Stop button + Esc interrupt. Two-phase send: `POST /start` (fast) →
  SSE `result` event, `/api/chat/result` poll backstops drops (native ES reattaches,
  server replays). Sessions sidebar (search, rename, export w/ tools, date groups,
  quota-safe store). 7 skins (titlebar picker, `formy.skin`). Token passthrough +
  401 prompt-to-save (`formy.token`). Keyword+number highlight, hover copy (fade),
   button press scale, keyboard-togglable headers, responsive titlebar + <900px layout.
   P1 (2026-10-04): per-msg HH:MM timestamps (full date hover), user Edit + assistant
   ↻ Retry (truncate + resend via send(), no server change), per-block Copy→Copied!,
   ctx footer (est tokens · % · ~$ + fill bar, model-aware windows).
   P2-split (2026-10-04): prism-lite highlight (py/js/bash/json/sql, string-first
   tokenizer, zero-dep) + distinct subagent card (🛰️, indented).
   Mermaid (2026-10-04, on request): ```mermaid fences render to SVG via lazy CDN
   (pinned 11.17.2 + SRI, `securityLevel:strict`); code fallback offline; 3.5MB
   never vendored, loads once on first diagram.
   Sessions batch (2026-10-04): ⋯ menu (pin/archive/duplicate/rename/export/
   delete), ★ pin sort, archive + toggle, collapsible date groups, tab title,
   #tag chips + click-filter, content search, JSON export/import, ctx click for
   in/out detail. Skipped: projects (tags cover it), share link (localhost),
   CLI bridge (agent.py doesn't log store.db).
   Slash (2026-10-04): composer autocomplete (↑↓/Tab/Enter/Esc), built-ins
   /help /clear /compact|/compress [focus] /model /new /usage /theme, skills
   from GET /api/skills (substring match, built-ins win; unknown incl. skills
   pass through to agent). POST /api/compact summarizes via SUMMARY_SYSTEM.
   /workspace skipped (single repo root). Server restarted (new endpoints).
   Workspace (2026-10-04): inspector Files tab (System|Files, drag-resize 220–
   700px persisted) — lazy tree (click toggle, dbl-click nav), breadcrumbs,
   preview md/code(img inline, binary→download), edit+save (dirty guard),
   +File/+Dir/rename/delete, ⎇ branch·dirty badge, `workspace://` chat links
   open in preview. Server restarted (files/file/raw/mkdir/delete/rename).
   Power batch (2026-10-04): Tasks tab (cron list/add/run/pause/resume/delete
   + run transcripts open in preview; cron.py pause|resume verbs + CLI),
   workspace upload (base64 JSON ≤5MB), rollback checkpoints (snapshot/diff/
   restore via git, store `.jobs/checkpoints.json`; create uses `git add -u`
   so untracked files stay out), send-key toggle (Enter vs ⌘/Ctrl+Enter),
   explicit light/dark/system theme, ⧉ collapse-all tools. Server restarted.
    Final three (2026-10-04): voice mic (Web Speech, frontend-only, hidden if
    unsupported) + wave card (Web Audio analyser, red dot + live wave + timer,
    `no mic` fallback), terminal Tab (real pty bash, ANSI colors, 1s poll, typed cmds
    need no approval — approval is for agent actions; max 4, idle reap 30min,
    New kills old pty, dies with server), Skills tab (list/search/preview/edit/new/delete on
    existing file endpoints, same skills/ dir). Server restarted (term).
  XSS: `textContent` + escaped-first md.
- `SOUL.md` / `AGENTS.md` — identity + conventions (were one `system_prompt.md`).
- `system_prompt.md` — legacy layer, UI-editable, wrapped by builder.
- `memories/MEMORY.md` + `memories/USER.md` — seeded 2026-10, tracked in git.
- `skills/` — empty (agent-writable, traversal-guarded, 24k cap).
- `README.md` — hermes-webui shape (Why/table, quickstart, features, config,
  remote, MCP, tests, mermaid architecture, roadmap) + `docs/images/ui-chat.png`.
- `.env` — keys (git-ignored). `.env.example` documents all `FORMY_*` + `MCP_TIMEOUT`.
- `opencode-v2-explained.md` (223) — v2 guide + Hermes comparisons + file map.
- `todo-app/` — sample app, 13/13 green (`cd todo-app && python3 -m unittest`).
- GitHub: `https://github.com/z67n8n742p-ux/my-project.git` (branch `main`,
  pushed through `93b9d12` stability pass).
  `markdown-test.md` intentionally untracked/local.

## Key facts
- Server RUNNING (PID 85189, `auth=off, loopback only`, stability-pass code).
  Start: `NO_BROWSER=1 PORT=8000 nohup python3 server.py > server.restart.log 2>&1 &`,
  stop: `pkill -f server.py`. Restart needed for `server.py`/`tools.py`/`cron.py` changes only.
- Frontend served from disk per request → hard-refresh (`Cmd+Shift+R`,
  Safari `Option+Cmd+R`) after UI edits.
- SQLite `.sessions/store.db` — tables `runs` + `messages` + `messages_fts` (FTS5,
  trigger-kept; NO `claims` table — idempotency is `runs.run_id` + JSONL transcripts).
  Messages pruned to newest 20k on every turn; RUNS pruned at 10min on insert +
  progress; SSE channels capped at 2000 entries per run.
- Interrupt is cooperative between steps AND mid-generation (partial kept +
  `*(interrupted by user)*`); never mid-tool. Approval wait (≤180s) ends at next
  step boundary on interrupt.
- `question` excluded from web; CLI blocks on `input()`.
- `subagent` bounded 6 steps, no recursion, no `question`, no approval ctx (deny-only).
- Approval e2e-verified 2026-10-04 (`rm -rf` parked → denied → never executed).
  Two-phase e2e-verified same day (start → result poll → `reply: ok`; SSE replay
  `stream_full`+`think_full`+`result`+`done`).
- Deleted earlier: `loop.py`, `p0-test-report.md` (junk, `git rm`).
- Git-ignored: `.env`, `.mcp.json`, `server.log`, `server.restart.log`, `.todos.json`,
  `.jobs/`, `.sessions/`, `.cron/`, `.venv/`, `__pycache__/`.

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
- EventSource auto-reconnects natively — hence server-side replay + 10s run wait.
- Steer/interrupt/respond on a run known only to SQLite (restarted server) → 404,
  send as new chat. `result` falls back to SQLite after RUNS eviction (10min).
- RUNS entries now also carry `tools` + `interrupted` + `sid` (SSE result + approval scope).
- No `node` — JS parsed with system `jsc`
  (`/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc`;
  extract script to `/tmp/main.js` first, expect only runtime `document` ReferenceError).
  2026-10-03: caught a real stray-`}` that naive bracket-counting missed.
- Stream chunk granularity is provider-side: short replies may arrive as 1 chunk.
- Thinking is model-driven (`reasoning_content`): GLM emits it, other models may not.
- Remote = SSH tunnel (`ssh -L 8000:127.0.0.1:8000`), paired with `FORMY_TOKEN`.
- Bigger perf lever if lag returns: cap live bubble to last N chars while streaming
  (full md only at finish).
- User has ADHD → keep reports short, tables over prose.

## Suggested next steps
- Turn journal (#6, optional) — audit trail separate from transcript; only if forensics wanted.
- Deferred hardening (known, needs redesign not patch): approval cards keyed per
  run (4 parallel dangerous calls can collide), MCP/bg-job fd cleanup,
  `read`/`extract` size caps, mermaid SVG sanitizer, RUNS_LOCK across SQLite I/O.
- Hermes leftovers still skipped: session projects (tags cover it), share link
  (localhost), extension dir (`registerHermesSkin`), CLI bridge logging.
- Decided against: `bootstrap.py` (2 commands work fine), gateway/Telegram, plugins, ACP,
  cloud backends, training, imagegen/TTS/X-search.
