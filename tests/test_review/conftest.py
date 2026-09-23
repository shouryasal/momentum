"""Shared fixtures for the self-improvement suite: a real temp git repo that stands in for
the live checkout, plus a journal database.

Everything here is deliberately real git — the v2 design turns on worktrees, cherry-picks,
conflicts and reverts behaving the way git actually behaves, and a mock of git would have
happily passed the v1 code that shipped the HIGH bug.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config

LIVE_BRANCH = "live-main"
NOW = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def head(root: Path) -> str:
    return (git(root, "rev-parse", "HEAD").stdout or "").strip()


def branch(root: Path) -> str:
    return (git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()


def tree_state(root: Path) -> dict[str, str]:
    """Every tracked file's content — the byte-identical check for the live checkout."""
    listing = git(root, "ls-files").stdout.splitlines()
    out: dict[str, str] = {}
    for rel in listing:
        p = root / rel
        if p.is_file():
            try:
                out[rel] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:  # pragma: no cover - no binaries in the fixture
                out[rel] = "<binary>"
    return out


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return head(root)


@pytest.fixture
def live_cfg(tmp_path: Path) -> EarnConfig:
    """The repo config, pointed at a throwaway branch and worktree root."""
    cfg = load_config()
    cfg.git.live_branch = LIVE_BRANCH
    cfg.git.worktree_root = str(tmp_path / "worktrees")
    return cfg


@pytest.fixture
def live_repo(tmp_path: Path, live_cfg: EarnConfig, monkeypatch) -> Iterator[tuple]:
    """``(cfg, root, jdb)`` where ``root`` is a git repo on ``git.live_branch``."""
    root = tmp_path / "live"
    root.mkdir()
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    git(root, "init", "-b", LIVE_BRANCH)
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    for rel in ("config", "changes", "prompts", "reports", "knowledge",
                ".claude/skills", "ops/locks"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    # The real repo gitignores its data; without this the journal database lands in the
    # session's commit and every commit-scope check would (correctly) refuse it.
    (root / ".gitignore").write_text(
        "journal/\nvar/\nlogs/\nops/locks/\n"
        "knowledge/*.db*\nknowledge/flags.json\nknowledge/state/\n"
        "__pycache__/\n")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.30},
                                   "trend": {"ma_days": 200}}}, indent=2))
    (root / "config" / "backtest.yaml").write_text(
        "costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
    (root / "prompts" / "research.v2.md").write_text("# research v2\n\n{{STATE}}\n")
    (root / "lessons.md").write_text("# Earn lessons\n")
    (root / "changes" / ".gitkeep").write_text("")
    (root / "reports" / ".gitkeep").write_text("")
    (root / "knowledge" / ".gitkeep").write_text("")
    commit_all(root, "base")

    journal, _ = db.init_all(live_cfg, root=root)
    jdb = db.connect(journal)
    yield live_cfg, root, jdb
    jdb.close()


@pytest.fixture
def skill_src() -> Path:
    """The real skills directory, for tests that copy a skill into a temp repo."""
    return REPO_ROOT / ".claude" / "skills"
