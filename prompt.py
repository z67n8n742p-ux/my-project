"""Prompt assembly (Hermes prompt_builder pattern, minimal port).

Tiers: stable (SOUL + AGENTS) -> context (legacy system_prompt.md / UI text)
       -> volatile (MEMORY + USER snapshot, re-read every turn).

Contract:
  - Never raises: missing files yield empty sections.
  - Never breaks the UI: /api/system still edits system_prompt.md only.
  - Memory snapshot shows usage % so the model self-limits.
  - Snapshot is live (re-read per /api/chat), not frozen: memory edits
    take effect on the next turn without a restart.
"""
from pathlib import Path

ROOT = Path(__file__).parent
MEMORY_FILE = ROOT / "memories" / "MEMORY.md"
USER_FILE = ROOT / "memories" / "USER.md"
SOUL_FILE = ROOT / "SOUL.md"
AGENTS_FILE = ROOT / "AGENTS.md"

MEMORY_LIMIT = 2200
USER_LIMIT = 1375


def _read_entries(path: Path):
    try:
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
        return out
    except Exception:
        return []


def _read_text(path: Path) -> str:
    try:
        return path.read_text() if path.exists() else ""
    except Exception:
        return ""


def memory_snapshot() -> str:
    mem = _read_entries(MEMORY_FILE)
    user = _read_entries(USER_FILE)
    mem_chars = sum(len(e) + 1 for e in mem)
    user_chars = sum(len(e) + 1 for e in user)
    parts = []
    if mem:
        pct = min(100, int(mem_chars / MEMORY_LIMIT * 100)) if MEMORY_LIMIT else 0
        parts.append(
            f"MEMORY (personal notes) [{pct}% — {mem_chars}/{MEMORY_LIMIT} chars]\n"
            + "\n".join(f"- {e}" for e in mem)
        )
    if user:
        pct = min(100, int(user_chars / USER_LIMIT * 100)) if USER_LIMIT else 0
        parts.append(
            f"USER PROFILE [{pct}% — {user_chars}/{USER_LIMIT} chars]\n"
            + "\n".join(f"- {e}" for e in user)
        )
    return "\n\n".join(parts)


def build_system_prompt(base_system: str = "") -> str:
    """Assemble final system prompt. base_system = legacy system_prompt.md / UI text."""
    soul = _read_text(SOUL_FILE).strip()
    agents = _read_text(AGENTS_FILE).strip()
    mem = memory_snapshot()
    sections = []
    if soul:
        sections.append(soul)
    if agents:
        sections.append(agents)
    if (base_system or "").strip():
        sections.append((base_system or "").strip())
    if mem:
        sections.append("═══ REMEMBERED (frozen snapshot — persists across sessions) ═══\n" + mem)
    return "\n\n".join(s for s in sections if s)
