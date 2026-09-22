"""review_run wrapper: preflight (branch, packs, lesson archiving), fable->opus
explicit rerun with holds, recurring-cause escalation, per-model table, shadow
verdict, fewshot refresh, review skill lint."""

import json
import re
import subprocess
from datetime import UTC, datetime

import pytest
import yaml

from ops import db
from ops.config import REPO_ROOT, load_config
from runs.decision_core import StageMeta, StageResult
from runs.review_run import ReviewRun, prev_weeks

NOW = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)  # Sunday 20:00 Gulf
WEEK = "2026-W39"


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / "prompts" / "examples").mkdir(parents=True)
    (root / "prompts" / "review.v1.md").write_text(
        (REPO_ROOT / "prompts" / "review.v1.md").read_text())
    (root / "config").mkdir()
    (root / "config" / "models.yaml").write_text(
        (REPO_ROOT / "config" / "models.yaml").read_text())
    (root / "config" / "backtest.yaml").write_text(
        "costs:\n  fee_bps: 10.0\n  slippage_bps: 5.0\n")
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


def session_ok(week=WEEK, root=None, jdb=None):
    """A fake session that produces the required outputs."""

    def runner(prompt, *, model, **kwargs):
        (root / "reports").mkdir(exist_ok=True)
        (root / "reports" / f"review-{week}.md").write_text(
            f"# Review {week}\n\ngraded table here\n\n## Per-model quality\n")
        jdb.execute("INSERT OR REPLACE INTO decision_grades(run_id, graded_at,"
                    " review_week, process_grade, process_rubric_json, grader_model,"
                    " grader_run_id) VALUES ('2026-09-23T08:30+04:00','x',?,80,'{}',?,'r')",
                    (week, model))
        jdb.commit()
        return StageResult(True, "done", StageMeta(subtype="success", cost_usd=3.5,
                                                   served_model=model))
    return runner


def test_happy_path_fable(env):
    cfg, root, jdb, kdb, alerts = env
    rr = ReviewRun(cfg, jdb, kdb, root=root, now=NOW,
                   session_runner=session_ok(root=root, jdb=jdb),
                   alert=lambda t, s="info": alerts.append((s, t)))
    assert rr.main_flow() == 0
    report = (root / "reports" / f"review-{WEEK}.md").read_text()
    assert "## Per-model quality" in report
    row = jdb.execute("SELECT * FROM runs WHERE kind='review'").fetchone()
    assert row["status"] == "success" and row["requested_model"] == "claude-fable-5-1"
    # branch created
    branches = subprocess.run(["git", "branch"], cwd=root, capture_output=True,
                              text=True).stdout
    assert f"review/{WEEK}" in branches
    # grading packs generated
    assert (root / "reports" / "weekly" / WEEK / "inputs.json").exists()


def test_fable_failure_reruns_on_opus(env):
    cfg, root, jdb, kdb, alerts = env
    calls = []

    def runner(prompt, *, model, **kwargs):
        calls.append(model)
        if model == "claude-fable-5-1":
            return StageResult(False, None, StageMeta(error="refused"))
        return session_ok(root=root, jdb=jdb)(prompt, model=model, **kwargs)

    rr = ReviewRun(cfg, jdb, kdb, root=root, now=NOW, session_runner=runner,
                   alert=lambda t, s="info": alerts.append((s, t)))
    assert rr.main_flow() == 0
    assert calls == ["claude-fable-5-1", "claude-opus-5"]
    row = jdb.execute("SELECT requested_model FROM runs WHERE kind='review'").fetchone()
    assert row["requested_model"] == "claude-opus-5"
    assert any("HELD" in t for _, t in alerts)


def test_recurring_cause_escalates_after_3_weeks(env):
    cfg, root, jdb, kdb, alerts = env
    for w in prev_weeks(WEEK, 3):
        jdb.execute("INSERT INTO root_cause_events(event_id, review_week, kind, cause,"
                    " recurrence_key, fix_path, learn_eligible, eligibility_rule,"
                    " evidence_json) VALUES (?,?,'missed_run','ops','cron-drift','x',0,'r','{}')",
                    (f"e-{w}", w))
    jdb.commit()
    rr = ReviewRun(cfg, jdb, kdb, root=root, now=NOW,
                   session_runner=session_ok(root=root, jdb=jdb),
                   alert=lambda t, s="info": alerts.append((s, t)))
    escalated = rr.check_recurring_causes()
    assert escalated == ["cron-drift"]
    assert any(s == "critical" and "RECURRING" in t for s, t in alerts)
    # marked: a second call does not re-escalate
    assert rr.check_recurring_causes() == []


def test_shadow_verdict_and_promotion_alert(env):
    cfg, root, jdb, kdb, alerts = env
    mc = yaml.safe_load((root / "config" / "models.yaml").read_text())
    mc["shadow"].update({"enabled": True, "model": "sonnet",
                         "started": "2026-08-01", "days": 30})
    (root / "config" / "models.yaml").write_text(yaml.safe_dump(mc))
    targets = json.dumps({"BTC": 0.4, "ETH": 0.25, "USDT": 0.35})
    for i in range(10):
        rid = f"2026-09-{10 + i:02d}T08:30+04:00"
        for shadow in (0, 1):
            jdb.execute("INSERT INTO proposals(run_id, shadow, ts_utc, valid, module,"
                        " targets_json, model) VALUES (?,?,?,1,'trend',?,?)",
                        (rid, shadow, f"2026-09-{10 + i:02d}T04:30:00Z", targets,
                         "claude-sonnet-5" if shadow else "claude-opus-5"))
    jdb.commit()
    rr = ReviewRun(cfg, jdb, kdb, root=root, now=NOW,
                   session_runner=session_ok(root=root, jdb=jdb),
                   alert=lambda t, s="info": alerts.append((s, t)))
    stats = rr.grade_shadow()
    assert stats["agreement_rate"] == 1.0 and stats["window_done"]
    assert stats["recommendation"] == "PROMOTE"
    assert any("promotion" in t for _, t in alerts)


def test_fewshot_refresh_monthly(env):
    cfg, root, jdb, kdb, alerts = env
    targets = json.dumps({"BTC": 0.4, "ETH": 0.25, "USDT": 0.35})
    for i, grade in enumerate((30, 45, 80, 95)):
        rid = f"2026-09-{10 + i:02d}T08:30+04:00"
        jdb.execute("INSERT INTO proposals(run_id, shadow, ts_utc, valid, module,"
                    " abstain, targets_json, invalidation) VALUES"
                    " (?,0,?,1,'trend',0,?,'BTC below 200d')",
                    (rid, f"2026-09-{10 + i:02d}T04:30:00Z", targets))
        jdb.execute("INSERT INTO decision_grades(run_id, graded_at, review_week,"
                    " process_grade, process_rubric_json, outcome_grade, grader_model,"
                    " grader_run_id) VALUES (?,?,?,?,'{}','better','m','r')",
                    (rid, "x", WEEK, grade))
    jdb.commit()
    rr = ReviewRun(cfg, jdb, kdb, root=root, now=NOW,
                   session_runner=session_ok(root=root, jdb=jdb))
    assert rr.refresh_fewshot() is True
    fs = json.loads((root / cfg.paths.fewshot).read_text())
    kinds = [e["kind"] for e in fs["examples"]]
    grades = [e["grade"] for e in fs["examples"]]
    assert kinds.count("best") == 2 and kinds.count("worst") == 2
    assert set(grades) == {30, 45, 80, 95}
    assert rr.refresh_fewshot() is False  # same month: no second refresh


REVIEW_SKILLS = ("post-mortem", "strategy-lab", "tca", "risk-gate", "ops-runbook")


@pytest.mark.parametrize("name", REVIEW_SKILLS)
def test_review_skill_lint(name):
    text = (REPO_ROOT / ".claude" / "skills" / name / "SKILL.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m
    fm = yaml.safe_load(m.group(1))
    assert fm["name"] == name
    assert len(m.group(2).splitlines()) < 500
    tests_dir = REPO_ROOT / ".claude" / "skills" / name / "tests"
    assert tests_dir.exists() and any(tests_dir.rglob("test_*.py"))
    if name == "ops-runbook":
        assert fm.get("disable-model-invocation") is True
