"""apply_changes gate matrix in a temp git repo: each failing gate -> its verdict;
all-pass -> cherry-picked merge + tag + change_log; tier-2 diff refusal; fallback
authorship held; param budget; freeze hold; counterfactual-null."""

import json
import subprocess
from datetime import UTC, datetime

import pytest

from ops import db
from ops.config import load_config
from ops.lib import flags as flagslib
from runs import apply_changes

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """Temp git repo with a params commit on a review branch + journal DB."""
    cfg = load_config()
    root = tmp_path
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "config").mkdir()
    (root / "changes").mkdir()
    (root / "config" / "params-sleeve-a.json").write_text(
        json.dumps({"sleeve": "a", "params": {"vol": {"target_annual": 0.30}}}))
    (root / "config" / "backtest.yaml").write_text(
        "costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    _git(root, "checkout", "-b", "review/2026-W39")
    (root / "config" / "params-sleeve-a.json").write_text(
        json.dumps({"sleeve": "a", "params": {"vol": {"target_annual": 0.25}}}))
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "vol target 0.30 -> 0.25")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "main")

    journal, _ = db.init_all(cfg, root=root)
    flagslib.touch(root / cfg.paths.flags_file, now=NOW)
    jdb = db.connect(journal)
    jdb.execute("INSERT INTO replay_runs(replay_id, candidate_ref, baseline_ref,"
                " model, days, snapshots_used, scores_json, passed)"
                " VALUES ('rp-pass','prompt:x','prompt:y','m',30,25,'{}',1)")
    jdb.execute("INSERT INTO replay_runs(replay_id, candidate_ref, baseline_ref,"
                " model, days, snapshots_used, scores_json, passed)"
                " VALUES ('rp-fail','prompt:x','prompt:y','m',30,5,'{}',0)")
    jdb.commit()
    yield cfg, root, jdb, commit
    jdb.close()


def change(commit, **over):
    c = {
        "id": "2026-09-27-vol-down", "created_at": "2026-09-27T16:30:00Z",
        "author_run_id": "review-2026-W39", "author_model": "claude-fable-5-1",
        "prompt_version": "review.v1", "tier": 1, "kind": "params",
        "target": "config/params-sleeve-a.json",
        "what": {"summary": "vol 0.30 -> 0.25", "commit": commit},
        "why": "3 repeated reasoning root causes on high-vol whipsaw",
        "bounds_check": [{"param": "sleeve_a.vol.target_annual", "old": 0.30,
                          "new": 0.25, "min": 0.10, "max": 0.50, "max_step": 0.05,
                          "ok": True}],
        "backtest": {"timerange": "20240901-20260901", "years": 2.0, "fee_bps": 10.0,
                     "slippage_bps": 5.0, "baseline": {}, "candidate": {}},
        "walk_forward": {"windows": 8, "scheme": "expanding", "in_sample_delta": 2.0,
                         "out_sample_delta": 1.5, "pass": True},
        "replay": {"replay_id": "rp-pass", "days": 30, "scores": {},
                   "baseline_scores": {}, "pass": True},
        "counterfactual": {"week": "2026-W39", "decisions_changed": 2,
                           "process_grade_delta": 4.0},
        "branch": "review/2026-W39", "status": "proposed",
    }
    c.update(over)
    return c


def _check(repo, c):
    cfg, root, jdb, _ = repo
    return apply_changes.check(c, cfg, jdb, root,
                               backtest_costs={"fee_bps": 10.0, "slippage_bps": 5.0},
                               now=NOW)


def test_all_pass_auto_merges_and_tags(repo):
    cfg, root, jdb, commit = repo
    (root / "changes" / "2026-09-27-vol-down.json").write_text(
        json.dumps(change(commit)))
    results = apply_changes.apply_all(cfg, jdb, root, NOW)
    assert results == [("2026-09-27-vol-down", "auto_merged",
                        pytest.approx("ok", abs=0) if False else "ok")] or \
           results[0][1] == "auto_merged"
    # merged onto main, tagged, params file carries the new value
    assert "0.25" in (root / "config" / "params-sleeve-a.json").read_text()
    assert "change/2026-09-27-vol-down" in _git(root, "tag").stdout
    row = jdb.execute("SELECT * FROM change_log").fetchone()
    assert row["status"] == "auto_merged" and row["is_param_change"] == 1
    saved = json.loads((root / "changes" / "2026-09-27-vol-down.json").read_text())
    assert saved["status"] == "auto_merged"


@pytest.mark.parametrize("mutate, verdict, needle", [
    (lambda c: c.update(tier=2), "reject", "schema"),
    (lambda c: c.update(target="strategies/riskgate.py"), "reject", "tier-2"),
    (lambda c: c["bounds_check"][0].update(new=0.05), "reject", "outside"),
    (lambda c: c["bounds_check"][0].update(old=0.45, new=0.25), "reject", "step"),
    (lambda c: c["backtest"].update(years=1.5), "reject", "backtest"),
    (lambda c: c["backtest"].update(fee_bps=7.0), "reject", "costs"),
    (lambda c: c["walk_forward"].update(out_sample_delta=-1.0, **{"pass": False}),
     "reject", "walk-forward"),
    (lambda c: c["replay"].update(replay_id="rp-missing"), "reject", "not found"),
    (lambda c: c["replay"].update(replay_id="rp-fail"), "hold", "replay"),
    (lambda c: c["counterfactual"].update(decisions_changed=0, process_grade_delta=0.0),
     "reject", "counterfactual-null"),
    (lambda c: c.update(author_model="claude-opus-5"), "hold", "fallback"),
    (lambda c: c.update(kind="model", target="config/models.yaml",
                        bounds_check=None) or c.pop("bounds_check"),
     "hold", "human"),
])
def test_gate_matrix(repo, mutate, verdict, needle):
    _, _, _, commit = repo
    c = change(commit)
    mutate(c)
    res = _check(repo, c)
    assert res.verdict == verdict, (res.verdict, res.reason)
    assert needle in res.reason


def test_commit_touching_tier2_rejected(repo):
    cfg, root, jdb, _ = repo
    _git(root, "checkout", "review/2026-W39")
    (root / "strategies").mkdir(exist_ok=True)
    (root / "strategies" / "riskgate.py").write_text("# sneaky\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "touch tier2")
    bad_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "main")
    res = _check(repo, change(bad_commit))
    assert res.verdict == "reject" and "tier-2 paths" in res.reason


def test_param_budget_two_per_month(repo):
    cfg, root, jdb, commit = repo
    for i in (1, 2):
        jdb.execute("INSERT INTO change_log(change_id, proposed_at, kind, target,"
                    " status, author_model, decided_at, is_param_change)"
                    " VALUES (?,?, 'params','x','auto_merged','m',?,1)",
                    (f"c{i}", "2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z"))
    jdb.commit()
    res = _check(repo, change(commit))
    assert res.verdict == "reject" and "budget" in res.reason


def test_tier1_freeze_holds(repo):
    cfg, root, jdb, commit = repo
    flagslib.set_flag(root / cfg.paths.flags_file, "tier1_freeze",
                      severity="freeze_tier1", reason="tca gap", set_by="tca_job",
                      now=NOW)
    res = _check(repo, change(commit))
    assert res.verdict == "hold" and "freeze" in res.reason


def test_live_phase_holds_for_approval(repo, monkeypatch):
    cfg, root, jdb, commit = repo
    cfg2 = cfg.model_copy(deep=True)
    cfg2.autonomy.tier1_auto_merge = False
    (root / "changes" / "2026-09-27-vol-down.json").write_text(
        json.dumps(change(commit)))
    results = apply_changes.apply_all(cfg2, jdb, root, NOW)
    assert results[0][1] == "held"
    assert "Telegram" in results[0][2]
