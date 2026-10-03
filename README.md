# formyproject agent

Localhost coding agent: Python backend + single-file web frontend, backed by MaxPlus AI
(OpenAI-compatible). Opencode-v2 long-task loop (inbox idempotency, steer, retry,
compaction, SSE, SQLite) with Hermes-pattern upgrades (registry, backends, approval,
memory, fallback, parallel tools, skills, cron, skins).

## Run

```
python3 -m pip install -r requirements.txt
export MAXPLUS_API_KEY="ccsk-..."
NO_BROWSER=1 PORT=8000 python3 server.py   # open http://localhost:8000
```

| Action | Command |
|---|---|
| Stop server | `pkill -f server.py` |
| Restart (needed after `server.py`/`tools.py` edits) | `NO_BROWSER=1 PORT=8000 nohup python3 server.py > server.restart.log 2>&1 &` |
| CLI | `python3 agent.py [--model X]` |
| UI edits | No restart — hard-refresh (`Cmd+Shift+R`, Safari: `Option+Cmd+R`) |

## Layout

| File | What |
|---|---|
| `server.py` | Backend (`127.0.0.1:8000`). Chat, steer, interrupt, SSE, sessions, cron tick, token auth |
| `tools.py` | 23 tools (files, shell, search, web, memory, cron, process, extract, MCP…) |
| `agent.py` | CLI with same retry/compaction/fallback (non-streaming) |
| `index.html` | Whole frontend, zero deps |
| `backends.py` | `bash` routing: local/docker/ssh, resolved per call |
| `approval.py` | Dangerous-command gate (deny on web, prompt on CLI TTY) |
| `prompt.py` | System prompt builder (`SOUL.md` + `AGENTS.md` + memory, re-read live) |
| `cron.py` | Scheduled jobs (`every Ns\|m\|h`, `daily@HH:MM`), transcripts in `.cron/runs/` |
| `skills/` | Agent-writable reusable procedures |
| `memories/` | `MEMORY.md` + `USER.md`, seeded, tracked in git |
| `todo-app/` | Sample app + tests (`python3 -m unittest`) |

## Config (all optional, see `.env.example`)

| Var | Effect |
|---|---|
| `MAXPLUS_API_KEY` | Required for chat (`ccsk-...`) |
| `FORMY_TOKEN` | Off by default; when set, every `/api/*` needs it (header or `?token=`) |
| `FORMY_HOST` | Loopback default; non-loopback bind without `FORMY_TOKEN` refuses to start |
| `FORMY_FALLBACK_MODELS` | Comma list tried after the primary model fails |
| `FORMY_BACKEND` | `local`\|`docker`\|`ssh` for `bash` (+ container/target vars) |
| `FORMY_APPROVAL` | `allow` bypasses the dangerous-command gate (sandbox only) |
| `FORMY_FILE_SCOPE` | `allow` bypasses the sensitive-path write guard (sandbox only) |
| `MCP_TIMEOUT` | Seconds per MCP stdio call (per-server `timeout` in `.mcp.json` wins) |

MCP servers: copy `.mcp.example.json` → `.mcp.json` (git-ignored), no restart needed.

## Gotchas

- macOS zsh has no `pip` → use `python3 -m pip`. Python 3.9, `openai>=1.0.0` unpinned.
- No `EXA_API_KEY` → websearch falls back to DuckDuckGo (often bot-blocked, fails honestly).
- JS has no test runner here — parse-check with system `jsc` (see `handoff.md`).
- Cron runs only while the server is up. Min interval `every 60s`, max 20 jobs.
- `question` tool is CLI-only (web excludes it); `subagent` is capped at 6 steps, no recursion.
