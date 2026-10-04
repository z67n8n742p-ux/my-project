# MEMORY — one entry per line. Lines starting with # are comments.
formyproject: localhost coding agent — `server.py` (ThreadingHTTPServer 127.0.0.1:8000) + single-file `index.html` + `agent.py` CLI; backend MaxPlus OpenAI-compatible
Server restart needed for `server.py`/`tools.py` changes; frontend served from disk → hard-refresh (Cmd+Shift+R) after UI edits
SQLite `.sessions/store.db` (runs+messages+FTS); stale `running`→`interrupted` on boot; interrupt is cooperative (between steps); steer/interrupt 404 after restart → send as new chat
Compaction at 60 msgs/80k chars, keeps newest 20; tool output >8k spills full text to `.jobs/`, short preview to model
Approval: web deny-only, CLI prompts on TTY, `FORMY_APPROVAL=allow` sandbox-only; backends local default (docker needs container, ssh needs target)
websearch (Exa, confirmed working) + webfetch verified live
