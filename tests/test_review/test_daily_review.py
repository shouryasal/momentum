"""daily_review: nightly grading of the previous Gulf day, wrong-decision rule
-> trace reports, hallucinated-citation lint, Laplace source reliability,
fallback rerun, idempotence, Sunday-grade respect."""

import json
import subprocess
from datetime import UTC, datetime

import pytest

from ops import db
from ops.config import REPO_ROOT, load_config
from runs.daily_review import DailyReview
from runs.decision_core import StageMeta, StageResult

NOW = datetime(2026, 9, 22, 17, 30, tzinfo=UTC)  # 21:30 Gulf on 2026-09-22
DAY = "2026-09-21"                               # the graded (previous) Gulf day
RID1 = "2026-09-21T08:30+04:00"
RID2 = "2026-09-21T16:00+04:00"


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / "prompts").mkdir()
    (root / "prompts" / "daily_review.v1.md").write_text(
        (REPO_ROOT / "prompts" / "daily_review.v1.md").read_text())
    (root / "config").mkdir()
    (root / "config" / "models.yaml").write_text(
        (REPO_ROOT / "config" / "models.yaml").read_text())
    (root / "changes").mkdir()
    (root / "lessons.md").write_text("# Earn lessons\n")
    (root / ".claude").mkdir()
    for sub in ("hooks", "skills"):
        subprocess.run(["cp", "-r", str(REPO_ROOT / ".claude" / sub),
                        str(root / ".claude" / sub)], capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, capture_output=True)
    journal, knowledge = db.init_all(cfg, root=root)
    jdb, kdb = db.connect(journal), db.connect(knowledge)
    alerts = []
    yield cfg, root, jdb, kdb, alerts
    jdb.close()
    kdb.close()


def _proposal(jdb, rid, ts):
    jdb.execute(
        "INSERT INTO proposals(run_id, shadow, ts_utc, valid, module, targets_json)"
        " VALUES (?,0,?,1,'trend','{\"BTC\":0.4,\"ETH\":0.25,\"USDT\":0.35}')",
        (rid, ts))
    jdb.commit()


def _grade(jdb, rid, grade, outcome=None, rubric='{"thesis_consistent": true}',
           week="2026-W39"):
    jdb.execute(
        "INSERT OR REPLACE INTO decision_grades(run_id, graded_at, review_week,"
        " process_grade, process_rubric_json, outcome_grade, grader_model,"
        " grader_run_id) VALUES (?,'x',?,?,?,?,'m','r')",
        (rid, week, grade, rubric, outcome))
    jdb.commit()


def session_ok(root, jdb, day=DAY):
    def runner(prompt, *, model, **kwargs):
        (root / "reports" / "daily").mkdir(parents=True, exist_ok=True)
        (root / "reports" / "daily" / f"{day}.md").write_text(
            f"# Daily review {day}\n\nnarrative\n")
        for line in prompt.splitlines():
            if line.startswith("- 2026-"):
                _grade(jdb, line[2:].strip(), 85)
        return StageResult(True, "done", StageMeta(subtype="success", cost_usd=1.2,
                                                   served_model=model,
                                                   applied_effort="max",
                                                   auth_source="none"))
    return runner


def _dr(cfg, root, jdb, kdb, alerts, runner):
    return DailyReview(cfg, jdb, kdb, root=root, now=NOW, session_runner=runner,
                       alert=lambda t, s="info": alerts.append((s, t)))


def test_happy_path(env):
    cfg, root, jdb, kdb, alerts = env
    _proposal(jdb, RID1, "2026-09-21T04:30:00Z")
    _proposal(jdb, RID2, "2026-09-21T12:00:00Z")
    dr = _dr(cfg, root, jdb, kdb, alerts, session_ok(root, jdb))
    assert dr.main_flow() == 0
    report = (root / "reports" / "daily" / f"{DAY}.md").read_text()
    assert "Deterministic appendix" in report and "wrong decisions: none" in report
    for rid in (RID1, RID2):
        assert jdb.execute("SELECT 1 FROM decision_grades WHERE run_id=?",
                           (rid,)).fetchone()
    row = jdb.execute("SELECT * FROM runs WHERE stage='daily_review'").fetchone()
    assert row["status"] == "success" and row["effort"] == "max"
    assert row["auth_source"] == "none" and row["run_id"] == f"daily-{DAY}"
    branches = subprocess.run(["git", "branch"], cwd=root, capture_output=True,
                              text=True).stdout
    assert f"daily/{DAY}" in branches
    assert (root / "reports" / "daily" / "packs" / f"{DAY}-inputs.json").exists()


def test_idempotent_on_report(env):
    cfg, root, jdb, kdb, alerts = env
    p = root / "reports" / "daily" / f"{DAY}.md"
    p.parent.mkdir(parents=True)
    p.write_text("done already\n")
    calls = []
    dr = _dr(cfg, root, jdb, kdb, alerts, lambda *a, **k: calls.append(1))
    assert dr.main_flow() == 0 and not calls


def test_sunday_graded_run_ids_skipped(env):
    cfg, root, jdb, kdb, alerts = env
    _proposal(jdb, RID1, "2026-09-21T04:30:00Z")
    _proposal(jdb, RID2, "2026-09-21T12:00:00Z")
    _grade(jdb, RID1, 90)  # Sunday's review already graded this one
    prompts = []

    def runner(prompt, *, model, **kwargs):
        prompts.append(prompt)
        return session_ok(root, jdb)(prompt, model=model, **kwargs)

    dr = _dr(cfg, root, jdb, kdb, alerts, runner)
    assert dr.ungraded_run_ids() == [RID2]
    assert dr.main_flow() == 0
    assert RID2 in prompts[0]
    graded_lines = [ln for ln in prompts[0].splitlines() if ln.startswith("- 2026")]
    assert graded_lines == [f"- {RID2}"]


class TestWrongDecisionRule:
    def test_matrix(self, env):
        cfg, root, jdb, kdb, alerts = env
        cases = [
            ("2026-09-21T08:30+04:00", 60, None, '{"a": true}', True),    # low grade
            ("2026-09-21T12:00+04:00", 80, "worse", '{"a": false}', True),
            ("2026-09-21T16:00+04:00", 80, "better", '{"a": false}', False),
            ("2026-09-21T18:00+04:00", 80, "worse", '{"a": true}', False),
        ]
        for i, (rid, grade, outcome, rubric, _) in enumerate(cases):
            _proposal(jdb, rid, f"2026-09-21T{4 + i:02d}:30:00Z")
            _grade(jdb, rid, grade, outcome, rubric)
        dr = _dr(cfg, root, jdb, kdb, alerts, session_ok(root, jdb))
        wrong = dr.wrong_decisions()
        assert [w[0] for w in wrong] == [rid for rid, *_, exp in cases if exp]
        for _, _why, path in wrong:
            assert path.exists() and "trace" in str(path)
        assert "process_grade 60" in wrong[0][1]
        assert "rubric failed: a" in wrong[1][1]


class TestCitationLint:
    def _brief(self, root, links):
        d = root / "knowledge" / "briefs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{DAY}.md").write_text(
            "# brief\n" + "\n".join(f"claim ([src]({li}))" for li in links))

    def test_known_links_pass(self, env):
        cfg, root, jdb, kdb, alerts = env
        kdb.execute("INSERT INTO news_items(url_hash, source, source_class, title,"
                    " url, fetched_at, classified_by) VALUES"
                    " ('h1','CoinDesk','secondary','t','https://ok/1','x','rule')")
        kdb.commit()
        self._brief(root, ["https://ok/1"])
        dr = _dr(cfg, root, jdb, kdb, alerts, session_ok(root, jdb))
        assert dr.citation_lint() == []
        assert jdb.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0

    def test_unknown_link_is_incident_and_recurrence(self, env):
        cfg, root, jdb, kdb, alerts = env
        self._brief(root, ["https://made.up/story"])
        dr = _dr(cfg, root, jdb, kdb, alerts, session_ok(root, jdb))
        assert dr.citation_lint() == ["https://made.up/story"]
        inc = jdb.execute("SELECT * FROM incidents").fetchone()
        assert inc["kind"] == "other" and inc["root_cause"] == "reasoning"
        rce = jdb.execute("SELECT * FROM root_cause_events").fetchone()
        assert rce["recurrence_key"] == "hallucinated_citation"
        assert rce["cause"] == "reasoning"
        assert any(s == "critical" and "HALLUCINATED" in t for s, t in alerts)
        # deterministic event_id: a rerun does not duplicate the recurrence row
        dr.citation_lint()
        assert jdb.execute("SELECT COUNT(*) FROM root_cause_events"
                           ).fetchone()[0] == 1


def test_source_reliability_laplace(env):
    cfg, root, jdb, kdb, alerts = env
    rows = [  # (hash, corroborated, claim_verified)
        ("a", 1, None), ("b", 1, 1), ("c", 0, 0),
    ]
    for h, corr, cv in rows:
        kdb.execute(
            "INSERT INTO news_items(url_hash, source, source_class, title, url,"
            " fetched_at, classified_by, corroborated, claim_verified)"
            " VALUES (?,?,?,?,?,?,'rule',?,?)",
            (h, "CoinDesk", "secondary", "t", f"https://n/{h}",
             "2026-09-21T10:00:00Z", corr, cv))
    kdb.commit()
    dr = _dr(cfg, root, jdb, kdb, alerts, session_ok(root, jdb))
    assert dr.update_source_reliability() == 1
    r = kdb.execute("SELECT * FROM source_reliability WHERE source='CoinDesk'"
                    ).fetchone()
    assert (r["n_unconfirmed"], r["n_corroborated_later"], r["n_falsified"],
            r["n_claims_checked"], r["n_claims_verified"]) == (1, 2, 1, 2, 1)
    # successes = 2 corroborated + 1 verified; trials = 2 + 1 unconfirmed + 2 checked
    assert r["score"] == pytest.approx((3 + 1) / (5 + 2), abs=1e-4)
    # a second day accumulates instead of resetting
    dr.update_source_reliability()
    r2 = kdb.execute("SELECT n_corroborated_later FROM source_reliability").fetchone()
    assert r2["n_corroborated_later"] == 4


def test_failed_session_reruns_on_fallback(env):
    cfg, root, jdb, kdb, alerts = env
    _proposal(jdb, RID1, "2026-09-21T04:30:00Z")
    calls = []

    def runner(prompt, *, model, **kwargs):
        calls.append(model)
        if model == cfg.daily_review.model:
            return StageResult(False, None, StageMeta(error="refused"))
        return session_ok(root, jdb)(prompt, model=model, **kwargs)

    dr = _dr(cfg, root, jdb, kdb, alerts, runner)
    assert dr.main_flow() == 0
    assert calls == [cfg.daily_review.model, cfg.daily_review.fallback_model]
    row = jdb.execute("SELECT requested_model FROM runs WHERE stage='daily_review'"
                      ).fetchone()
    assert row["requested_model"] == cfg.daily_review.fallback_model
    assert any("HELD" in t for _, t in alerts)


def test_kill_switch(env):
    cfg, root, jdb, kdb, alerts = env
    kp = root / cfg.risk.kill_file
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text("stop")
    dr = _dr(cfg, root, jdb, kdb, alerts, session_ok(root, jdb))
    assert dr.main_flow() == 0
    row = jdb.execute("SELECT status FROM runs WHERE stage='daily_review'").fetchone()
    assert row["status"] == "killed"


def test_grade_inputs_day_window(env):
    """--day builds the Gulf-day pack: [D-1 20:00Z, D 20:00Z)."""
    cfg, root, jdb, kdb, alerts = env
    _proposal(jdb, RID1, "2026-09-21T04:30:00Z")   # inside the Gulf day
    _proposal(jdb, "2026-09-22T08:30+04:00", "2026-09-22T04:30:00Z")  # next day
    import os
    import sys as _sys

    out = root / "pack.json"
    subprocess.run([_sys.executable,
                    str(root / ".claude/skills/post-mortem/scripts/grade_inputs.py"),
                    "--day", DAY, "--out", str(out)], cwd=root,
                   env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, check=True)
    pack = json.loads(out.read_text())
    assert [d["run_id"] for d in pack["decisions"]] == [RID1]
    assert pack["week_start"] == "2026-09-20" and pack["week_end"] == "2026-09-21"
