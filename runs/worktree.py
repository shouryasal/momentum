"""Isolated git worktrees for the automated review / daily-review sessions (spec §10).

The HIGH bug this fixes: the review jobs used to ``git checkout -B review/<week>`` and
``git reset --hard`` **inside the live checkout**, so a running system's working tree moved
underneath the bots, and ``apply_changes`` then cherry-picked a commit that was already on
HEAD (recorded "rejected" for a passing change).

From v2 an automated session gets its own worktree::

    git worktree add -B <kind>/<key> <git.worktree_root>/<kind>-<key> <git.live_branch>

The live checkout never changes branch, never resets and never moves. Two mechanisms make
the session see the *live* data while editing tier-1 files in its own checkout:

* ``EARN_STATE_ROOT`` points at the live root, so ``ops.lib.paths`` and every ``paths.*``
  entry resolve to the live databases, flags, proposals and reports;
* the untracked data entries are symlinked into the worktree, so a call site that builds a
  path from ``Path.cwd()`` instead of the state root still lands on the live file.

Symlinks are created only where git did not materialise something (git owns tracked paths),
and every one of them is appended to ``.git/info/exclude`` so ``git status`` in the worktree
stays clean — a dirty tree would make ``apply_changes`` refuse the merge.

Nothing in here talks to a model. ``git`` is invoked through an injectable runner so tests
drive a temp repository.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops.lib import paths

GitRunner = Callable[..., subprocess.CompletedProcess]

#: Data entries linked into a worktree when git left the path empty. Order is irrelevant.
DATA_LINKS: tuple[str, ...] = (
    "knowledge",
    "journal",
    "reports",
    "changes",
    "proposals",
    "data",
    "logs",
    "lessons.md",
    "lessons-archive.md",
)

#: How deep the linker walks into a tracked directory before giving up. ``knowledge/state``
#: is two levels down; nothing the sessions read is deeper than three.
MAX_LINK_DEPTH = 3

EXCLUDE_HEADER = "# earn: worktree data symlinks (runs/worktree.py)"


class WorktreeError(RuntimeError):
    """A git worktree operation failed."""


@dataclass(frozen=True)
class Worktree:
    """One automated session's checkout."""

    kind: str
    key: str
    branch: str
    path: Path
    live_root: Path
    links: tuple[str, ...] = field(default=())

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment an automated session runs with."""
        e = dict(os.environ if base is None else base)
        e["EARN_STATE_ROOT"] = str(self.live_root)
        e["EARN_LIVE_ROOT"] = str(self.live_root)
        e["EARN_WORKTREE"] = str(self.path)
        e["EARN_AUTOMATED_RUN"] = "1"
        return e


def _run(root: Path, *args: str, runner: GitRunner | None = None) -> subprocess.CompletedProcess:
    if runner is not None:
        return runner(root, *args)
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def branch_for(kind: str, key: str) -> str:
    return f"{kind}/{key}"


def dir_name(kind: str, key: str) -> str:
    return f"{kind}-{key}".replace("/", "-")


def worktree_root(cfg, live_root: Path) -> Path:
    """``git.worktree_root`` resolved against the live checkout (it is usually ``../``)."""
    raw = Path(str(cfg.git.worktree_root)).expanduser()
    return raw if raw.is_absolute() else (live_root / raw).resolve()


# --------------------------------------------------------------------------- linking


def _tracked_paths(root: Path, runner: GitRunner | None = None) -> set[str]:
    r = _run(root, "ls-files", runner=runner)
    return {line for line in (r.stdout or "").splitlines() if line}


def _has_tracked_under(tracked: set[str], rel: str) -> bool:
    prefix = rel + "/"
    return any(t == rel or t.startswith(prefix) for t in tracked)


def _link_tree(live: Path, wt: Path, rel: str, tracked: set[str], made: list[str],
               depth: int) -> None:
    """Link ``live`` into ``wt`` without ever displacing something git tracks."""
    if not live.exists():
        return
    target = wt / rel
    if not target.exists() and not target.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.symlink_to(live, target_is_directory=live.is_dir())
        except OSError:  # pragma: no cover - no symlink permission (Windows without dev mode)
            return
        made.append(rel)
        return
    if not (live.is_dir() and target.is_dir()) or depth <= 0:
        return
    for child in sorted(live.iterdir()):
        child_rel = f"{rel}/{child.name}"
        if _has_tracked_under(tracked, child_rel) and child.is_dir():
            _link_tree(child, wt, child_rel, tracked, made, depth - 1)
        elif child_rel not in tracked:
            _link_tree(child, wt, child_rel, tracked, made, depth - 1)


def _write_exclude(wt_path: Path, rels: list[str], runner: GitRunner | None = None) -> None:
    """Append the linked paths to the worktree's private exclude file."""
    if not rels:
        return
    r = _run(wt_path, "rev-parse", "--git-path", "info/exclude", runner=runner)
    raw = (r.stdout or "").strip()
    if not raw:
        return
    p = Path(raw)
    if not p.is_absolute():
        p = wt_path / p
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = [line for line in existing.splitlines() if line]
    if EXCLUDE_HEADER not in lines:
        lines.append(EXCLUDE_HEADER)
    for rel in rels:
        entry = f"/{rel}"
        if entry not in lines:
            lines.append(entry)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def link_data(wt_path: Path, live_root: Path, *, names: tuple[str, ...] = DATA_LINKS,
              runner: GitRunner | None = None) -> list[str]:
    """Symlink the untracked live data entries into ``wt_path``; returns what was linked."""
    tracked = _tracked_paths(wt_path, runner)
    made: list[str] = []
    for name in names:
        _link_tree(live_root / name, wt_path, name, tracked, made, MAX_LINK_DEPTH)
    _write_exclude(wt_path, made, runner)
    return made


# --------------------------------------------------------------------------- lifecycle


def create(cfg, kind: str, key: str, *, live_root: Path | None = None,
           runner: GitRunner | None = None, link: bool = True) -> Worktree:
    """Create (or reuse) the worktree for ``<kind>/<key>`` off ``git.live_branch``."""
    live = Path(live_root) if live_root is not None else paths.REPO_ROOT
    root = worktree_root(cfg, live)
    root.mkdir(parents=True, exist_ok=True)
    path = root / dir_name(kind, key)
    branch = branch_for(kind, key)
    if not (path / ".git").exists():
        r = _run(live, "worktree", "add", "-B", branch, str(path), cfg.git.live_branch,
                 runner=runner)
        if r.returncode != 0 and not (path / ".git").exists():
            raise WorktreeError(
                f"git worktree add {branch} -> {path} failed: {(r.stderr or '').strip()}")
    links = tuple(link_data(path, live, runner=runner)) if link else ()
    return Worktree(kind=kind, key=key, branch=branch, path=path, live_root=live, links=links)


def reset(wt: Worktree, *, runner: GitRunner | None = None) -> None:
    """Throw away the session's uncommitted mess — **in the worktree only**."""
    _run(wt.path, "reset", "--hard", runner=runner)
    _run(wt.path, "clean", "-fd", runner=runner)


def is_clean(wt_path: Path, *, runner: GitRunner | None = None) -> bool:
    r = _run(wt_path, "status", "--porcelain", runner=runner)
    return not (r.stdout or "").strip()


def commits_on(wt_path: Path, branch: str, base: str, *,
               runner: GitRunner | None = None) -> list[str]:
    """Commits on ``branch`` that ``base`` does not have, oldest first."""
    r = _run(wt_path, "log", "--format=%H", "--reverse", f"{base}..{branch}", runner=runner)
    return [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]


def remove(wt: Worktree | Path, *, live_root: Path | None = None,
           runner: GitRunner | None = None) -> None:
    """Drop a worktree (its branch survives — the commits are still needed by the merge)."""
    path = wt.path if isinstance(wt, Worktree) else Path(wt)
    live = Path(live_root) if live_root is not None else (
        wt.live_root if isinstance(wt, Worktree) else paths.REPO_ROOT)
    _run(live, "worktree", "remove", "--force", str(path), runner=runner)
    _run(live, "worktree", "prune", runner=runner)


def list_paths(live_root: Path, *, runner: GitRunner | None = None) -> list[Path]:
    r = _run(live_root, "worktree", "list", "--porcelain", runner=runner)
    out: list[Path] = []
    for line in (r.stdout or "").splitlines():
        if line.startswith("worktree "):
            p = Path(line.split(" ", 1)[1].strip())
            if p.resolve() != Path(live_root).resolve():
                out.append(p)
    return out


def prune(cfg, *, live_root: Path | None = None, runner: GitRunner | None = None,
          now: datetime | None = None) -> list[str]:
    """Remove worktrees older than ``git.worktree_ttl_days``; returns what went."""
    live = Path(live_root) if live_root is not None else paths.REPO_ROOT
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=int(cfg.git.worktree_ttl_days))
    root = worktree_root(cfg, live)
    removed: list[str] = []
    for path in list_paths(live, runner=runner):
        try:
            inside = path.resolve().is_relative_to(root)
        except (OSError, ValueError):  # pragma: no cover - unresolvable path
            inside = False
        if not inside or not path.exists():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if mtime < cutoff:
            remove(path, live_root=live, runner=runner)
            removed.append(str(path))
    _run(live, "worktree", "prune", runner=runner)
    return removed
