"""THE single source of the tier-2 (human-only) path list, plus the read denylist and the
Bash heuristics the PreToolUse hook uses.

Imported by the hook, the SDK in-process hooks, ``runs/apply_changes.py`` (the hard
guarantee: refuse to merge a commit touching tier 2) and the tests.

Stdlib only — this runs inside hook subprocesses, with no repo on ``sys.path``.
"""

from __future__ import annotations

import fnmatch
import re
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
    "console/**",                     # the console is code, and it is the human's side
    "var/**",                         # signed mode state, bless digest, runtime overlays
    "knowledge/flags.json",           # written ONLY through ops.lib.flags
    "knowledge/state/**",             # computed market state; code writes it, not a model
    ".claude/settings.json",
    ".claude/hooks/**",
    # Every skill's deterministic scripts are tier 2 (skills.policy.*.scripts: human).
    # Their SKILL.md bodies and tests stay tier 1 and self-improvable through the gate,
    # except ops-runbook, which is human-only end to end.
    ".claude/skills/*/scripts/**",
    ".claude/skills/market-state/scripts/**",
    ".claude/skills/tca/tests/**",
    ".claude/skills/risk-gate/tests/**",
    ".claude/skills/ops-runbook/**",
    # The tier-1 overlays are written ONLY by the change gate (runs/apply_changes.py),
    # never by a session's own hand.
    "config/skills-registry.auto.yaml",
    "config/prompts-auto.yaml",
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
    "config/models-auto.yaml",       # auto-shadow / auto-promotion overlay
]

#: Tier-0 data the session may write even though it lives outside its worktree — these are
#: symlinked to the live state root and are free by design (spec §7).
TIER0_WRITABLE = [
    "knowledge/**",
    "reports/**",
    "changes/**",
    "proposals/**",
    "lessons.md",
    "lessons-archive.md",
]

#: Reading these is denied outright, in any tool, during an automated run.
READ_DENIED = [
    ".env",
    ".env.*",
    "var/state/**",
    "var/runtime/**",
    "**/.config/earn/**",
    "**/.claude/.credentials.json",
]

WRITE_TOKENS = (">", ">>", "tee ", "mv ", "cp ", "sed -i", "rm ", "truncate",
                "git checkout --", "git restore", "chmod", "chown", "ln ", "install -",
                "dd ", "python3 -c", "touch ")

#: Anything that could reach the local console or the network from a Bash command.
NETWORK_TOKENS = ("curl", "wget", "nc ", "netcat", "ssh ", "scp ", "rsync ")
CONSOLE_PATTERNS = (re.compile(r"127\.0\.0\.1"), re.compile(r"\blocalhost\b"),
                    re.compile(r"\b0\.0\.0\.0\b"))


def _norm(rel_path: str) -> str:
    """Canonicalise a repo-relative path: strip ``./``, collapse ``..``, keep it POSIX."""
    rel = str(rel_path).replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    parts: list[str] = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            else:
                parts.append("..")
            continue
        parts.append(seg)
    return str(PurePosixPath(*parts)) if parts else ""


def _seg_match(rp: list[str], i: int, pp: list[str], j: int) -> bool:
    while j < len(pp):
        seg = pp[j]
        if seg == "**":
            if j == len(pp) - 1:
                return True
            return any(_seg_match(rp, k, pp, j + 1) for k in range(i, len(rp) + 1))
        if i >= len(rp) or not fnmatch.fnmatchcase(rp[i], seg):
            return False
        i += 1
        j += 1
    return i == len(rp)


def _match(rel: str, pattern: str) -> bool:
    """Segment-wise glob: ``*`` stops at ``/``, ``**`` spans any number of segments."""
    rp = [s for s in rel.split("/") if s]
    pp = [s for s in pattern.split("/") if s]
    return _seg_match(rp, 0, pp, 0)


def matches_any(rel_path: str, patterns: list[str]) -> bool:
    rel = _norm(rel_path)
    return any(_match(rel, p) for p in patterns)


def is_tier2(rel_path: str) -> bool:
    rel = _norm(rel_path)
    if rel.startswith("../"):
        return True          # anything above the repo is off limits by construction
    for exc in TIER1_EXCEPTIONS:
        if rel == exc:
            return False
    return any(_match(rel, p) for p in TIER2_PATTERNS)


def is_tier0_writable(rel_path: str) -> bool:
    """True for the shared data paths a session may write outside its own worktree."""
    rel = _norm(rel_path)
    if is_tier2(rel):
        return False
    return any(_match(rel, p) for p in TIER0_WRITABLE)


def is_read_denied(path: str) -> bool:
    """``.env``, the signed state files and the credential stores are never readable."""
    raw = str(path).replace("\\", "/")
    rel = _norm(raw)
    for pattern in READ_DENIED:
        if _match(rel, pattern) or _match(raw.lstrip("/"), pattern):
            return True
    name = rel.rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.")


def bash_touches_tier2(command: str) -> str | None:
    """Conservative string check: a tier-2-looking path combined with a write-shaped
    token. Returns the offending fragment or None."""
    if not any(tok in command for tok in WRITE_TOKENS):
        return None
    for word in _words(command):
        if ("/" in word or word.startswith(".env")
                or word in (".gitignore", "pyproject.toml", "CLAUDE.md",
                            ".pre-commit-config.yaml")):
            if is_tier2(word):
                return word
    return None


def bash_reads_denied(command: str) -> str | None:
    """A read of a denied path by any means (``cat``, ``grep``, ``source``, ``<``)."""
    for word in _words(command):
        if is_read_denied(word) and ("/" in word or word.startswith(".env")):
            return word
    return None


def bash_reaches_network(command: str) -> str | None:
    """``curl``/``wget``/the console origin — an automated run never calls the console."""
    lowered = command.lower()
    for tok in NETWORK_TOKENS:
        if re.search(rf"(^|[\s;&|(]){re.escape(tok.strip())}\b", lowered):
            return tok.strip()
    for pat in CONSOLE_PATTERNS:
        if pat.search(lowered):
            return pat.pattern
    return None


def _words(command: str) -> list[str]:
    cleaned = command
    for ch in ";|&\n\t":
        cleaned = cleaned.replace(ch, " ")
    return [w.strip("'\"()<>$`,") for w in cleaned.split() if w.strip("'\"()<>$`,")]
