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

That's it. Start with 1.
