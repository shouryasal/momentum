#!/usr/bin/env python3
"""PreToolUse hook: block writes to tier-2 (human-only) paths during AUTOMATED runs.

Active only when EARN_AUTOMATED_RUN=1 (set by ops/envwrap.sh for the research and
review runs); interactive human Claude Code sessions are unaffected. This is the
tripwire — the hard guarantee is runs/apply_changes.py refusing to merge a branch
that touches tier 2.

stdin: the hook JSON (hook_event_name, tool_name, tool_input, cwd).
Deny: print permissionDecision JSON, exit 0. Allow: exit 0 silently.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tier2_paths import bash_touches_tier2, is_tier2  # noqa: E402


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    if os.environ.get("EARN_AUTOMATED_RUN") != "1":
        return 0
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    cwd = Path(data.get("cwd") or os.getcwd())

    if tool in ("Write", "Edit", "NotebookEdit"):
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not raw:
            return 0
        p = Path(raw)
        try:
            rel = str((cwd / p if not p.is_absolute() else p).resolve().relative_to(
                cwd.resolve()))
        except ValueError:
            deny(f"write outside the repository: {raw}")
            return 0
        if is_tier2(rel):
            deny(f"tier-2 path '{rel}' is human-only (spec §7); propose a"
                 " changes/*.json instead")
    elif tool == "Bash":
        hit = bash_touches_tier2(tool_input.get("command", ""))
        if hit:
            deny(f"bash write touching tier-2 path '{hit}' is human-only (spec §7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
