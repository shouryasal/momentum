"""Worktree isolation — the fix for spec §11 issue 6.

The regression these tests exist for: the review jobs used to check out a branch and
``git reset --hard`` in the LIVE checkout, moving a running system's working tree and
leaving ``apply_changes`` cherry-picking onto the very HEAD the commit was already on.

So the load-bearing assertion is the boring one: after a full session the live checkout's
branch, HEAD and every tracked file are byte-identical.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from runs import worktree

from .conftest import LIVE_BRANCH, NOW, branch, commit_all, git, head, tree_state


def test_create_makes_a_branch_off_live_and_leaves_the_live_checkout_alone(live_repo):
    cfg, root, _ = live_repo
    before_head, before_branch, before_tree = head(root), branch(root), tree_state(root)

    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)

    assert wt.branch == "review/2026-W39"
    assert (wt.path / ".git").exists()
    assert (wt.path / "config" / "params-sleeve-a.json").exists()
    # the live checkout has not moved in any respect
    assert head(root) == before_head
    assert branch(root) == before_branch
    assert tree_state(root) == before_tree


def test_a_full_session_leaves_the_live_checkout_byte_identical(live_repo):
    """The HIGH-bug regression test."""
    cfg, root, _ = live_repo
    before = (head(root), branch(root), tree_state(root))

    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)
    # the session edits a tier-1 file and commits, twice, and then fails and is reset
    (wt.path / "config" / "params-sleeve-a.json").write_text('{"sleeve": "a", "params": {}}')
    commit_all(wt.path, "session edit")
    (wt.path / "prompts" / "research.v2.md").write_text("# rewritten\n")
    worktree.reset(wt)          # the fallback path: worktree only

    assert (head(root), branch(root), tree_state(root)) == before
    assert worktree.is_clean(wt.path)


def test_env_exports_the_three_roots_and_marks_the_run_automated(live_repo):
    cfg, root, _ = live_repo
    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)
    env = wt.env(base={})
    assert env["EARN_STATE_ROOT"] == str(root)
    assert env["EARN_LIVE_ROOT"] == str(root)
    assert env["EARN_WORKTREE"] == str(wt.path)
    assert env["EARN_AUTOMATED_RUN"] == "1"


def test_data_links_point_at_the_live_root_and_do_not_dirty_the_worktree(live_repo):
    cfg, root, _ = live_repo
    # untracked live data the session must see
    (root / "knowledge" / "flags.json").write_text('{"flags": {}}')
    (root / "knowledge" / "briefs").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "briefs" / "2026-09-26.md").write_text("# brief\n")

    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)

    linked = wt.path / "knowledge" / "flags.json"
    assert linked.exists(), wt.links
    assert linked.read_text() == '{"flags": {}}'
    assert (wt.path / "knowledge" / "briefs" / "2026-09-26.md").read_text() == "# brief\n"
    # git must not see the links, or apply_changes would refuse the merge as dirty
    assert worktree.is_clean(wt.path), git(wt.path, "status", "--porcelain").stdout


def test_a_write_through_the_link_lands_in_the_live_data_root(live_repo):
    cfg, root, _ = live_repo
    (root / "knowledge" / "briefs").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "briefs" / "keep.md").write_text("x")
    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)
    (wt.path / "knowledge" / "briefs" / "written-by-session.md").write_text("hello")
    assert (root / "knowledge" / "briefs" / "written-by-session.md").read_text() == "hello"


def test_create_is_idempotent(live_repo):
    cfg, root, _ = live_repo
    first = worktree.create(cfg, "review", "2026-W39", live_root=root)
    (first.path / "reports" / "note.md").write_text("draft\n")
    second = worktree.create(cfg, "review", "2026-W39", live_root=root)
    assert second.path == first.path
    assert (second.path / "reports" / "note.md").read_text() == "draft\n"


def test_commits_on_lists_only_the_session_commits(live_repo):
    cfg, root, _ = live_repo
    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)
    (wt.path / "prompts" / "research.v2.md").write_text("# one\n")
    first = commit_all(wt.path, "one")
    (wt.path / "prompts" / "research.v2.md").write_text("# two\n")
    second = commit_all(wt.path, "two")
    assert worktree.commits_on(wt.path, wt.branch, LIVE_BRANCH) == [first, second]


def test_prune_removes_worktrees_past_the_ttl_and_keeps_fresh_ones(live_repo):
    cfg, root, _ = live_repo
    old = worktree.create(cfg, "review", "2026-W30", live_root=root)
    fresh = worktree.create(cfg, "daily", "2026-09-26", live_root=root)
    stale = (NOW - timedelta(days=int(cfg.git.worktree_ttl_days) + 1)).timestamp()
    os.utime(old.path, (stale, stale))

    removed = worktree.prune(cfg, live_root=root, now=NOW)

    assert str(old.path) in removed
    assert str(fresh.path) not in removed
    assert not old.path.exists()
    assert fresh.path.exists()
    # and the live checkout is still where it was
    assert branch(root) == LIVE_BRANCH


def test_remove_drops_the_worktree_but_keeps_the_branch(live_repo):
    cfg, root, _ = live_repo
    wt = worktree.create(cfg, "review", "2026-W39", live_root=root)
    (wt.path / "prompts" / "research.v2.md").write_text("# kept\n")
    sha = commit_all(wt.path, "kept")
    worktree.remove(wt)
    assert not wt.path.exists()
    # the commit still exists in the shared object database — the merge needs it
    assert git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def test_worktree_root_resolves_relative_to_the_live_checkout(live_repo, tmp_path):
    cfg, root, _ = live_repo
    cfg.git.worktree_root = "../earn-worktrees"
    resolved = worktree.worktree_root(cfg, root)
    assert resolved == (root / ".." / "earn-worktrees").resolve()
    assert Path(resolved).name == "earn-worktrees"
