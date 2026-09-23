"""THE single source of the tier-2 (human-only) path list, plus the read denylist and the
Bash heuristics the PreToolUse hook uses.

Imported by the hook, the SDK in-process hooks, ``runs/apply_changes.py`` (the hard
guarantee: refuse to merge a commit touching tier 2) and the tests.

Stdlib only — this runs inside hook subprocesses, with no repo on ``sys.path``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath

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
                "git checkout --", "git restore", "git apply", "git clean", "chmod",
                "chown", "ln ", "install -", "dd ", "python -c", "python3 -c", "perl",
                "awk -i", "gawk -i", "patch ", "tar ", "unzip ", "xargs", "touch ")

#: Anything that could reach the local console or the network from a Bash command.
NETWORK_TOKENS = ("curl", "wget", "nc ", "netcat", "ssh ", "scp ", "rsync ")
CONSOLE_PATTERNS = (re.compile(r"127\.0\.0\.1"), re.compile(r"\blocalhost\b"),
                    re.compile(r"\b0\.0\.0\.0\b"))

#: Ways of running code this hook cannot read. An automated run never needs one: every
#: script it may execute is a named, human-owned (tier-2) file, so an interpreter reading
#: its payload from the command line, from stdin or from a heredoc is by definition code
#: the model wrote itself. ``pytest`` is on the list for the same reason — a skill's
#: ``tests/**`` is tier 1, i.e. model-writable, and pytest imports it.
INTERPRETER_PATTERNS = (
    re.compile(r"(?:^|[\s;&|(])(?:python|py)[0-9.]*\s+(?:-[BEISOuvx]+\s+)*"
               r"(?:-[A-Za-z]*c|-)(?=[\s'\"])"),
    re.compile(r"(?:^|[\s;&|(])(?:sh|bash|zsh|dash|ksh)\s+-[A-Za-z]*c(?=[\s'\"])"),
    re.compile(r"(?:^|[\s;&|(])(?:perl|ruby|php|lua|osascript)\b"),
    re.compile(r"(?:^|[\s;&|(])node\s+-[A-Za-z]*e(?=[\s'\"])"),
    re.compile(r"(?:^|[\s;&|(])(?:pytest|py\.test|tox|nox)\b"),
    re.compile(r"(?:^|[\s;&|(])(?:python|py)[0-9.]*\s+-m\s+(?:pytest|py_compile|code)\b"),
    re.compile(r"(?:^|[\s;&|(])(?:eval|exec|source)\s"),
    re.compile(r"(?:^|[\s;&|(])\.\s+\S"),
)

#: ``cmd <<EOF`` / ``cmd <<-'EOF'`` — the body never reaches any word-level check.
HEREDOC_PATTERN = re.compile(r"<<-?\s*[\"']?[A-Za-z_]")

#: Writers whose *destinations live inside their payload*, not on the command line. An
#: archive member list and a diff's ``+++`` lines are as invisible to a path check as a
#: ``python -c`` body: ``tar -xf payload.tar -C .`` can drop ``config/earn.yaml`` into the
#: worktree while naming no tier-2 path at all, and ``git apply`` / ``patch`` do the same
#: with a diff. ``xargs`` and ``find -exec`` build their own argv. None of these appears in
#: any automated allowlist, so refusing them outright costs a session nothing.
OPAQUE_WRITE_PATTERNS = (
    re.compile(r"(?:^|[\s;&|(])(?:tar|unzip|7z|unar|bsdtar|cpio)\b"),
    re.compile(r"(?:^|[\s;&|(])git\s+(?:apply|am)\b"),
    re.compile(r"(?:^|[\s;&|(])patch\b"),
    re.compile(r"(?:^|[\s;&|(])xargs\b"),
    re.compile(r"-exec\s"),
    re.compile(r"(?:^|[\s;&|(])(?:base64|xxd|uudecode)\s+(?:-[A-Za-z]*\s+)*-{1,2}d"),
)

#: The whole vocabulary a command may use and still be allowed to *name* a tier-2 path.
#: Everything here reads, stages or runs a human-owned file; nothing here writes one.
READ_ONLY_BASH = (
    re.compile(r"^git\s+(?:status|log|diff|show|rev-parse|cat-file|ls-files|ls-tree|"
               r"blame|add|commit|branch|tag|describe|shortlog|config\s+--get)\b"),
    re.compile(r"^(?:cat|head|tail|wc|ls|file|stat|grep|rg|find|diff|sort|uniq|cut|tr|"
               r"column|jq|yq|md5sum|sha256sum|echo|printf|true|test|basename|dirname|"
               r"realpath|readlink|nl|od|xxd)\b"),
    re.compile(r"^(?:python|py)[0-9.]*\s+\S*\.claude/skills/[^/\s]+/scripts/\S+\.py\b"),
    re.compile(r"^(?:python|py)[0-9.]*\s+-m\s+evals\.[A-Za-z_]+\b"),
)

#: Absolute locations outside every known root that are still ordinary scratch space.
ABSOLUTE_EXEMPT = ("/tmp/", "/var/tmp/", "/dev/null", "/dev/stdout", "/dev/stderr",
                   "/dev/fd/", "/proc/self/fd/")


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


def known_roots() -> list[str]:
    """Every directory an absolute path may legitimately be expressed against.

    ``$EARN_WORKTREE`` (the session's checkout), ``$EARN_STATE_ROOT`` / ``$EARN_LIVE_ROOT``
    (the live data and code roots) and, last, the checkout this file lives in.
    """
    out: list[str] = []
    for var in ("EARN_WORKTREE", "EARN_STATE_ROOT", "EARN_LIVE_ROOT"):
        value = (os.environ.get(var) or "").strip()
        if value:
            out.append(str(Path(value).expanduser()).replace("\\", "/").rstrip("/"))
    out.append(str(Path(__file__).resolve().parents[2]).replace("\\", "/").rstrip("/"))
    return [r for r in out if r]


def relativise(path: str) -> tuple[str, bool]:
    """``(path to match, landed outside every known root)``.

    ``is_tier2`` and ``is_read_denied`` used to look only at the raw string, so
    ``/home/me/earn/config/earn.yaml`` normalised to a path starting ``home/`` that no
    pattern matches — the whole tier-2 list was one absolute path away from silent. An
    absolute path that belongs to no known root cannot be classified at all, and an
    unclassifiable path is treated as tier 2, never as free.
    """
    raw = str(path).replace("\\", "/").strip()
    if not raw.startswith("/"):
        return raw, False
    for root in known_roots():
        if raw == root:
            return "", False
        if raw.startswith(root + "/"):
            return raw[len(root) + 1:], False
    return raw, True


def is_tier2(rel_path: str) -> bool:
    raw, outside = relativise(rel_path)
    if outside:
        # Scratch space is still scratch space; everything else above the roots is
        # unclassifiable, and unclassifiable means human-only.
        return not raw.startswith(ABSOLUTE_EXEMPT)
    rel = _norm(raw)
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
    """``.env``, the signed state files and the credential stores are never readable.

    An absolute path is relativised against the session's roots first, so
    ``$EARN_STATE_ROOT/var/state/mode.json`` is denied exactly like ``var/state/mode.json``.
    """
    raw = str(path).replace("\\", "/")
    scoped, _outside = relativise(raw)
    for candidate in {_norm(scoped), _norm(raw), raw.lstrip("/")}:
        for pattern in READ_DENIED:
            if _match(candidate, pattern):
                return True
    name = _norm(raw).rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.")


def _path_words(command: str) -> list[str]:
    return [w for w in _words(command)
            if ("/" in w or w.startswith(".env")
                or w in (".gitignore", "pyproject.toml", "CLAUDE.md",
                         ".pre-commit-config.yaml"))]


def bash_touches_tier2(command: str) -> str | None:
    """Conservative string check: a tier-2-looking path combined with a write-shaped
    token. Returns the offending fragment or None."""
    if not any(tok in command for tok in WRITE_TOKENS):
        return None
    for word in _path_words(command):
        if is_tier2(word):
            return word
    return None


def _segments(command: str) -> list[str]:
    """The command split on shell separators, so each verb can be judged on its own."""
    cleaned = re.sub(r"&&|\|\||[;|&\n]", "\x00", str(command or ""))
    return [s.strip() for s in cleaned.split("\x00") if s.strip()]


def bash_mentions_tier2(command: str) -> str | None:
    """A tier-2 path named by a command that is not plainly read-only.

    The old rule was "a write verb *and* a tier-2 path", which meant every verb the token
    list had never heard of — ``perl -pi``, ``gawk -i inplace``, ``tar -x``, a heredoc —
    was a silent allow. This is the inversion: naming a human-only path is the trigger,
    and only a short list of reading, staging and named-script commands is excused.
    """
    mentioned = next((w for w in _path_words(command) if is_tier2(w)), None)
    if mentioned is None:
        return None
    for segment in _segments(command):
        if not any(p.match(segment) for p in READ_ONLY_BASH):
            return mentioned
    return None


def bash_denial(command: str) -> str | None:
    """The reason an automated run may not run this Bash command, or ``None``.

    Order matters: the constructs that hide their payload are refused before any attempt
    to reason about the paths in them, because for those the paths prove nothing.
    """
    cmd = str(command or "")
    if HEREDOC_PATTERN.search(cmd):
        return ("a heredoc hides its payload from the tier-2 check and is never allowed"
                " in an automated run")
    for pattern in INTERPRETER_PATTERNS:
        hit = pattern.search(cmd)
        if hit:
            return (f"'{hit.group(0).strip()}' runs code this hook cannot read; an"
                    " automated run may only execute named tier-2 scripts")
    for pattern in OPAQUE_WRITE_PATTERNS:
        hit = pattern.search(cmd)
        if hit:
            return (f"'{hit.group(0).strip()}' writes paths that live inside its payload,"
                    " where no tier-2 check can see them")
    hit = bash_touches_tier2(cmd)
    if hit:
        return f"bash write touching tier-2 path '{hit}' is human-only (spec §7)"
    hit = bash_mentions_tier2(cmd)
    if hit:
        return (f"bash command names tier-2 path '{hit}' and is not one of the read-only"
                " commands allowed to (spec §7)")
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
