"""THE single source of the tier-2 (human-only) path list. Imported by the
PreToolUse hook, the SDK in-process hooks, runs/apply_changes.py (the hard
guarantee: refuse to merge a branch touching tier 2) and the tests.

Stdlib only — this runs inside hook subprocesses.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

TIER2_PATTERNS = [
    "config/**",
    ".env*",
    "schemas/**",
    "strategies/**",
    "runs/**",
    "evals/**",
    "ops/**",
    "journal/**",
    "tests/**",
    "data/**",
    "ft_userdata/**",
    ".claude/settings.json",
    ".claude/hooks/**",
    ".claude/skills/tca/**",
    ".claude/skills/risk-gate/**",
    ".claude/skills/ops-runbook/**",
    ".gitignore",
    "pyproject.toml",
    ".pre-commit-config.yaml",
    "CLAUDE.md",
]

# Tier-1 carve-outs: gated-writable through changes/*.json + apply_changes even
# though their parents are tier 2.
TIER1_EXCEPTIONS = [
    "config/params-sleeve-a.json",
    "config/params-sleeve-b.json",
]

# Explicitly tier 0/1 (documentation aid; anything not tier-2 is writable by runs):
#   knowledge/**, lessons.md, lessons-archive.md, changes/**, reports/**, prompts/**,
#   proposals/**, .claude/skills/{decide,strategy-lab,market-state,crypto-brief,
#   reg-watch,post-mortem}/**

WRITE_TOKENS = (">", ">>", "tee ", "mv ", "cp ", "sed -i", "rm ", "truncate",
                "git checkout --", "git restore", "chmod", "ln ")


def _match(rel: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return rel == prefix or rel.startswith(prefix + "/")
    if "*" in pattern:
        return fnmatch.fnmatch(rel, pattern)
    return rel == pattern


def is_tier2(rel_path: str) -> bool:
    rel = str(PurePosixPath(rel_path))
    if rel.startswith("./"):
        rel = rel[2:]
    for exc in TIER1_EXCEPTIONS:
        if rel == exc:
            return False
    return any(_match(rel, p) for p in TIER2_PATTERNS)


def bash_touches_tier2(command: str) -> str | None:
    """Conservative string check: a tier-2-looking path combined with a write-shaped
    token. Returns the offending fragment or None."""
    if not any(tok in command for tok in WRITE_TOKENS):
        return None
    for word in command.replace(";", " ").replace("|", " ").split():
        w = word.strip("'\"()<>&")
        if ("/" in w or w.startswith(".env")
                or w in (".gitignore", "pyproject.toml", "CLAUDE.md",
                         ".pre-commit-config.yaml")):
            if is_tier2(w):
                return w
    return None
