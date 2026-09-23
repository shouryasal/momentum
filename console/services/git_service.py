"""What git knows, for the header chip, the config History tab and config saves.

Every call shells out to ``git`` with an explicit working directory, a timeout and no
shell, and every failure is data rather than an exception: a host without git, a tarball
instead of a checkout, or a repository the console cannot read all come back as
``GitInfo(available=False, error=...)``. The header must render anyway.

Only :func:`commit_paths` writes, and only what it is given: it stages exactly the listed
paths and commits them with the audit reason. It never pushes, never checks out, never
resets.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from console import security
from ops.lib import paths

DEFAULT_TIMEOUT_S = 5.0
COMMIT_TIMEOUT_S = 15.0
LOG_FORMAT = "%H%x1f%an%x1f%aI%x1f%s"


class GitError(RuntimeError):
    """A git command that the caller asked to succeed did not."""


@dataclass(frozen=True)
class GitInfo:
    """Branch/commit/dirty for the header and ``GET /api/meta``."""

    available: bool
    branch: str | None = None
    commit: str | None = None
    short: str | None = None
    dirty: bool = False
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "branch": self.branch,
            "commit": self.commit,
            "short": self.short,
            "dirty": self.dirty,
            "error": self.error,
        }


def _root(root: Path | str | None) -> Path:
    return Path(root) if root is not None else paths.REPO_ROOT


def run_git(
    args: Sequence[str], *, root: Path | str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
) -> tuple[int, str, str]:
    """``(returncode, stdout, stderr)``; ``(127, "", reason)`` when git is unusable."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(_root(root)),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0] if args else ''} timed out after {timeout_s:g}s"
    except OSError as e:
        return 126, "", str(e)
    return proc.returncode, proc.stdout.strip(), security.redact(proc.stderr.strip())


def is_repo(root: Path | str | None = None) -> bool:
    code, out, _ = run_git(["rev-parse", "--is-inside-work-tree"], root=root)
    return code == 0 and out == "true"


def git_info(root: Path | str | None = None) -> GitInfo:
    """Never raises: an unavailable git is reported, not thrown."""
    code, out, err = run_git(["rev-parse", "--is-inside-work-tree"], root=root)
    if code != 0 or out != "true":
        return GitInfo(available=False, error=err or "not a git checkout")
    _, commit, _ = run_git(["rev-parse", "HEAD"], root=root)
    _, branch, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], root=root)
    status_code, status, status_err = run_git(["status", "--porcelain"], root=root)
    return GitInfo(
        available=True,
        branch=branch or None,
        commit=commit or None,
        short=commit[:8] if commit else None,
        dirty=bool(status.strip()) if status_code == 0 else False,
        error=status_err or None,
    )


def file_history(
    path: str, *, root: Path | str | None = None, limit: int = 20
) -> list[dict[str, str]]:
    """Recent commits touching ``path`` — the per-field "last changed by X at T" blame."""
    code, out, err = run_git(
        ["log", f"-{int(limit)}", f"--format={LOG_FORMAT}", "--", path], root=root
    )
    if code != 0:
        raise GitError(err or f"git log failed for {path}")
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        rows.append(
            {"commit": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
        )
    return rows


def show_file(rev: str, path: str, *, root: Path | str | None = None) -> str:
    """The content of ``path`` at ``rev`` — what the History tab diffs against."""
    code, out, err = run_git(["show", f"{rev}:{path}"], root=root)
    if code != 0:
        raise GitError(err or f"cannot read {path} at {rev}")
    return out


def diff(path: str | None = None, *, root: Path | str | None = None, staged: bool = False) -> str:
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args += ["--", path]
    code, out, err = run_git(args, root=root)
    if code != 0:
        raise GitError(err or "git diff failed")
    return out


def commit_paths(
    files: Sequence[str],
    message: str,
    *,
    root: Path | str | None = None,
    allow_empty: bool = False,
) -> str | None:
    """Stage exactly ``files`` and commit them. Returns the new sha, or ``None`` if nothing
    changed. Raises :class:`GitError` on a real failure; never pushes."""
    if not files:
        raise GitError("commit_paths needs at least one path")
    code, _, err = run_git(["add", "--", *files], root=root, timeout_s=COMMIT_TIMEOUT_S)
    if code != 0:
        raise GitError(err or "git add failed")
    status_code, status, _ = run_git(["diff", "--cached", "--name-only"], root=root)
    if status_code == 0 and not status.strip() and not allow_empty:
        return None
    args = ["commit", "-m", message, "--", *files]
    code, _, err = run_git(args, root=root, timeout_s=COMMIT_TIMEOUT_S)
    if code != 0:
        raise GitError(err or "git commit failed")
    _, sha, _ = run_git(["rev-parse", "HEAD"], root=root)
    return sha or None
