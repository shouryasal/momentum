"""Step 6 — a tier-1 change: worktree, recomputed evidence, merge, revert.

Git is real here, top to bottom: a live checkout on ``git.live_branch``, a real
``git worktree`` off it, a real commit inside the worktree, a real cherry-pick onto the
live branch and a real ``git revert``. The only injected seams are the two runners that
would shell out to Docker for a backtest (there is no Docker here), and they are injected
with *plausible* numbers so the check that matters — the one recomputing the parameter
arithmetic from the commit diff itself — stays real.

The point of the whole design is the middle act: the model's own reported numbers are
stored and never gate anything. A change that claims a 40% edge it did not earn must be
held, with the mismatch on the record.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals import verify_change
from ops import db
from ops.config import load_config
from runs import apply_changes, worktree

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

LIVE_BRANCH = "e2e-live"
NOW = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
AUTHOR_RUN = "review-2026-W39"
AUTHOR_MODEL = "claude-fable-5-1"


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def head(root: Path) -> str:
    return (git(root, "rev-parse", "HEAD").stdout or "").strip()


@pytest.fixture
def cfg(tmp_path: Path):
    c = load_config()
    c.git.live_branch = LIVE_BRANCH
    c.git.worktree_root = str(tmp_path / "worktrees")
    c.autonomy.tier1_auto_merge = True
    c.autonomy.kinds["params"].test = "auto"
    return c


@pytest.fixture
def live(cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real git checkout that stands in for the live tree, plus its journal."""
    root = tmp_path / "live"
    root.mkdir()
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    git(root, "init", "-b", LIVE_BRANCH)
    git(root, "config", "user.email", "e2e@earn")
    git(root, "config", "user.name", "e2e")
    git(root, "config", "commit.gpgsign", "false")
    for rel in ("config", "changes", "prompts", "reports", "knowledge", "ops/locks"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(
        "journal/\nvar/\nlogs/\nops/locks/\nknowledge/*.db*\nknowledge/flags.json\n"
        "knowledge/state/\n__pycache__/\n")
    (root / "config" / "params-sleeve-a.json").write_text(json.dumps(
        {"sleeve": "a", "params": {"vol": {"target_annual": 0.30},
                                   "trend": {"ma_days": 200}}}, indent=2) + "\n")
    (root / "config" / "backtest.yaml").write_text(
        "costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
    # `apply_changes.check` refuses to merge while `tier1_freeze` cannot be read — an
    # absent flags file is a freeze, by design. Write the empty, current one.
    flags = root / cfg.paths.flags_file
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(json.dumps({
        "version": 1, "updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "flags": {},
    }))
    for rel in ("changes", "reports", "knowledge"):
        (root / rel / ".gitkeep").write_text("")
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")

    journal, _knowledge = db.init_all(cfg, root=root)
    jdb = db.connect(journal)
    jdb.execute(
        "INSERT INTO runs(run_id, stage, kind, started_utc, status, requested_model,"
        " served_model) VALUES (?, 'review', 'review', ?, 'success', ?, ?)",
        (AUTHOR_RUN, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), AUTHOR_MODEL, AUTHOR_MODEL))
    jdb.commit()
    yield cfg, root, jdb
    jdb.close()


def fake_runners() -> verify_change.Runners:
    """Docker is not here. Everything these two return is deliberately *modest*."""
    def backtest(arm: str, cwd: Path, timerange: str, fee: float, strategy: str) -> dict:
        base = {"cagr": 0.12, "max_drawdown": 0.18, "sharpe": 0.9, "trades": 120}
        if arm == "candidate":
            base = {"cagr": 0.14, "max_drawdown": 0.17, "sharpe": 1.0, "trades": 118}
        return base

    def walk_forward(arm: str, cwd: Path) -> dict:
        oos = 0.04 if arm == "candidate" else 0.03
        return {"windows": [{"oos_return": oos}, {"oos_return": oos}]}

    return verify_change.Runners(backtest=backtest, walk_forward=walk_forward)


def make_candidate(cfg, root: Path, *, new_value: float):
    """A real worktree, a real tier-1 edit, a real commit on its own branch."""
    wt = worktree.create(cfg, "review", "2026-W39", live_root=root, link=False)
    assert wt.path.exists(), wt
    target = wt.path / "config" / "params-sleeve-a.json"
    payload = json.loads(target.read_text())
    payload["params"]["vol"]["target_annual"] = new_value
    target.write_text(json.dumps(payload, indent=2) + "\n")
    git(wt.path, "add", "-A")
    git(wt.path, "commit", "-m", f"vol target -> {new_value}")
    return head(wt.path), wt.branch, wt.path


def change_doc(commit: str, branch: str, *, claimed: list[dict],
               change_id: str = "2026-09-27-vol-down",
               walk_forward: dict | None = None) -> dict:
    doc = {
        "id": change_id,
        "created_at": "2026-09-27T16:30:00Z",
        "author_run_id": AUTHOR_RUN,
        "author_model": AUTHOR_MODEL,
        "prompt_version": "review.v2",
        "tier": 1,
        "kind": "params",
        "target": "config/params-sleeve-a.json",
        "what": {"summary": "vol target 0.30 -> 0.25", "commit": commit, "op": "edit"},
        "why": "three repeated whipsaw root causes in high-vol weeks this quarter",
        "branch": branch,
        "status": "proposed",
        "bounds_check": claimed,
    }
    if walk_forward is not None:
        doc["walk_forward"] = walk_forward
    return doc


def write_change(root: Path, change: dict) -> Path:
    path = root / "changes" / f"{change['id']}.json"
    path.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n")
    return path


class TestWorktree:
    def test_a_worktree_is_a_real_checkout_off_the_live_branch(self, live) -> None:
        cfg, root, _jdb = live
        wt = worktree.create(cfg, "review", "2026-W39", live_root=root, link=False)
        assert (wt.path / ".git").exists()
        assert wt.branch == "review/2026-W39"
        assert (wt.path / "config" / "params-sleeve-a.json").exists()
        # The live checkout is untouched and still on its own branch.
        assert (git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip() \
            == LIVE_BRANCH
        listed = worktree.list_paths(root)
        assert any(p == wt.path for p in listed), listed


class TestEvidenceRecomputation:
    def test_the_recomputation_overrides_what_the_model_reported(self, live) -> None:
        """The model claims 0.30 -> 0.10. The commit says 0.30 -> 0.25. The commit wins."""
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.25)
        change = change_doc(commit, branch, claimed=[
            {"param": "sleeve_a.vol.target_annual", "old": 0.30, "new": 0.10,
             "min": 0.10, "max": 0.50, "max_step": 0.05, "ok": True},
        ])

        result = verify_change.verify(change, cfg, jdb, live_root=root,
                                      worktree=wt_path, runners=fake_runners(), now=NOW)
        assert result.verdict == "pass", result.reason

        verified = {row["param"]: row for row in result.verified["bounds_check"]}
        assert verified["sleeve_a.vol.target_annual"]["new"] == 0.25, verified
        assert verified["sleeve_a.vol.target_annual"]["old"] == 0.30, verified
        # The claim is untouched and is NOT what the gate read.
        assert change["bounds_check"][0]["new"] == 0.10
        bounds = next(c for c in result.checks if c.name == "bounds")
        assert bounds.verdict == "pass"
        assert bounds.data["bounds"][0]["new"] == 0.25

    def test_an_inflated_scalar_claim_is_flagged_as_a_mismatch(self, live) -> None:
        """``walk_forward.out_sample_delta`` is a scalar, so the comparator sees it."""
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.25)
        change = change_doc(commit, branch, claimed=[], walk_forward={
            "windows": 2, "scheme": "expanding", "in_sample_delta": None,
            "out_sample_delta": 9.9, "pass": True,
        })
        result = verify_change.verify(change, cfg, jdb, live_root=root,
                                      worktree=wt_path, runners=fake_runners(), now=NOW)
        fields = {m["field"] for m in result.mismatches}
        assert "walk_forward.out_sample_delta" in fields, result.mismatches
        hit = next(m for m in result.mismatches
                   if m["field"] == "walk_forward.out_sample_delta")
        assert hit["claimed"] == 9.9
        assert hit["verified"] != 9.9

    def test_a_list_shaped_claim_is_compared_entry_by_entry(self, live) -> None:
        """REGRESSION. ``flatten`` used not to descend into lists, and ``bounds_check``
        is exactly that shape — so a model could claim any parameter arithmetic it liked
        and ``find_mismatches`` raised nothing. The gate never trusted the claim (it
        reads only the recomputation), but "evidence is recomputed, never trusted" also
        means the divergence has to be *visible*: on the console's claimed-vs-verified
        panel, and in ``change_log.checks_json``.

        The commit really moves ``vol.target_annual`` to 0.25. The claim says 0.10, and
        invents a ``max_step`` three times the real one.
        """
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.25)
        change = change_doc(commit, branch, claimed=[
            {"param": "sleeve_a.vol.target_annual", "old": 0.30, "new": 0.10,
             "min": 0.10, "max": 0.50, "max_step": 0.15, "ok": True},
        ])
        result = verify_change.verify(change, cfg, jdb, live_root=root,
                                      worktree=wt_path, runners=fake_runners(), now=NOW)
        by_field = {m["field"]: m for m in result.mismatches}
        key = "bounds_check[sleeve_a.vol.target_annual]"
        assert f"{key}.new" in by_field, result.mismatches
        assert by_field[f"{key}.new"]["claimed"] == 0.10
        assert by_field[f"{key}.new"]["verified"] == 0.25
        assert f"{key}.max_step" in by_field, result.mismatches
        # Entries are keyed by `param`, not by position, so a reordered claim still lines
        # up with the recomputation it is about.
        assert verify_change.flatten({"a": [{"param": "p", "b": 1}]}) == {
            "a[p].param": "p", "a[p].b": 1}
        assert verify_change.flatten({"a": [{"b": 1}, {"b": 2}]}) == {
            "a[0].b": 1, "a[1].b": 2}

    def test_a_claim_never_widens_the_bounds(self, live) -> None:
        """Claiming the step is legal does not make it legal."""
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.12)  # a 0.18 step, max 0.05
        change = change_doc(commit, branch, claimed=[
            {"param": "sleeve_a.vol.target_annual", "old": 0.30, "new": 0.12,
             "min": 0.10, "max": 0.50, "max_step": 0.50, "ok": True},
        ])
        result = verify_change.verify(change, cfg, jdb, live_root=root,
                                      worktree=wt_path, runners=fake_runners(), now=NOW)
        assert result.verdict in ("fail", "reject"), result.reason
        assert "step" in result.reason, result.reason

    def test_both_the_claim_and_the_recomputation_are_stored(self, live) -> None:
        """``change_log`` keeps the model's claim beside what was recomputed, so the
        over-claim is visible afterwards even though it never gated anything."""
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.25)
        change = change_doc(commit, branch, claimed=[
            {"param": "sleeve_a.vol.target_annual", "old": 0.30, "new": 0.10,
             "min": 0.10, "max": 0.50, "max_step": 0.05, "ok": True},
        ])
        path = write_change(root, change)

        def verifier(c, config, journal, **kwargs):
            kwargs["worktree"] = wt_path
            return verify_change.verify(c, config, journal, runners=fake_runners(),
                                        **kwargs)

        before = head(root)
        _id, status, _reason = apply_changes.apply_one(
            change, path, cfg, jdb, root, now=NOW, alert=lambda *a, **k: None,
            verifier=verifier, mode="test")
        # The merge still happens (the arithmetic is sound); the mismatch is recorded
        # against it so a human sees the model over-claimed.
        rows = jdb.execute(
            "SELECT claimed_evidence_json, verified_evidence_json, status"
            " FROM change_log WHERE change_id=?", (change["id"],)).fetchone()
        assert rows is not None, "no change_log row"
        claimed = json.loads(rows["claimed_evidence_json"])
        verified = json.loads(rows["verified_evidence_json"])
        assert claimed["bounds_check"][0]["new"] == 0.10
        assert verified["bounds_check"][0]["new"] == 0.25
        assert status in ("auto_merged", "held"), status
        if status == "auto_merged":
            assert head(root) != before
        events = [r["event"] for r in jdb.execute(
            "SELECT event FROM change_events WHERE change_id=? ORDER BY id",
            (change["id"],))]
        assert "verifying" in events and events[-1] in ("merged", "held")


class TestApplyAndRevert:
    def test_apply_then_revert(self, live) -> None:
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.25)
        change = change_doc(commit, branch, claimed=[
            {"param": "sleeve_a.vol.target_annual", "old": 0.30, "new": 0.25,
             "min": 0.10, "max": 0.50, "max_step": 0.05, "ok": True},
        ])
        path = write_change(root, change)

        def verifier(c, config, journal, **kwargs):
            kwargs["worktree"] = wt_path
            return verify_change.verify(c, config, journal, runners=fake_runners(),
                                        **kwargs)

        base_head = head(root)
        _id, status, reason = apply_changes.apply_one(
            change, path, cfg, jdb, root, now=NOW, alert=lambda *a, **k: None,
            verifier=verifier, mode="test")
        assert status == "auto_merged", reason

        target = root / "config" / "params-sleeve-a.json"
        assert json.loads(target.read_text())["params"]["vol"]["target_annual"] == 0.25
        assert head(root) != base_head
        tags = (git(root, "tag", "--list").stdout or "").split()
        assert f"change/{change['id']}" in tags, tags
        assert json.loads(path.read_text())["status"] == "auto_merged"

        # ---- revert -----------------------------------------------------------
        outcome, note = apply_changes.revert(change["id"], "human:console:e2e", cfg, jdb,
                                             root, reason="e2e undo", now=NOW)
        assert outcome == "reverted", note
        assert json.loads(target.read_text())["params"]["vol"]["target_annual"] == 0.30
        row = jdb.execute("SELECT status, reverted_by FROM change_log WHERE change_id=?",
                          (change["id"],)).fetchone()
        assert row["status"] == "reverted"
        assert row["reverted_by"], "no revert commit recorded"
        assert json.loads(path.read_text())["status"] == "reverted"

        events = [r["event"] for r in jdb.execute(
            "SELECT event FROM change_events WHERE change_id=? ORDER BY id",
            (change["id"],))]
        assert events[-1] == "reverted", events

        second = apply_changes.revert(change["id"], "human:console:e2e", cfg, jdb, root,
                                      now=NOW)
        assert second[0] == "conflict", second

    def test_a_tier_two_target_is_rejected_outright(self, live) -> None:
        cfg, root, jdb = live
        commit, branch, wt_path = make_candidate(cfg, root, new_value=0.25)
        change = change_doc(commit, branch, claimed=[], change_id="2026-09-27-tier2")
        change["target"] = "ops/healthcheck.py"
        result = apply_changes.check(change, cfg, jdb, root, mode="test")
        assert result.verdict == "reject", result.reason
        assert "tier-2" in result.reason
