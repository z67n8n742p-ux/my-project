# How opencode v2 Does Long Tasks — Simple Guide

Source: `https://github.com/anomalyco/opencode/tree/v2` (branch `v2`)
Specs: `specs/v2/README.md`, `session.md`, `tools.md`

## Big idea

Long task = small safe steps in a loop.
Never inject new input mid-step. Queue it, deliver at safe points.

```
you type -> inbox -> runner loop -> tools -> repeat -> done
```

## The 6 parts

| # | Part | What it does | Easy picture |
|---|---|---|---|
| 1 | Inbox | New messages wait in a queue first | Email inbox, not open letter |
| 2 | Safe boundary | New input enters only between steps | Wait for red light, then cross |
| 3 | Step loop | 1 LLM call + its tools = 1 step, repeat | One bite at a time |
| 4 | Retry | Only 429 / 5xx / broken stream retry, max 1+4 with backoff | Try again, not forever |
| 5 | Compaction | Old history → short summary, keep new as-is | Notes, not full book |
| 6 | Recovery | Save claim before run, resume after crash | Bookmark in book |

## Rules opencode uses

- Same Session ID = same session. Same message ID = ignore repeat.
- `steer` = deliver at next safe point. `queue` = wait until idle.
- 1 step can retry inside itself without new input.
- 1 bad tool call fails alone. Other tools keep running.
- Tool output: save full text to file, send short preview to model.
- Crash: mark old `running` tools as `failed`, then continue. No magic replay.

## How it maps to this project (current)

| v2 idea | Here today |
|---|---|
| Inbox + IDs | Done: `run + item_id`, resend = `deduped:true` (mem + SQLite cross-restart, 409 if in-flight) |
| Safe boundary | Done: `/api/steer` + `queue` mode, `_drain_steer()` between steps |
| Retry 1+4 | Done: `_chat_create_with_retry`, 429/5xx only, never 4xx; plus `_model_chain` fallback (`FORMY_FALLBACK_MODELS`) |
| Bound output | Done: 8k cap, head 4k + tail 4k, full spill to `.jobs/` |
| 1 bad tool isolation | Done: per-call try/except + `run_tool_calls()` (parallel, order-restored) |
| Compaction | Done: `compact_messages()`, per-step check at 60 msgs / 80k chars, keeps newest 20, never splits tool pairs |
| SSE events | Done: `GET /api/event` stream + `/api/progress` compat + 10s wait for run race |
| Save to disk | Done: SQLite `.sessions/store.db` (runs + messages + FTS5), jsonl turns, boot recovery, cron overlap guard |
| POST safety | Done: 10MB cap → 413, bad `Content-Length` → 400, never crashes |

---

# Hermes-agent vs formyproject — coding-agent deep comparison

Source: `https://github.com/nousresearch/hermes-agent` (docs: architecture, agent-loop, tools-runtime, session-storage, memory).
Here = `server.py` + `agent.py` + `tools.py` + `backends.py` + `approval.py` + `prompt.py` + `cron.py` + `index.html`.

## 1-line difference

| | Hermes | formyproject |
|---|---|---|
| Goal | Self-improving general agent on every platform | Single-user localhost coding agent (MaxPlus AI) |
| Size | Dozens of modules, 70+ tools, ~28 toolsets | 7 Python files, 23 tools, 6 toolsets, 1 test app (`todo-app/`, 13 tests green) |

## Agent loop

| | Hermes (`AIAgent` + `turn_*.py`) | Here (`run_agent()` in `server.py`, `chat_loop()` in `agent.py`) |
|---|---|---|
| Steps | `chat()` → `run_conversation()`; budget 500 turns, subagent cap 50 | `max_steps=12` (server), 20 (CLI). Subagent cap 6, no recursion, no `question` inside |
| Interrupts | Threaded `_interruptible_api_call` — cancels mid-API-call | Cooperative flag — stops only between steps, never mid-tool |
| Tool parallelism | `ThreadPoolExecutor`, order-restored | Same: `run_tool_calls()` (ThreadPoolExecutor ≤4, order-restored); single call runs inline |
| API modes | 3: `chat_completions` / `codex_responses` / `anthropic_messages` | 1: `chat.completions` via `openai.OpenAI` to MaxPlus only |
| Failure | Retry + fallback-provider chain + credential refresh | Retry 1+4 on 429/5xx + `_model_chain` fallback (`FORMY_FALLBACK_MODELS`); never 4xx |
| Steer | New message / `/stop` aborts API thread | `/api/steer` queued, `_drain_steer()` at safe boundary; `/api/interrupt` sets flag; `queue` mode parks while busy |
| Idempotency | `task_id` + gateway ownership + backlog | `run + item_id` in-mem `RUNS{}` + SQLite cross-restart + 409 if in-flight |
| Callbacks | 8 surfaces (tool_progress / thinking / clarify / stream_delta …) | 4 SSE channels: `stream` (tokens) + `think` (reasoning) + `mark` (step/restart) + tool-log `data` → `/api/event`; `/api/progress` kept for compat |

## Prompt assembly

| | Hermes (`prompt_builder.py`, 3 tiers) | Here (`prompt.py:build_system_prompt()`) |
|---|---|---|
| Tiers | `stable` → `context` → `volatile`, frozen mid-conversation | Same 3 tiers: `SOUL.md` + `AGENTS.md` → UI `system_prompt.md` → live `MEMORY` + `USER` snapshot |
| Caching | Anthropic cache breakpoints + `prompt_caching.py` | None |
| Ephemeral layers | Budget/context-pressure warnings per turn | Memory usage % shown so the model self-limits; live re-read every turn (not frozen) |

## Context / compaction

| | Hermes | Here (`compact_messages()` in `tools.py`, `_maybe_compact` per step) |
|---|---|---|
| Trigger | Preflight >50% window; gateway auto >85% | `>60 msgs` or `>80k chars` (CLI: 100k) |
| Keep | Last N intact, tool pairs never split | Keep newest 20, never split `tool` tail, never drop lead system msg |
| Method | Lossy compressor + pluggable `ContextEngine` + micro-compaction; flush memory first | One LLM summary (<300 words) → `[Auto-compacted summary]` system msg; failure → plain slice |
| Lineage | Forks child session, old rows `active=0` | In-place `messages[:] = new`, logged as `compact` tool entry |
| Bound output | Per-tool redaction + receipts | 8k cap, head 4k + tail 4k, full spill to `.jobs/` |

## Tools (21 vs 70+)

| | Hermes (`tools/registry.py` + `toolsets.py`) | Here (`tools.py`, `TOOLS_SPECS` + `TOOLSETS` + `get_tool_definitions()`) |
|---|---|---|
| Count | 70+ across ~28 toolsets, AST auto-discovery | 23 hand-listed across 6 toolsets (`files/shell/search/agent/mcp/all`) + `check_fn` availability gate |
| Files/shell | Full editors, PTY, process registry | `bash/edit/write/read/extract/grep/glob/apply_patch/lsp`(grep-fallback) + `process` (poll/wait/log/kill/stdin for bg jobs) |
| Agent | `todo/memory/session_search/delegate` intercept | `todowrite/cron/memory/session_search/skill/skill_manage/subagent/question/models` |
| Web | 4 backends + browser CDP supervisor | `webfetch` (loopback/metadata-blocked, regex strip) + `websearch` (Exa → DDG fallback, honest fail) + `extract` (text pdf/docx via stdlib, OCR refused honestly) |
| MCP | Dynamic `mcp_tool_discovery` from server config | 2 honest stubs (`mcp_list_resources`, `mcp_read_resource`) |
| Safety | `DANGEROUS_PATTERNS` + approve cards + allowlist + smart-LLM | `DANGEROUS_PATTERNS` (deny on web, prompt on CLI TTY, `FORMY_APPROVAL=allow` bypass) + sensitive-path guard on `write/edit/apply_patch` (`~/.ssh`, `/etc`, `/System`, keychains; `FORMY_FILE_SCOPE=allow` bypass) |
| Terminal | 7 backends: local/docker/ssh/singularity/modal/daytona/vercel | 3 backends: local/docker/ssh (`backends.py`, per-call resolution, best-effort workdir `cd`, `shlex` quoting, silent local fallback) |
| Dispatch | Pre-hook → `registry.dispatch` → post-hook, double error-wrap | `execute_tool()` if/else + `run_tool_calls()` parallel, per-call try/except |
| Async | `_run_async()` bridges CLI/gateway/worker threads | All sync (server parallelizes at thread-per-request + tool level) |

## Memory / skills (the former big gap — now closed, minus auto-review)

| | Hermes (closed learning loop) | Here (curated loop, no auto-review — deliberate) |
|---|---|---|
| Stores | `MEMORY.md` + `USER.md`, frozen snapshot, `§`-delimited | Same 2 files (`memories/`, 2200/1375 caps), live snapshot w/ usage % — seeded: 6 env facts (39%), 4 user prefs (20%) |
| Writes | Agent `memory` tool + `write_approval` gate + `/memory pending` | `memory` tool (add/replace/remove/list, dup reject, injection scan, over-budget → consolidate) |
| Background review | Post-turn fork learns lessons (cheaper model + digest) | Not built — you ARE the review on a laptop |
| Recall | `session_search`: FTS5 (~20ms) + Honcho/Mem0 optional | `session_search`: FTS5 → LIKE → jsonl fallback, read-only, never raises |
| Skills | Auto-create after complex tasks, self-patch, Skills Hub, `agentskills.io` compat | `skill()` reads + `skill_manage()` writes (traversal-guarded, 24k cap, patch-preferred); no auto-create, no hub |
| Context files | `SOUL.md` / `AGENTS.md` / project context per turn | Same: `SOUL.md` (who) + `AGENTS.md` (how) + UI text + memory |

## Session storage

| | Hermes (`hermes_state*.py`, `state.db` v31) | Here (`.sessions/`) |
|---|---|---|
| Engine | SQLite WAL: sessions + messages + usage + gateway/delivery/lock tables | SQLite WAL `store.db`: `runs` + `messages` + `messages_fts` (FTS5 w/ triggers, LIKE fallback) + per-session `.jsonl` turns |
| Search | 3× FTS5 + trigram + CJK, sanitizer, snippets, lineage | 1× FTS5 + snippet `>>>match<<<`, role filter, jsonl fallback |
| Meta | Tokens/cost, titles, source, user_id, cwd/branch, rewind/pin/archive, uids | Tool-name list per turn, `message_count` via jsonl; no tokens/cost, no pin/archive |
| Contention | 1s timeout + budgeted retry + `BEGIN IMMEDIATE` + checkpoint | `DB_LOCK` threading lock, 10s timeout (single process only) |
| Recovery | Migration chain v1→v31, stale-running → interrupted | `_store_init()`: stale `running` → `interrupted`, prune >24h; write-ahead claims per run |

## Serving / platforms

| | Hermes | Here |
|---|---|---|
| Entries | CLI + gateway + ACP + batch runner + API server + Python lib | `server.py` (`ThreadingHTTPServer` 127.0.0.1:8000, POST cap 10MB) + `agent.py` CLI (TTY `question`) |
| Chat surfaces | TUI + Telegram/Discord/Slack/WhatsApp/Signal/Email + dashboard + cron delivery | Single-file `index.html` (token streaming, thinking cards, steer-while-running, Esc incl. mid-generation, ● LIVE pill, export w/ tool logs) |
| Cron | First-class jobs, NL schedule, skill/script attach, any-platform delivery | `cron.py` + 30s tick daemon + overlap guard; `every <N>s\|m\|h` + `daily@HH:MM`; transcripts in `.cron/runs/`, sessions as `cron-<id>`; runs only while server is up |
| Plugins | `~/.hermes` / project / pip entry-points; memory + context-engine single-select | None (decided against) |
| Training | Trajectory gen (ShareGPT) + compression | None (decided against) |

## What was borrowed (all done)

1. ✅ `DANGEROUS_PATTERNS` + approve before `bash` (~30 lines → `approval.py`).
2. ✅ `check_fn` gating + `TOOLSETS` + `get_tool_definitions()` (`tools.py`).
3. ✅ `MEMORY.md`/`USER.md` + `memory` tool (seeded, injected every turn).
4. ✅ `session_search` FTS5 over SQLite (+ jsonl fallback).
5. ✅ Fallback model list (`FORMY_FALLBACK_MODELS`, tried in order).
6. ✅ Cron generalized from "auto-continue while todos open".
7. ✅ Parallel tools (`run_tool_calls`, order-restored).
8. ✅ Backends interface (`local/docker/ssh`, silent fallback).
9. ✅ Frontier hardening (2026-10): webfetch loopback block, sensitive-path file guard, POST 10MB cap, cron overlap guard, XSS fix in session search, per-call backend resolution.
10. ✅ Job control + docs (2026-10): `process` tool (poll/wait/log/kill/stdin over `bash background` jobs, 64-receipt retention) + `extract` (text-pdf/docx via stdlib, scanned PDFs refused honestly).

## Deliberately not copied

Gateway (Telegram/Discord/…), ACP, batch runner, plugins, training trajectories,
cloud backends (modal/daytona/vercel/singularity), Honcho/Mem0 providers,
background auto-review, prompt caching, billing/tokens, auth (localhost-only),
voice/mermaid/Tasks-panel UI. Revisit only on real pain.

---

# Hermes-webui vs formyproject frontend

Source: `https://github.com/nesquena/hermes-webui` (MIT). Skins ported from it.

| | hermes-webui | Here (`index.html`, 562 lines, zero deps) |
|---|---|---|
| Layout | 3-panel: sessions + chat + workspace browser | 3-panel: sessions + chat + system-prompt inspector (no file browser) |
| Composer | Model/profile/workspace pickers + context ring | Model picker (grouped, keyboard nav) + skin picker; no ring/profiles |
| Sessions | Pin/archive/projects/tags/share/CLI-bridge | Group by day, search (XSS-fixed), rename, delete, export w/ tool logs (cap 200, localStorage) |
| Chat | Token streaming, thinking cards, mermaid, voice, approval cards, slash commands | Token streaming (rAF-throttled live bubbles per turn), thinking cards (live + persisted), collapsible tool cards, steer-while-running, Esc, ● LIVE pill, `/`-ready placeholder text only |
| Settings | Control Center, password/OIDC/passkeys, providers UI | System-prompt textarea only; config lives in `.env` + `FORMY_*` |
| Themes | Theme × 11-skin matrix, server-persisted | 7 skins (`default/ares/mono/slate/poseidon/sisyphus/charizard`, `formy.skin` localStorage) + OS light/dark |
| Markdown | Full renderer + Prism.js | Dependency-free renderer (tables, task lists, fences + keyword/number highlight, copy button) |

---

# Can you copy Hermes patterns to your own laptop agent? Yes — done.

Hermes runs on laptop by default (`local` backend); the 7 backends are interfaces.
License is MIT (attribution kept in code comments). What "runs anywhere" means here:

| Backend | Needs | Here? |
|---|---|---|
| `local` | just your Mac | ✅ default, zero deps |
| `docker` | Docker Desktop | ✅ `FORMY_DOCKER_CONTAINER`, best-effort workdir |
| `ssh` | any remote box / $5 VPS | ✅ `FORMY_SSH_TARGET`, `BatchMode`, best-effort workdir |
| `singularity` | HPC clusters | ❌ skipped |
| `modal` / `daytona` / `vercel` | cloud accounts, hibernate-when-idle | ❌ skipped — disk IS persistence on a laptop |

## File map (where each Hermes idea lives now)

| Hermes file | Your file | Status |
|---|---|---|
| `agent/conversation_loop.py` | `server.py:run_agent()` + `agent.py:chat_loop()` | Done (12/20-step budgets, parallel tools) |
| `tools/registry.py` | `tools.py:get_tool_definitions()` + `TOOLSETS` | Done |
| `tools/approval.py` | `approval.py` + `_sensitive_path()` in `tools.py` | Done + extended to file writes |
| `agent/prompt_builder.py` | `prompt.py:build_system_prompt()` | Done (3 tiers, live snapshot) |
| `hermes_state.py` | `.sessions/store.db` (`runs` + `messages` + FTS) | Done |
| `agent/memory_manager.py` | `memories/` + `memory` tool | Done (minus auto-review) |
| `tools/environments/` | `backends.py:run()` | Done (3 of 7) |
| `cron/jobs.py` | `cron.py` + `_cron_tick()` in `server.py` | Done + overlap guard |
| webui skins/themes | `index.html` `<style>` + `#skinPick` | Done (7 of 11) |

## Laptop limits (honest, unchanged)

- No GPU-cluster training, no 24/7 Telegram bot unless laptop stays awake — fine for coding agent.
- Big contexts (200k+) are slower on laptop RAM — 60-msg / 80k-char compaction is well-tuned for this.
- Docker backend needs Docker Desktop running (~2GB RAM). Local backend needs nothing.
- `openai>=1.0.0` unpinned; Python 3.9 stdlib + `openai` only. No `node` on this machine — JS checked by bracket-balance vs git HEAD.

## Run

```
python3 -m pip install -r requirements.txt
NO_BROWSER=1 PORT=8000 python3 server.py   # open http://localhost:8000
```

## Remaining gaps (only on real pain)

Background auto-review, real MCP config, auth (only if ever bound beyond localhost),
`/skin` + `/theme` slash commands, approval allow-once cards,
context-usage ring. Decided against: `bootstrap.py`, gateway/Telegram, plugins, ACP,
cloud backends, training, voice, workspace browser, Tasks panel.
