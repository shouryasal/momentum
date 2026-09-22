"""The nightly learning loop (21:30 Gulf): grade the PREVIOUS Gulf day's
decisions, find the wrong ones and say why, catch hallucinated citations, and
update per-source news credibility — every day, not just Sunday.

Deterministic wrapper around one review-model session at max effort:

preflight   daily/<day> branch + the day's P&L-free grading pack
session     post-mortem skill grades ONLY run_ids the Sunday review has not
            already graded (write_grades.py applies the rubric weights in code)
postflight  (all code, no model)
  - wrong-decision rule: process_grade < daily_review.wrong_process_below OR
    (outcome_grade='worse' AND any rubric boolean false) -> a full trace report
    per wrong decision (runs/trace.py)
  - hallucinated-citation lint: every http link in the day's brief must exist in
    news_items.url; a miss is an incident + a root_cause_events row with
    recurrence_key 'hallucinated_citation' (the 3-week recurrence escalation
    applies) + an alert. Zero tolerance.
  - source_reliability: Laplace-smoothed per-source score from what the day's
    items did next (corroborated later? numeric claims verified?)
  - apply_changes over anything the session authored on the branch (same gates
    as Sunday; params cap ≤2/month holds, prompt/skill changes uncapped)
  - reports/daily/<day>.md gets the deterministic appendix

Idempotent on the report file; Sunday's deep pass (counterfactuals,
walk-forward, fewshot, shadow verdicts) stays in review_run.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import kill as killlib
from ops.lib import locks, tg
from runs import apply_changes, decision_core, router
from runs import trace as tracelib
from runs.common import guard_env, gulf_now, utc_iso

LINK_RE = re.compile(r"https?://[^\s)\]>\"']+")


class DailyReview:
    def __init__(self, cfg: EarnConfig, jdb, kdb, *, root: Path | None = None,
                 now: datetime | None = None, session_runner=None, alert=None,
                 git_runner=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT
        self.now = now or datetime.now(UTC)
        self.day = (gulf_now(self.now) - timedelta(days=1)).strftime("%Y-%m-%d")
        self.run_id = f"daily-{self.day}"
        self.session_runner = session_runner or decision_core.run_stage
        self.alert = alert or (lambda text, sev="info": tg.send(text, sev, conn=kdb))
        self.git = git_runner or self._git

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True)

    # ------------------------------------------------------------- day window

    def _window(self) -> tuple[str, str]:
        start = (datetime.strptime(self.day, "%Y-%m-%d").replace(tzinfo=UTC)
                 - timedelta(hours=4))
        return (start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def day_run_ids(self) -> list[str]:
        s, e = self._window()
        return [r["run_id"] for r in self.jdb.execute(
            "SELECT run_id FROM proposals WHERE shadow=0 AND ts_utc >= ?"
            " AND ts_utc < ? ORDER BY ts_utc", (s, e))]

    def ungraded_run_ids(self) -> list[str]:
        return [rid for rid in self.day_run_ids() if self.jdb.execute(
            "SELECT 1 FROM decision_grades WHERE run_id=?", (rid,)).fetchone()
            is None]

    def report_path(self) -> Path:
        return self.root / "reports" / "daily" / f"{self.day}.md"

    # ------------------------------------------------------------- preflight

    def preflight(self) -> None:
        import os

        self.git("checkout", "-B", f"daily/{self.day}")
        pack = self.root / "reports" / "daily" / "packs" / f"{self.day}-inputs.json"
        pack.parent.mkdir(parents=True, exist_ok=True)
        script = self.root / ".claude/skills/post-mortem/scripts/grade_inputs.py"
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        subprocess.run([sys.executable, str(script), "--day", self.day,
                        "--out", str(pack)], cwd=self.root, check=False, env=env)
        if gulf_now(self.now).day == 1:
            # monthly asset-intelligence refresh (deterministic scripts; the
            # session then rewrites the dossiers' narrative from the numbers)
            for name in ("asset_stats.py", "event_study.py"):
                subprocess.run(
                    [sys.executable, str(self.root / ".claude/skills/asset-dossier"
                                                     / "scripts" / name)],
                    cwd=self.root, check=False, env=env)

    # ------------------------------------------------------------- session

    def _session_prompt(self, model: str, run_ids: list[str]) -> str:
        template = (self.root / "prompts" / "daily_review.v1.md").read_text()
        listed = "\n".join(f"- {r}" for r in run_ids) or "- (none — all graded)"
        return (template.replace("{{DAY}}", self.day)
                .replace("{{MODEL}}", model)
                .replace("{{RUN_ID}}", self.run_id)
                .replace("{{RUN_IDS}}", listed))

    def run_session(self, model: str, run_ids: list[str]):
        effort = router.resolve(
            "review", models_cfg=router.load_models_cfg(
                self.root / "config" / "models.yaml")).effort
        return self.session_runner(
            self._session_prompt(model, run_ids), model=model,
            max_turns=self.cfg.daily_review.max_turns, effort=effort,
            max_usd=self.cfg.daily_review.max_budget_usd, cwd=self.root,
            allowed_tools=["Read", "Grep", "Glob", "Write", "Edit", "Skill",
                           "Bash(python3 *)", "Bash(pytest *)",
                           "Bash(git add *)", "Bash(git commit *)"],
            skills=["post-mortem", "strategy-lab", "asset-dossier"],
            deadline_s=2400)

    def _session_outputs_ok(self, run_ids: list[str]) -> bool:
        if not self.report_path().exists():
            return False
        if not run_ids:
            return True
        graded = sum(1 for rid in run_ids if self.jdb.execute(
            "SELECT 1 FROM decision_grades WHERE run_id=?", (rid,)).fetchone())
        return graded > 0

    # ------------------------------------------------------------- postflight

    def wrong_decisions(self) -> list[tuple[str, str, Path]]:
        out = []
        for rid in self.day_run_ids():
            g = self.jdb.execute("SELECT * FROM decision_grades WHERE run_id=?",
                                 (rid,)).fetchone()
            if g is None:
                continue
            why = None
            if g["process_grade"] < self.cfg.daily_review.wrong_process_below:
                why = f"process_grade {g['process_grade']} < " \
                      f"{self.cfg.daily_review.wrong_process_below}"
            elif g["outcome_grade"] == "worse":
                try:
                    rubric = json.loads(g["process_rubric_json"] or "{}")
                except json.JSONDecodeError:
                    rubric = {}
                failed = [k for k, v in rubric.items() if v is False]
                if failed:
                    why = f"outcome worse AND rubric failed: {','.join(failed)}"
            if why:
                path = tracelib.write_trace(self.cfg, self.jdb, rid, root=self.root)
                out.append((rid, why, path))
        return out

    def citation_lint(self) -> list[str]:
        """Every http link in the day's brief must exist in news_items.url —
        a link the model cannot source is a hallucination, zero tolerance."""
        brief = self.root / "knowledge" / "briefs" / f"{self.day}.md"
        if not brief.exists():
            return []
        bad = []
        for link in sorted(set(LINK_RE.findall(brief.read_text()))):
            if not self.kdb.execute("SELECT 1 FROM news_items WHERE url=?",
                                    (link,)).fetchone():
                bad.append(link)
        week = self.now.strftime("%G-W%V")
        for link in bad:
            import hashlib

            h = hashlib.sha256(link.encode()).hexdigest()[:8]
            self.jdb.execute(
                "INSERT INTO incidents(opened_utc, kind, severity, detail,"
                " root_cause) VALUES (?,?,?,?,?)",
                (utc_iso(self.now), "other", "warn",
                 f"hallucinated citation in brief {self.day}: {link}", "reasoning"))
            self.jdb.execute(
                "INSERT OR IGNORE INTO root_cause_events(event_id, review_week,"
                " kind, ref, cause, recurrence_key, fix_path, learn_eligible,"
                " eligibility_rule, evidence_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"halluc-{self.day}-{h}", week, "incident", self.day,
                 "reasoning", "hallucinated_citation", "prompts/crypto-brief",
                 1, "citation not in news_items.url", json.dumps({"link": link})))
        self.jdb.commit()
        if bad:
            self.alert(f"HALLUCINATED CITATION in brief {self.day}: {bad} — link"
                       f" not in the news archive", "critical")
        return bad

    def update_source_reliability(self) -> int:
        """Deterministic per-source credibility from what the day's items did
        next. Laplace-smoothed: score = (successes+1)/(trials+2) where a
        secondary item corroborated later or a verified numeric claim is a
        success; an item still uncorroborated or a falsified claim is a miss."""
        s, e = self._window()
        rows = self.kdb.execute(
            "SELECT source, source_class, corroborated, claim_verified"
            " FROM news_items WHERE fetched_at >= ? AND fetched_at < ?",
            (s, e)).fetchall()
        per: dict[str, dict[str, int]] = {}
        for r in rows:
            c = per.setdefault(r["source"], {"unconf": 0, "corr": 0, "falsified": 0,
                                             "checked": 0, "verified": 0})
            if r["source_class"] == "secondary":
                if r["corroborated"]:
                    c["corr"] += 1
                else:
                    c["unconf"] += 1
            if r["claim_verified"] is not None:
                c["checked"] += 1
                if r["claim_verified"]:
                    c["verified"] += 1
                else:
                    c["falsified"] += 1
        for source, c in per.items():
            cur = self.kdb.execute(
                "SELECT * FROM source_reliability WHERE source=?", (source,)
            ).fetchone()
            n_unconf = (cur["n_unconfirmed"] if cur else 0) + c["unconf"]
            n_corr = (cur["n_corroborated_later"] if cur else 0) + c["corr"]
            n_fals = (cur["n_falsified"] if cur else 0) + c["falsified"]
            n_check = (cur["n_claims_checked"] if cur else 0) + c["checked"]
            n_ver = (cur["n_claims_verified"] if cur else 0) + c["verified"]
            successes = n_corr + n_ver
            trials = n_corr + n_unconf + n_check
            score = (successes + 1) / (trials + 2)
            self.kdb.execute(
                "INSERT OR REPLACE INTO source_reliability(source, n_unconfirmed,"
                " n_corroborated_later, n_falsified, n_claims_checked,"
                " n_claims_verified, score, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (source, n_unconf, n_corr, n_fals, n_check, n_ver, round(score, 4),
                 utc_iso(self.now)))
        self.kdb.commit()
        return len(per)

    def postflight(self, model_used: str, meta, run_ids: list[str],
                   session_status: str) -> None:
        wrong = self.wrong_decisions()
        bad_links = self.citation_lint()
        n_sources = self.update_source_reliability()
        results = apply_changes.apply_all(self.cfg, self.jdb, self.root, self.now,
                                          alert=self.alert)
        lines = ["", "## Deterministic appendix (code)", "",
                 f"- graded this session: {len(run_ids)} run_id(s)"]
        for rid, why, path in wrong:
            lines.append(f"- WRONG DECISION {rid}: {why} — trace:"
                         f" `{path.relative_to(self.root)}`")
        if not wrong:
            lines.append("- wrong decisions: none")
        lines.append(f"- hallucinated citations: {bad_links or 'none'}")
        lines.append(f"- source reliability updated for {n_sources} source(s)")
        for cid, status, reason in results:
            lines.append(f"- change {cid}: {status} ({reason})")
        rp = self.report_path()
        rp.parent.mkdir(parents=True, exist_ok=True)
        base = rp.read_text().rstrip("\n") if rp.exists() else \
            f"# Daily review {self.day} (session produced no report)"
        rp.write_text(base + "\n" + "\n".join(lines) + "\n")
        self.jdb.execute(
            "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc,"
            " finished_utc, requested_model, served_model, cost_usd, num_turns,"
            " effort, auth_source, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, "daily_review", "review", utc_iso(self.now), utc_iso(),
             model_used, getattr(meta, "served_model", None),
             getattr(meta, "cost_usd", None), getattr(meta, "num_turns", None),
             getattr(meta, "applied_effort", None) or "max",
             getattr(meta, "auth_source", None), session_status,
             getattr(meta, "error", None)))
        self.jdb.commit()
        self.alert(f"daily review {self.day}: {len(run_ids)} graded,"
                   f" {len(wrong)} wrong, {len(bad_links)} bad citations")

    # ------------------------------------------------------------- flow

    def main_flow(self) -> int:
        if killlib.is_engaged(self.cfg, self.root):
            self.jdb.execute(
                "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc,"
                " status) VALUES (?,?,?,?, 'killed')",
                (self.run_id, "daily_review", "review", utc_iso(self.now)))
            self.jdb.commit()
            return 0
        if self.report_path().exists():
            return 0  # idempotent rerun
        self.preflight()
        run_ids = self.ungraded_run_ids()  # Sunday's grades are respected
        model = self.cfg.daily_review.model
        # the session runs even on a quiet day: the one-page narrative is wanted
        res = self.run_session(model, run_ids)
        if not (res.ok and self._session_outputs_ok(run_ids)):
            self.alert(f"daily review session on {model} failed"
                       f" ({res.meta.error}); rerunning on"
                       f" {self.cfg.daily_review.fallback_model} — its changes"
                       f" will be HELD", "warn")
            self.git("reset", "--hard")
            self.git("checkout", "-B", f"daily/{self.day}")
            model = self.cfg.daily_review.fallback_model
            res = self.run_session(model, run_ids)
        status = "success" if (res.ok and self._session_outputs_ok(run_ids)) \
            else "failed"
        self.postflight(model, res.meta, run_ids, status)
        return 0 if status == "success" else 1


def main() -> int:
    guard_env()
    cfg = load_config()
    with locks.acquire("daily_review"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return DailyReview(cfg, jdb, kdb).main_flow()


if __name__ == "__main__":
    sys.exit(main())
