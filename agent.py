"""
Own AI agent with own system prompt, using MaxPlus AI key.
Clones all 13 opencode tools via tools.py.

Setup:
  pip install -r requirements.txt
  export MAXPLUS_API_KEY="ccsk-... from https://maxplus-ai.cc/dashboard"
  python3 agent.py

  Optional:
    export MAXPLUS_BASE_URL="https://api.maxplus-ai.cc/chinese-specials/v1"  (default)
    export MAXPLUS_MODEL="glm-5.3-flash"  (default)

Docs: https://maxplus-ai.cc/docs/api
"""
import json
import os
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("missing dep: pip install -r requirements.txt")
    sys.exit(1)

from tools import TOOLS_SPECS, SUMMARY_SYSTEM, compact_messages, execute_tool

# auto-load .env (no dep needed)
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE_URL = os.getenv("MAXPLUS_BASE_URL", "https://api.maxplus-ai.cc/chinese-specials/v1")
API_KEY = os.getenv("MAXPLUS_API_KEY", os.getenv("OPENCODE_API_KEY", ""))
MODEL = os.getenv("MAXPLUS_MODEL", os.getenv("OPENCODE_MODEL", "glm-5.3-flash"))
SYSTEM_FILE = os.getenv("SYSTEM_FILE", "system_prompt.md")


def load_system_prompt() -> str:
    p = Path(SYSTEM_FILE)
    if p.exists():
        return p.read_text()
    return "You are a helpful coding agent with file and shell tools."


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        if status is not None and int(status) == 429:
            return True
        if status is not None and int(status) >= 500:
            return True
    except Exception:
        pass
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(k in name for k in ("ratelimit", "internalserver", "apiconnection", "apitimeout", "timeout", "connection")):
        return True
    if any(k in msg for k in ("rate limit", "429", "500", "502", "503", "504", "connection", "timeout", "overloaded")):
        if any(k in msg for k in ("401", "403", "404", "unknown model", "invalid api key")):
            return False
        return True
    return False


def _bound(text: str) -> str:
    try:
        from tools import _bound_text
        return _bound_text(text, "tool")
    except Exception:
        s = str(text or "")
        return s if len(s) <= 8000 else s[:4000] + f"\n...[truncated {len(s)} chars]...\n" + s[-4000:]


def _chat_no_tools(client, model, messages):
    """One LLM call with retry, no tools (for summarization)."""
    import random as _rnd
    import time as _tm
    last_err = None
    for attempt in range(5):
        try:
            return client.chat.completions.create(model=model, messages=messages)
        except Exception as e:
            last_err = e
            if attempt >= 4 or not _is_retryable(e):
                raise
            delay = (2 ** attempt) + _rnd.random() * 0.5
            print(f"[retry.scheduled attempt={attempt + 1}/4 in {delay:.1f}s] {str(e)[:200]}")
            _tm.sleep(delay)
    raise last_err


def _maybe_compact(client, messages):
    """Rolling compaction: summarize oldest, keep newest 20. No-op when under budget."""

    def _summarize(transcript):
        resp = _chat_no_tools(client, MODEL, [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": transcript},
        ])
        return (resp.choices[0].message.content or "").strip()

    new_msgs, info = compact_messages(messages, _summarize, keep_recent=20, max_msgs=60, max_chars=100000)
    if info.get("compacted") or info.get("error"):
        print(f"[compact {info['old_n']}->{info['new_n']}{' (' + info['error'] + ')' if info.get('error') else ''}]")
        messages[:] = new_msgs
    return info


def chat_loop():
    if not API_KEY:
        print("ERROR: set MAXPLUS_API_KEY first.")
        print('  export MAXPLUS_API_KEY="ccsk-..."  # from https://maxplus-ai.cc/dashboard')
        sys.exit(1)

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    system = load_system_prompt()
    print(f"connected: {BASE_URL} model={MODEL}")
    print(f"system: {SYSTEM_FILE} ({len(system)} chars)")
    print("tools (17): bash, edit, write, read, grep, glob, lsp, apply_patch, skill, todowrite, webfetch, websearch, question, subagent, models, mcp_list_resources, mcp_read_resource")
    print("type 'exit' to quit.\n")

    messages = [{"role": "system", "content": system}]

    while True:
        try:
            user = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if user.lower() in ("exit", "quit"):
            break
        if not user:
            continue
        messages.append({"role": "user", "content": user})

        # tool loop (max 20 steps per turn)
        for _ in range(20):
            _maybe_compact(client, messages)
            try:
                resp = None
                last_err = None
                import random as _rnd
                import time as _tm
                for attempt in range(5):
                    try:
                        resp = client.chat.completions.create(
                            model=MODEL,
                            messages=messages,
                            tools=TOOLS_SPECS,
                            tool_choice="auto",
                        )
                        break
                    except Exception as e:
                        last_err = e
                        if attempt >= 4 or not _is_retryable(e):
                            raise
                        delay = (2 ** attempt) + _rnd.random() * 0.5
                        print(f"[retry.scheduled attempt={attempt + 1}/4 in {delay:.1f}s] {str(e)[:200]}")
                        _tm.sleep(delay)
                if resp is None:
                    raise last_err
            except Exception as e:
                print(f"[api error] {e}")
                print("hint: check MAXPLUS_API_KEY and model name from /chinese-specials/v1/models")
                break

            msg = resp.choices[0].message
            # append assistant msg (with tool_calls if any)
            m = {"role": "assistant", "content": msg.content or ""}
            if getattr(msg, "tool_calls", None):
                m["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            messages.append(m)

            if not getattr(msg, "tool_calls", None):
                print(msg.content or "(empty)")
                break

            # execute tools (P0-5: per-call isolation — one bad call never kills siblings)
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception as e:
                    err = f"tool {name} error: invalid JSON arguments: {e}"
                    print(f"[tool:{name}] BAD_ARGS {e}")
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": err})
                    continue
                print(f"[tool:{name}] {json.dumps(args)[:300]}")
                try:
                    result = execute_tool(name, args)
                except Exception as e:
                    result = f"tool {name} error: {e}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _bound(str(result)),
                })
        else:
            print("[max tool steps reached]")


if __name__ == "__main__":
    # allow: python agent.py --model kimi-k2.5  or  --model=kimi-k2.5
    for a in sys.argv[1:]:
        if a.startswith("--model="):
            MODEL = a.split("=", 1)[1]
    if "--model" in sys.argv:
        i = sys.argv.index("--model")
        if i + 1 < len(sys.argv):
            MODEL = sys.argv[i + 1]
    chat_loop()
