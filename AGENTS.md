# AGENTS.md — how the agent works (project conventions layer)

Workflow:
- Inspect before answering: `read`, `glob`, `grep` before `edit`.
- Prefer small exact `edit` over rewriting whole files.
- Verify with `bash` (`python3 -m py_compile`, tests).
- Track multi-step work with `todowrite`.
- One bad tool call never kills siblings — keep going and report.
- Tool output over 8k chars spills to `.jobs/`; read it with `read`.
- Reusable workflows go to `skills/<name>/SKILL.md` via `skill_manage`
  (create once, patch to improve). Lessons, not logs: rules + why, no chat dumps.
