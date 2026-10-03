"""Dangerous-command approval (Hermes DANGEROUS_PATTERNS pattern, minimal port).

Modes via FORMY_APPROVAL:
  ask  – CLI: prompt on TTY, deny when non-interactive (safe default).
  deny – web/server: never prompt, block with clear error (default for server).
  allow – bypass (explicit opt-in, e.g. sandbox).

Behavior contract (never breaks normal commands):
  - Safe commands pass through untouched, zero latency.
  - Dangerous commands in `deny`/non-TTY `ask` return an error string
    (tool isolation turns it into a normal tool result, not a crash).
  - Only a TTY CLI in `ask` mode prompts the user.
"""
import os
import re
import sys

# (regex, human-readable reason). Keep tight: rm -rf on /tmp or node_modules
# still matches – user can allow explicitly. False positive > wiped disk.
DANGEROUS_PATTERNS = [
    (r"\brm\s+.*-[a-z]*r[a-z]*f", "recursive delete (rm -rf)"),
    (r"\b(mkfs|dd\s+[^|]*of=|:\(\)\s*\{\s*:\|\:&\s*\}\s*;)", "disk wipe / fork bomb"),
    (r"\bDROP\s+TABLE\b", "destructive SQL (DROP TABLE)"),
    (r"\bDELETE\s+FROM\b(?![\s\S]*\bWHERE\b)", "destructive SQL (DELETE without WHERE)"),
    (r">\s*/etc/", "system config overwrite (> /etc/)"),
    (r"\b(systemctl\s+(stop|disable|mask)|service\s+\S+\s+stop)\b", "service manipulation"),
    (r"\b(chmod\s+-R\s+777\s+/|chown\s+-R\s+\S+\s+/)\b", "recursive permission change on /"),
    (r"(curl|wget)\s+[^|]*\|\s*(sh|bash)", "remote code execution (curl|sh)"),
    (r"\b(pkill\s+-9\s+-1|kill\s+-9\s+-1|shutdown|reboot|halt)\b", "process kill / shutdown"),
]


def detect(command: str):
    """Return reason string if dangerous, else None."""
    cmd = command or ""
    for rx, reason in DANGEROUS_PATTERNS:
        try:
            if re.search(rx, cmd, re.IGNORECASE):
                return reason
        except Exception:
            continue
    return None


def _mode() -> str:
    return (os.getenv("FORMY_APPROVAL", "") or "").strip().lower()


def check(command: str, default_ask: bool = False) -> tuple:
    """Decide whether `command` may run.

    Returns (allowed: bool, message: str). message is "" when allowed.
    Never raises, never prompts unless TTY + ask mode.
    """
    reason = detect(command)
    if not reason:
        return True, ""
    mode = _mode() or ("ask" if default_ask else "deny")
    if mode == "allow":
        return True, ""
    if mode == "ask" and sys.stdin.isatty():
        try:
            ans = input(f"[approval] dangerous command ({reason}):\n  $ {command}\nAllow once? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            return True, ""
        return False, f"approval denied ({reason}): command blocked by user"
    return False, (
        f"approval denied ({reason}): blocked by policy. "
        f"Run in CLI to approve interactively, or set FORMY_APPROVAL=allow in a sandbox."
    )
