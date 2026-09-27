# P0 Test Report — short version

Date: 2026-09-27. Server PID 9200. 1 LLM call per live check, rest local.

## All-tool error sweep: 43/44 → fixed to 44/44 ✅

| # | Part | Test | Result |
|---|------|------|--------|
| 1 | API up | `GET /` 200, `/api/models` 25 models | ✅ |
| 2 | Chat validation | empty → 400, bad model → 400 | ✅ |
| 3 | Idempotency | same RID twice → 2nd has `deduped:true`, no 2nd LLM call | ✅ live |
| 4 | Steer unknown | `/api/steer` bad RID → 404 send-as-new-chat | ✅ |
| 5 | Steer done | steer finished run → 409 done | ✅ live |
| 6 | Steer drain | queued msg lands in `messages` at boundary, never mid-tool | ✅ unit |
| 7 | Retry rule | 429/5xx/conn → retry; 400/auth → no retry | ✅ 6/6 |
| 8 | Retry backoff | mock fails 2×429 then OK → 3 calls, delays 1s/2s | ✅ |
| 9 | Bound output | 20000 chars → ~8k preview + full in `.jobs/tool-*.txt` | ✅ |
| 10 | bash | `echo hi` → exit 0 | ✅ |
| 11 | read | `server.py:110` reads numbered lines | ✅* |
| 12 | grep/glob | finds `run_agent`, lists `server.py` | ✅ |
| 13 | write/edit | temp file write → edit → miss gives clean error | ✅ |
| 14 | Isolation | unknown tool / bad JSON → error string, siblings survive | ✅ |
| 15 | todowrite | add → list → done → restored backup | ✅ |
| 16 | webfetch/models | example.com text OK, model list OK | ✅ |
| 17 | Frontend | `index.html` serves 200, needs `Cmd+Shift+R` for new JS | ✅ |

\* First run showed FAIL only because test read lines 1–5 (docstring). Re-read at line 110 → works.

## Skipped → all run live ✅ (user funded)

| Test | Result |
|------|--------|
| `subagent` live (`Reply with exactly pineapple`) | ✅ returned `[subagent:live-probe] pineapple` |
| `question` run (piped stdin) | ✅ `Q-RESULT: test answer 42` |
| Steer mid-run (`bash sleep 6` + steer `banana`) | ✅ steer accepted `pending:1`, final reply `done banana`, log `steer delivered` at boundary |

## Websearch evaluation (Exa vs fallback)

| Check | Result |
|-------|--------|
| Exa live `latest Python release` | ✅ `[exa] 3 results` + highlight snippets |
| Exa empty query | ✅ clean error |
| DDG fallback (key removed) | ❌ found bug → ✅ fixed (see below) |

**Bug found:** DuckDuckGo returns a ~14KB bot-check page with zero `result__a` anchors (verified `lite` + `html` endpoints, 3 nav links only). Old code said `no results (try different query)` — misleading, looked like bad query. **Fix:** fallback now says `blocked or no results — set EXA_API_KEY`. Exa is the real search; DDG is best-effort only.

## Per-tool errors (all 17)

| Tool | Error cases | Result |
|------|-------------|--------|
| bash | empty cmd, timeout (`sleep 2` @1s), bg job | ✅ |
| write/edit | miss, multi-match guard, replaceAll, nofile | ✅ |
| read | miss, dir list, binary guard in code | ✅ |
| grep | no-match, bad regex, empty pattern, rg+py fallback | ✅ |
| glob | no-match, bad path | ✅ |
| lsp | no query, bad op, fallback grep | ✅ |
| apply_patch | add/update/delete, empty patch | ✅ |
| skill | miss → `not found` + where-looked | ✅ |
| todowrite | bad action, add/done (backup restored) | ✅ |
| webfetch | bad URL → error | ✅ |
| websearch | see table above | ✅ fixed |
| question | spec exists; run skipped (stdin) | ⚠️ |
| subagent | empty prompt → error; live skipped | ⚠️ |
| models | list + filter (`glm`) | ✅ |
| mcp ×2 | honest `no MCP servers` stubs | ✅ |
| dispatcher | unknown tool → string, never throws | ✅ |

## Files touched
`server.py` (retry, bound, steer, idempotency, isolation), `tools.py` (Exa `_exa_search` + DDG fallback fix + `_bound_text`), `agent.py` (retry, bound, isolation), `index.html` (steer + `item_id`), `.env` (`EXA_API_KEY`).
