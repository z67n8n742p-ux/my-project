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

## How it maps to this project

| v2 idea | Here today |
|---|---|
| Inbox + IDs | Done: `run + item_id`, resend = `deduped:true` |
| Safe boundary | Done: `/api/steer`, drain between steps |
| Retry 1+4 | Done: `_chat_create_with_retry`, 429/5xx only |
| Bound output | Done: `_bound_for_model`, full in `.jobs/` |
| 1 bad tool isolation | Done: try/except per call |
| Compaction | Done: `compact_messages` in `tools.py`, per-step check, 62→22 live-verified |
| SSE events | Done: `GET /api/event` stream + frontend EventSource, `/api/progress` kept for compat |
| Save to disk | Done: SQLite `.sessions/store.db` (runs + write-ahead claims, boot recovery), jsonl turns |

## What to do next (in order)

1. ~~Compaction~~ done. ~~SSE~~ done. ~~SQLite~~ done.
2. Next options: syntax highlighting, session export with tool logs, real MCP config.

---

# Hermes-agent vs formyproject — coding-agent deep comparison

Source: `https://github.com/nousresearch/hermes-agent` (docs: architecture, agent-loop, tools-runtime, session-storage, memory).
Here = `server.py` + `agent.py` + `tools.py` + `index.html` (opencode-v2 port).

## 1-line difference

| | Hermes | formyproject |
|---|---|---|
| Goal | Self-improving general agent that lives on every platform | Minimal localhost coding-agent clone of opencode-v2 |
| Size | ~47k commits scale, 70+ tools, 28 toolsets, ~25k tests | 3 Python files, 17 tools, 1 test app (`todo-app/`) |

## Agent loop

| | Hermes (`AIAgent` in `agent/conversation_loop.py` + `turn_*.py`) | Here (`run_agent()` in `server.py:345`, `chat_loop()` in `agent.py:117`) |
|---|---|---|
| Steps | `chat()` → `run_conversation()`; budget 500 turns, subagent cap 50 | `max_steps=12` (server), 20 (CLI). Subagent cap 6, no recursion |
| Interrupts | Threaded `_interruptible_api_call` — cancels mid-API-call | Cooperative flag `_is_interrupted()` — stops only between steps, never mid-tool |
| Tool parallelism | `ThreadPoolExecutor` for multi-calls, order-restored | Sequential `for tc in msg.tool_calls` |
| API modes | 3: `chat_completions` / `codex_responses` / `anthropic_messages`, auto-resolved | 1: `chat.completions` via `openai.OpenAI` to MaxPlus only |
| Failure | Retry + fallback-provider chain + credential refresh + aux-task fallback | Retry 1+4 on 429/5xx only (`_chat_create_with_retry`), never 4xx |
| Steer | New message / `/stop` aborts API thread, re-routes | `/api/steer` queued, `_drain_steer()` only at safe boundary; `/api/interrupt` sets flag |
| Idempotency | `task_id` + gateway ownership markers + completion backlog | `run + item_id` in-mem `RUNS{}` + SQLite cross-restart (`_store_get`) + 409 if in-flight |
| Callbacks | 8 surfaces: tool_progress / thinking / reasoning / clarify / step / stream_delta / tool_gen / status | 1 surface: `live_logs` list → SSE `/api/event` + `/api/progress` poll |

## Prompt assembly

| | Hermes (`prompt_builder.py`, 3 tiers) | Here (`system_prompt.md`, 12 lines) |
|---|---|---|
| Tiers | `stable` (identity/tools/skills) → `context` (files) → `volatile` (memory/profile/time) | Single static system string, editable in UI |
| Stability rule | Frozen mid-conversation to preserve prefix cache; `/model` explicitly breaks it | Rebuilt per `/api/chat` from `system + history[-60:]` |
| Caching | Anthropic cache breakpoints + `prompt_caching.py` | None |
| Ephemeral layers | Budget/context-pressure warnings injected per turn | None |

## Context / compaction

| | Hermes | Here (`compact_messages()` in `tools.py:751`, `_maybe_compact` per step) |
|---|---|---|
| Trigger | Preflight >50% window; gateway auto >85% | `>60 msgs` or `>80k chars` (CLI: 100k) |
| Keep | Last N intact (`protect_last_n=20`), tool pairs never split | Keep newest 20, never split `tool` role tail, never drop lead system msg |
| Method | Lossy `context_compressor.py` + pluggable `ContextEngine` ABC + micro-compaction; flush memory first | One LLM summary (`SUMMARY_SYSTEM`, <300 words) → `[Auto-compacted summary]` system msg; failure → plain slice |
| Lineage | Compression forks child session (`parent_session_id`), old rows `active=0` | No fork; in-place `messages[:] = new` |
| Bound output | Same idea, richer: per-tool redaction + receipts | `_bound_for_model()`: 8k cap, head 4k + tail 4k, full spill to `.jobs/` |

## Tools

| | Hermes (`tools/registry.py` + `model_tools.py` + `toolsets.py`) | Here (`tools.py`, static `TOOLS_SPECS`) |
|---|---|---|
| Count | 70+ across ~28 toolsets, AST auto-discovery, no manual list | 17 hand-listed: bash/edit/write/read/grep/glob/lsp/apply_patch/skill/todowrite/webfetch/websearch/question/subagent/models/mcp_* |
| Gating | Per-tool `check_fn` (key? binary? service?) + enabled/disabled toolsets + platform presets | Always on except `question` stripped in web mode |
| Safety | `DANGEROUS_PATTERNS` (rm -rf, mkfs, DROP, curl\|sh…) + interactive approve / gateway callback / allowlist + smart-LLM approve | None — bash runs raw via `subprocess` |
| Terminal | 7 backends: local/docker/ssh/singularity/modal/daytona/vercel + PTY + process registry + receipts | 1 backend: local `subprocess.run` + `background=true` → `.jobs/job-*.log` |
| Web | 4 backends + browser CDP supervisor + doc extraction | `webfetch` (regex strip) + `websearch` (Exa → DDG fallback, honest fail) |
| MCP | Dynamic `mcp_tool_discovery` from server config | 2 stubs returning "no MCP servers configured" |
| LSP | Real diagnostics wiring | Grep-based fallback string |
| Dispatch | `handle_function_call` → agent-loop intercept (todo/memory/session_search/delegate) → pre-hook → `registry.dispatch` → post-hook, double error-wrap | `execute_tool()` if/else chain, per-call try/except (one bad call never kills siblings) |
| Async | `_run_async()` bridges CLI loop / gateway loop / worker threads | All sync |

## Memory / skills (the big gap)

| | Hermes (closed learning loop) | Here (none) |
|---|---|---|
| Stores | `MEMORY.md` 2200 chars + `USER.md` 1375 chars, frozen snapshot in prompt, `§`-delimited | No memory files; only `.todos.json` via `todowrite` |
| Writes | Agent `memory` tool (add/replace/remove, substring match, dup reject, injection scan); `write_approval` gate + `/memory pending` | N/A |
| Background review | Post-turn fork learns lessons → memory/skill writes (`auxiliary.background_review`, cheaper-model + digest + defer-on-local-GPU + token cap) | N/A |
| Recall | `session_search`: FTS5 over all sessions (~20ms), no LLM; Honcho/Hindsight/Mem0 providers optional | N/A — history is last-60 in request body |
| Skills | Auto-create after complex tasks, self-patch in use, `/skills`, Skills Hub, `agentskills.io` compat, `/journey` timeline | `skill(name/path/id)` just reads a `SKILL.md` file (≤20k chars) |
| Context files | `SOUL.md` / `AGENTS.md` / project context injected per turn | Only `system_prompt.md` |

## Session storage

| | Hermes (`hermes_state*.py`, `state.db` v31) | Here (`.sessions/`) |
|---|---|---|
| Engine | SQLite WAL, `sessions` + `messages` + `session_model_usage` + `state_meta` + gateway/delivery/compression-lock tables | SQLite `runs(run_id,reply,tools,done,status,ts)` + per-session `.jsonl` turns |
| Search | 3× FTS5 (`messages_fts` + trigram + CJK) with triggers, sanitizer, snippet `>>>match<<<`, lineage queries | No search |
| Meta | Tokens/cost per model/task, titles (unique), source (`cli`/`telegram`/…), `user_id`, cwd/branch, rewind/archived/pinned, `message_uid` + tool-call uids | `message_count` only via jsonl length; no tokens/cost |
| Contention | 1s SQLite timeout + 20s/60s/0.5s budgeted retry + `BEGIN IMMEDIATE` + WAL checkpoint/50 + lock-owner logging | `DB_LOCK` threading lock (single process only) |
| Recovery | Migration chain v1→v31, `_reconcile_columns`, stale-running → interrupted, `hermes sessions recover` | `_store_init()`: stale `running` → `interrupted`, prune >24h |
| Isolation | Per-profile `HERMES_HOME` (own db/config/memories), test live-guard | Single dir; git-ignored `.sessions/.jobs/.todos.json` |

## Serving / platforms

| | Hermes | Here |
|---|---|---|
| Entries | CLI + gateway + ACP (VS Code/Zed/JetBrains) + batch runner + API server + Python lib | `server.py` (`ThreadingHTTPServer` 127.0.0.1:8000) + `agent.py` CLI |
| Chat surfaces | TUI (multiline, autocomplete, streaming, reasoning view) + Telegram/Discord/Slack/WhatsApp/Signal/Email + web dashboard + cron delivery | Single-file `index.html`, EventSource live tool log, Esc = interrupt |
| Cron | First-class agent jobs (`cron/jobs.py`), natural-language schedule, skill/script attach, any-platform delivery | None (suggested: auto-continue while todos open) |
| Plugins | `~/.hermes` / project / pip entry-points; tools + hooks + CLI cmds; memory + context-engine single-select | None |
| Training | Trajectory gen (ShareGPT) + compression for next-gen tool models | None |

## What to borrow (cheapest first)

1. `DANGEROUS_PATTERNS` + approve before `bash` — biggest safety win, ~30 lines.
2. `check_fn` gating (hide `websearch` without key instead of honest-fail text).
3. `MEMORY.md`/`USER.md` + `memory` tool — Hermes' cheapest superpower.
4. `session_search` FTS5 over `.sessions/*.jsonl` — recall without rereading everything.
5. Fallback model list (not just retry-same-model).
6. Cron = your "auto-continue while todos open" idea, generalized.

---

# Can you copy Hermes patterns to your own laptop agent? Yes.

Short answer: **yes, and laptop is the easiest target.** Hermes already runs on laptop by default (`local` backend). The 7 backends are interfaces — you only need 1 to start. License is MIT, copying patterns is allowed (keep attribution).

## What "runs anywhere" really means

| Backend | Needs | On your laptop? |
|---|---|---|
| `local` | just your Mac | yes, day 1 |
| `docker` | Docker Desktop | yes, `docker ps` works |
| `ssh` | any remote box / $5 VPS | yes, later |
| `singularity` | HPC clusters | skip |
| `modal` / `daytona` / `vercel` | cloud accounts, hibernate-when-idle | skip — stub the interface, add when needed |

Daytona/Modal "hibernate" = serverless persistence. You don't need it on a laptop — your disk IS persistence.

## Copy this, skip that (laptop build)

| Hermes pattern | Do on laptop | Skip for now |
|---|---|---|
| `AIAgent` loop: prompt → call → tools → repeat, 500 budget | yes — raise yours 12 → 100, add thread-interrupt later | fallback-provider chain, 3 API modes (keep 1: `chat_completions`) |
| `tools/registry.py`: self-register + `check_fn` + toolsets | yes — biggest structural win, ~100 lines | 70 tools; start with your 17 |
| `DANGEROUS_PATTERNS` approval | yes — ~30 lines, highest ROI | smart-LLM auto-approve |
| Prompt tiers (stable/context/volatile) + frozen snapshot | yes — split `system_prompt.md` into 3 parts | Anthropic cache breakpoints |
| Compaction at 50% + protect-last-20 + lineage | yes — yours already does this; add % trigger | micro-compaction, context-engine plugins |
| `state.db` sessions+messages+FTS5 | yes — 1 table + 1 FTS table is enough | trigram/CJK, billing, 31 migrations |
| `MEMORY.md` + `USER.md` + `session_search` | yes — the whole learning loop in 2 files + 1 tool | Honcho/Mem0 providers, background review, `/journey` |
| Gateway (Telegram/Discord/…) + cron + ACP | no — laptop CLI + `:8000` UI is enough | add Telegram later via 1 adapter if wanted |

## How — 6 steps in order

1. **Registry** (~1 evening): `TOOLS_SPECS` list → `register(name, toolset, schema, handler, check_fn)`. Auto-import `tools/*.py`. Nothing else changes, but adding tool #18 becomes 1 file.
2. **Environments interface** (~1 evening): `run(cmd, cwd)` → `local_run()` today; `docker_run()` / `ssh_run()` same signature tomorrow. Your `bash` tool calls the interface, not `subprocess` directly.
3. **Approval** (~1 hour): copy `DANGEROUS_PATTERNS` regexes, check before `bash`, ask on CLI / block on web. Done.
4. **Prompt builder** (~1 evening): `system_prompt.md` → `SOUL.md` (who) + `AGENTS.md` (how) + `MEMORY.md`/`USER.md` (remembered). Assemble in that order. Freeze per session.
5. **Storage upgrade** (~1 evening): `.sessions/*.jsonl` → SQLite `messages(session_id, role, content, tool_calls, ts)` + `messages_fts`. Gives you `session_search` free.
6. **Memory loop** (~1 evening): `memory` tool (add/replace/remove) + inject into prompt + `/new` at task boundaries. Skip background review — you ARE the review on a laptop.

Total: ~1 week of evenings. You already have steps 0 (loop, retry, steer, SSE, SQLite claims) done.

## File map (where each Hermes idea lands in yours)

| Hermes file | Your file | Change |
|---|---|---|
| `agent/conversation_loop.py` | `server.py:run_agent()` | raise budget, add parallel tools later |
| `tools/registry.py` | `tools.py:execute_tool()` | replace if/else with dict + `register()` |
| `tools/approval.py` | `tools.py:bash()` | check-then-run wrapper |
| `agent/prompt_builder.py` | `system_prompt.md` | split into 3 files, concat at chat start |
| `hermes_state.py` | `.sessions/store.db` | add `messages` + FTS table next to `runs` |
| `agent/memory_manager.py` | new `memories/` dir | 2 markdown files + 1 tool |
| `tools/environments/` | `tools.py:bash()` | wrap in `backends/local.py` interface |

## Laptop limits (honest)

- No GPU-cluster training, no 24/7 Telegram bot unless laptop stays awake — fine for coding agent.
- Big contexts (200k+) are slower on laptop RAM — your 60-msg / 80k-char compaction is actually well-tuned for this.
- Docker backend needs Docker Desktop running (~2GB RAM). Local backend needs nothing.

That's it. Start with registry + approval.
