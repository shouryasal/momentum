#!/usr/bin/env python3
"""PreToolUse hook: the tripwire around an AUTOMATED Claude run (spec §10).

Active only when ``EARN_AUTOMATED_RUN=1`` (set by ``ops/envwrap.sh`` and by
``runs.worktree.Worktree.env()``); an interactive human session is untouched. The hard
guarantee elsewhere is ``runs/apply_changes.py`` refusing to merge a commit that touches
tier 2 — this hook stops the write from happening in the first place.

What it denies

* a **write outside the session's worktree** (``$EARN_WORKTREE``), except the allow-listed
  tier-0 data paths under ``$EARN_STATE_ROOT`` (``knowledge/``, ``reports/``, ``changes/``,
  ``proposals/``, ``lessons*.md``);
* a write to any **tier-2** path, after canonicalising ``.``/``..``/symlinks — so
  ``a/../config/earn.yaml`` and a symlink into ``config/`` are caught too;
* a **read** (``Read``/``Grep``/``Glob``, or a Bash ``cat``/``grep``/``source``) of ``.env*``,
  ``var/state/**``, ``~/.config/earn/**`` or the Claude credential store;
* a **Bash write** to tier 2 (redirects, ``mv``, ``sed -i``, ``rm`` …), any Bash command
  that merely *names* a tier-2 path without being one of the read-only commands allowed
  to, every inline interpreter or heredoc (``python -c``, ``sh -c``, ``perl``, ``pytest``,
  ``<<EOF``) and every writer whose destinations live inside its payload (``tar -x``,
  ``unzip``, ``git apply``, ``patch``, ``xargs``, ``find -exec``) — those run code, or
  write paths, that no path check can see;
* any Bash command that reaches the **console origin** or the network (``curl``, ``wget``,
  ``127.0.0.1``, ``localhost``): an automated run must never drive the human's console.

Every denial is appended to ``logs/hook-denials.jsonl`` under the state root, which the
Invariants page reads.

stdin: the hook JSON (hook_event_name, tool_name, tool_input, cwd).
Deny: print permissionDecision JSON, exit 0. Allow: exit 0 silently.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tier2_paths import (  # noqa: E402
    bash_denial,
    bash_reaches_network,
    bash_reads_denied,
    is_read_denied,
    is_tier0_writable,
    is_tier2,
)

READ_TOOLS = ("Read", "Grep", "Glob", "NotebookRead")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _log_denial(reason: str, tool: str, detail: dict) -> None:
    """Best effort — a failure to log must never turn a deny into an allow."""
    try:
        root = Path(os.environ.get("EARN_STATE_ROOT") or os.getcwd())
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "hook-denials.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tool": tool, "reason": reason,
                "worktree": os.environ.get("EARN_WORKTREE"),
                "detail": detail}) + "\n")
    except OSError:
        pass


def deny(reason: str, *, tool: str = "", detail: dict | None = None) -> None:
    _log_denial(reason, tool, detail or {})
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _resolve(raw: str, cwd: Path) -> Path:
    p = Path(raw).expanduser()
    p = p if p.is_absolute() else cwd / p
    try:
        return p.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return p


def _relative_to(path: Path, base: Path) -> str | None:
    try:
        return str(path.relative_to(base.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return None


def check_write(raw: str, cwd: Path) -> str | None:
    """Returns a denial reason, or None when the write is allowed."""
    if not raw:
        return None
    target = _resolve(raw, cwd)
    worktree = os.environ.get("EARN_WORKTREE")
    state_root = os.environ.get("EARN_STATE_ROOT")
    base = Path(worktree).resolve() if worktree else cwd.resolve()

    rel = _relative_to(target, base)
    if rel is not None:
        if is_tier2(rel):
            return (f"tier-2 path '{rel}' is human-only (spec §7); propose a"
                    " changes/*.json instead")
        return None

    # Outside the worktree: only the shared tier-0 data paths under the state root.
    if state_root:
        data_rel = _relative_to(target, Path(state_root))
        if data_rel is not None:
            if is_tier2(data_rel):
                return f"tier-2 path '{data_rel}' is human-only (spec §7)"
            if is_tier0_writable(data_rel):
                return None
            return (f"write to '{data_rel}' is outside the worktree and is not an"
                    " allow-listed tier-0 data path")
    return (f"write outside the session worktree: {raw}"
            f" (worktree: {worktree or base})")


def main() -> int:
    if os.environ.get("EARN_AUTOMATED_RUN") != "1":
        return 0
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    cwd = Path(data.get("cwd") or os.getcwd())

    if tool in WRITE_TOOLS:
        raw = (tool_input.get("file_path") or tool_input.get("notebook_path")
               or tool_input.get("path") or "")
        if is_read_denied(str(raw)):
            deny(f"'{raw}' holds secrets and is never writable", tool=tool,
                 detail={"path": str(raw)})
            return 0
        reason = check_write(str(raw), cwd)
        if reason:
            deny(reason, tool=tool, detail={"path": str(raw)})
    elif tool in READ_TOOLS:
        raw = (tool_input.get("file_path") or tool_input.get("path")
               or tool_input.get("pattern") or "")
        if raw and is_read_denied(str(raw)):
            deny(f"reading '{raw}' is denied: it holds secrets or signed state",
                 tool=tool, detail={"path": str(raw)})
    elif tool == "Bash":
        command = tool_input.get("command", "") or ""
        net = bash_reaches_network(command)
        if net:
            deny(f"bash command reaches '{net}': an automated run never calls the"
                 f" console or the network", tool=tool, detail={"match": net})
            return 0
        secret = bash_reads_denied(command)
        if secret:
            deny(f"bash command reads '{secret}', which holds secrets or signed state",
                 tool=tool, detail={"match": secret})
            return 0
        reason = bash_denial(command)
        if reason:
            deny(reason, tool=tool, detail={"command": command[:400]})
    elif tool in ("WebFetch", "WebSearch"):
        deny(f"{tool} is not available to an automated Earn run", tool=tool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
