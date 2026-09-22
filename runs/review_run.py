"""The Sunday review run (20:00 Gulf): deterministic preflight -> one Agent SDK
session on claude-fable-5-1 (explicit opus rerun on failure — never a silent
fallback; fallback-authored changes are HELD by apply_changes) -> deterministic
postflight (per-model quality table, shadow verdict, fewshot refresh, apply_changes,
journal row, Telegram summary).

Recurring-cause escalation: a root cause recurring 3 consecutive ISO weeks goes to
the human with the evidence — a loop that keeps diagnosing the same thing is
itself the fault (spec §7).

Shadow promotion: when the 30-day shadow window closes, the deterministic
postflight computes agreement/validity/process-grade stats and, if the shadow model
qualifies, writes the recommendation into the report and alerts the human —
config/models.yaml is tier-2, so the promotion itself is a human edit (recorded
deviation from "promotion is a tier-1 change": the evidence pipeline is automated,
the final apply is human).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import locks, tg
from runs import apply_changes, decision_core, router
from runs.common import guard_env, utc_iso

LESSONS_TOOL = ".claude/skills/post-mortem/scripts/lessons_tool.py"
GRADE_INPUTS = ".claude/skills/post-mortem/scripts/grade_inputs.py"
OUTCOME_STATS = ".claude/skills/post-mortem/scripts/outcome_stats.py"


def iso_week(now: datetime) -> str:
    return now.strftime("%G-W%V")


def prev_weeks(week: str, n: int) -> list[str]:
    y, w = int(week[:4]), int(week[6:])
    d = datetime.fromisocalendar(y, w, 1)
    return [(d - timedelta(weeks=i)).strftime("%G-W%V") for i in range(n)]


class ReviewRun:
    def __init__(self, cfg: EarnConfig, jdb, kdb, *, root: Path | None = None,
                 now: datetime | None = None, session_runner=None, alert=None,
                 git_runner=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT
        self.now = now or datetime.now(UTC)
        self.week = iso_week(self.now)
        self.run_id = f"review-{self.week}"
        self.session_runner = session_runner or decision_core.run_stage
        self.alert = alert or (lambda text, sev="info": tg.send(text, sev, conn=kdb))
        self.git = git_runner or self._git

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True)

    # ------------------------------------------------------------- preflight

    def preflight(self) -> None:
        # archive stale lessons (code-side, spec §7 180-day rule)
        sys.path.insert(0, str(self.root / ".claude" / "skills" / "post-mortem" / "scripts"))
        import lessons_tool

        moved = lessons_tool.archive_stale(
            self.root / "lessons.md", self.root / "lessons-archive.md",
            self.cfg.review.lessons_reconfirm_days, self.now)
        if moved:
            self.alert(f"archived stale lessons: {moved}")
        # branch
        self.git("checkout", "-B", f"review/{self.week}")
        # grading packs (PYTHONPATH: the scripts resolve the ops package from the
        # code repo even when self.root is a test sandbox)
        import os

        outdir = self.root / "reports" / "weekly" / self.week
        outdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        for script, out in ((GRADE_INPUTS, "inputs.json"), (OUTCOME_STATS, "outcomes.json")):
            subprocess.run([sys.executable, str(self.root / script),
                            "--out", str(outdir / out)], cwd=self.root, check=False,
                           env=env)
        self.check_recurring_causes()

    def check_recurring_causes(self) -> list[str]:
        weeks = prev_weeks(self.week, self.cfg.review.recurring_cause_weeks)
        rows = self.jdb.execute(
            "SELECT recurrence_key, COUNT(DISTINCT review_week) AS w,"
            " GROUP_CONCAT(event_id) AS evidence FROM root_cause_events"
            " WHERE review_week IN ({}) AND escalated = 0"
            " GROUP BY recurrence_key HAVING w >= ?".format(
                ",".join("?" * len(weeks))),
            (*weeks, self.cfg.review.recurring_cause_weeks)).fetchall()
        escalated = []
        for r in rows:
            self.alert(f"RECURRING ROOT CAUSE {r['recurrence_key']} for"
                       f" {self.cfg.review.recurring_cause_weeks} straight weeks"
                       f" (events: {r['evidence']}) — the loop keeps diagnosing the"
                       f" same thing; human attention needed", "critical")
            self.jdb.execute("UPDATE root_cause_events SET escalated=1"
                             " WHERE recurrence_key=?", (r["recurrence_key"],))
            escalated.append(r["recurrence_key"])
        self.jdb.commit()
        return escalated

    # ------------------------------------------------------------- session

    def _session_prompt(self, model: str) -> str:
        template = (self.root / "prompts" / "review.v1.md").read_text()
        return (template.replace("{{WEEK}}", self.week)
                .replace("{{MODEL}}", model)
                .replace("{{RUN_ID}}", self.run_id))

    def run_session(self, model: str):
        effort = router.resolve("review",
                                models_cfg=router.load_models_cfg(
                                    self.root / "config" / "models.yaml")).effort
        return self.session_runner(
            self._session_prompt(model), model=model,
            max_turns=self.cfg.review.max_turns, effort=effort,
            max_usd=self.cfg.review.max_budget_usd, cwd=self.root,
            allowed_tools=["Read", "Grep", "Glob", "Write", "Edit", "Skill",
                           "Bash(python3 *)", "Bash(pytest *)", "Bash(git add *)",
                           "Bash(git commit *)", "Bash(docker compose *)",
                           "Bash(bash *)"],
            skills=["post-mortem", "strategy-lab", "tca", "risk-gate"],
            deadline_s=5400)

    def _session_outputs_ok(self) -> bool:
        report = self.root / "reports" / f"review-{self.week}.md"
        if not report.exists():
            return False
        graded = self.jdb.execute(
            "SELECT COUNT(*) AS n FROM decision_grades WHERE review_week=?",
            (self.week,)).fetchone()["n"]
        decisions = self.jdb.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE shadow=0 AND ts_utc >= ?",
            ((self.now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),)
        ).fetchone()["n"]
        return graded > 0 or decisions == 0

    # ------------------------------------------------------------- postflight

    def per_model_table(self) -> str:
        """Spec §3a: decisions, validity rate, process grade and cost per model."""
        rows = self.jdb.execute(
            "SELECT p.model, COUNT(*) AS n, AVG(p.valid) * 100 AS validity,"
            " AVG(g.process_grade) AS grade,"
            " (SELECT COALESCE(SUM(r.cost_usd),0) FROM runs r"
            "   WHERE r.requested_model = p.model) AS cost"
            " FROM proposals p LEFT JOIN decision_grades g ON g.run_id = p.run_id"
            " WHERE p.model IS NOT NULL GROUP BY p.model").fetchall()
        lines = ["## Per-model quality", "",
                 "| model | decisions | validity % | avg process grade | cost $ |",
                 "|---|---|---|---|---|"]
        for r in rows:
            grade = f"{r['grade']:.0f}" if r["grade"] is not None else "—"
            lines.append(f"| {r['model']} | {r['n']} | {r['validity']:.0f} |"
                         f" {grade} | {r['cost']:.2f} |")
        return "\n".join(lines) + "\n"

    def grade_shadow(self) -> dict | None:
        mc = router.load_models_cfg(self.root / "config" / "models.yaml")
        sh = mc.get("shadow", {})
        if not sh.get("enabled") or not sh.get("started"):
            return None
        tol = self.cfg.review.replay.target_tolerance
        pairs = self.jdb.execute(
            "SELECT p.run_id, p.targets_json AS primary_t, p.module AS primary_m,"
            " s.targets_json AS shadow_t, s.module AS shadow_m, s.valid AS shadow_valid"
            " FROM proposals p JOIN proposals s ON s.run_id = p.run_id AND s.shadow=1"
            " WHERE p.shadow=0 AND p.valid=1").fetchall()
        if not pairs:
            return None
        agree = 0
        valid = 0
        for r in pairs:
            valid += bool(r["shadow_valid"])
            if r["shadow_valid"] and r["primary_t"] and r["shadow_t"]:
                pt, st = json.loads(r["primary_t"]), json.loads(r["shadow_t"])
                same = (r["primary_m"] == r["shadow_m"] and all(
                    abs(pt.get(k, 0) - st.get(k, 0)) <= tol for k in set(pt) | set(st)))
                agree += same
        n = len(pairs)
        started = datetime.fromisoformat(str(sh["started"])).replace(tzinfo=UTC)
        window_done = (self.now - started).days >= int(sh.get("days", 30))
        stats = {"model": mc["models"].get(sh["model"], sh["model"]), "n": n,
                 "validity_rate": valid / n, "agreement_rate": agree / n,
                 "window_done": window_done}
        if window_done and stats["validity_rate"] >= 0.95 and stats["agreement_rate"] >= 0.8:
            stats["recommendation"] = "PROMOTE"
            self.alert(f"shadow window complete: {stats['model']} qualifies for"
                       f" promotion (validity {stats['validity_rate']:.0%}, agreement"
                       f" {stats['agreement_rate']:.0%}). models.yaml is tier-2 —"
                       f" apply by hand, then disable shadow.", "warn")
        else:
            stats["recommendation"] = "continue" if not window_done else "DO NOT PROMOTE"
        return stats

    def refresh_fewshot(self) -> bool:
        """First review of the calendar month: 2 best + 2 worst graded decisions
        with resolved outcomes -> prompts/examples/fewshot.json."""
        fs_path = self.root / self.cfg.paths.fewshot
        if fs_path.exists():
            try:
                cur = json.loads(fs_path.read_text())
                refreshed = datetime.fromisoformat(
                    cur["refreshed_at"].replace("Z", "+00:00"))
                if refreshed.strftime("%Y-%m") == self.now.strftime("%Y-%m"):
                    return False
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
        rows = self.jdb.execute(
            "SELECT g.run_id, g.process_grade, g.outcome_grade, p.targets_json,"
            " p.module, p.abstain, p.rationale_json, p.invalidation"
            " FROM decision_grades g JOIN proposals p ON p.run_id=g.run_id AND p.shadow=0"
            " WHERE g.outcome_grade IS NOT NULL ORDER BY g.process_grade").fetchall()
        if len(rows) < 4:
            return False
        picks = [("worst", r) for r in rows[:self.cfg.review.fewshot.worst]] + \
                [("best", r) for r in rows[-self.cfg.review.fewshot.best:]]
        examples = []
        for kind, r in picks:
            from evals import snapshot as snapshotlib

            excerpt = ""
            try:
                snap = snapshotlib.read_snapshot(r["run_id"], root=self.root)
                excerpt = snap.inputs["state"][:300]
            except Exception:
                pass
            examples.append({
                "run_id": r["run_id"], "grade": r["process_grade"], "kind": kind,
                "snapshot_excerpt": excerpt,
                "proposal": {"module": r["module"], "abstain": bool(r["abstain"]),
                             "targets": json.loads(r["targets_json"] or "{}"),
                             "invalidation": r["invalidation"]},
                "outcome_note": f"outcome {r['outcome_grade']}, process {r['process_grade']}",
            })
        fs_path.parent.mkdir(parents=True, exist_ok=True)
        fs_path.write_text(json.dumps(
            {"refreshed_at": utc_iso(self.now), "examples": examples}, indent=2) + "\n")
        return True

    def postflight(self, model_used: str, meta) -> None:
        report = self.root / "reports" / f"review-{self.week}.md"
        extra = [self.per_model_table()]
        shadow = self.grade_shadow()
        if shadow:
            extra.append(f"\n## Shadow model\n\n```json\n{json.dumps(shadow, indent=2)}\n```\n")
        if report.exists():
            report.write_text(report.read_text().rstrip("\n") + "\n\n" + "\n".join(extra))
        else:
            report.write_text(f"# Review {self.week} (session produced no report)\n\n"
                              + "\n".join(extra))
        self.refresh_fewshot()
        results = apply_changes.apply_all(self.cfg, self.jdb, self.root, self.now,
                                          alert=self.alert)
        # lint lessons after the session touched them
        sys.path.insert(0, str(self.root / ".claude" / "skills" / "post-mortem" / "scripts"))
        import lessons_tool

        problems = lessons_tool.lint((self.root / "lessons.md").read_text()
                                     if (self.root / "lessons.md").exists() else "")
        if problems:
            self.alert(f"lessons.md lint problems after review: {problems}", "warn")
        self.jdb.execute(
            "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc, finished_utc,"
            " requested_model, served_model, cost_usd, num_turns, effort,"
            " auth_source, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, "review", "review", utc_iso(self.now), utc_iso(),
             model_used, getattr(meta, "served_model", None),
             getattr(meta, "cost_usd", None), getattr(meta, "num_turns", None),
             getattr(meta, "applied_effort", None) or "max",
             getattr(meta, "auth_source", None),
             "success" if self._session_outputs_ok() else "failed",
             getattr(meta, "error", None)))
        self.jdb.commit()
        summary = "; ".join(f"{cid}:{status}" for cid, status, _ in results) or "no changes"
        self.alert(f"review {self.week} done on {model_used}: {summary}")

    # ------------------------------------------------------------- flow

    def main_flow(self) -> int:
        from ops.lib import kill as killlib

        if killlib.is_engaged(self.cfg, self.root):
            self.jdb.execute(
                "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc, status)"
                " VALUES (?,?,?,?, 'killed')",
                (self.run_id, "review", "review", utc_iso(self.now)))
            self.jdb.commit()
            return 0
        self.preflight()
        model = self.cfg.review.model
        res = self.run_session(model)
        if not (res.ok and self._session_outputs_ok()):
            # explicit rerun on the fallback model — branch reset, provenance kept
            self.alert(f"review session on {model} failed"
                       f" ({res.meta.error}); rerunning on"
                       f" {self.cfg.review.fallback_model} — its changes will be HELD",
                       "warn")
            self.git("reset", "--hard")
            self.git("checkout", "-B", f"review/{self.week}")
            model = self.cfg.review.fallback_model
            res = self.run_session(model)
        self.postflight(model, res.meta)
        return 0 if res.ok else 1


def main() -> int:
    guard_env()
    cfg = load_config()
    with locks.acquire("review"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return ReviewRun(cfg, jdb, kdb).main_flow()


if __name__ == "__main__":
    sys.exit(main())
