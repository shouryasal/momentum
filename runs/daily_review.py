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
  - auto-revert watch: for every change merged inside autonomy.auto_revert.window_days,
    the proposal validity rate and gate-breach count before vs after. Past the
    thresholds this writes an `auto_revert_requested` change_event — and stops.
    apply_changes (never this job) executes the revert, so the live HEAD keeps
    exactly two movers: apply_changes and the human.
  - reports/daily/<day>.md gets the deterministic appendix

Like the Sunday review, the session runs in its own git WORKTREE off git.live_branch
with EARN_STATE_ROOT pointing at the live data root: the live checkout never changes
branch and is never reset (spec §11 issue 6).

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
from ops.lib import locks, paths, tg
from runs import apply_changes, decision_core, profit_gaps, router, worktree
from runs import trace as tracelib
from runs.common import guard_env, gulf_now, utc_iso
from runs.review_run import (
    DAILY_POSTFLIGHT_RESERVE_S,
    run_session_in,
    session_deadline_s,
)

LINK_RE = re.compile(r"https?://[^\s)\]>\"']+")

#: Explicit script paths instead of ``Bash(python3 *)`` / ``Bash(bash *)`` (spec §10).
#: One list, shared with the weekly review — see
#: ``runs.decision_core.AUTOMATED_BASH_ALLOWLIST`` for why ``Bash(pytest ...)`` is not
#: in it any more.
DAILY_BASH_ALLOWLIST = list(decision_core.AUTOMATED_BASH_ALLOWLIST)


class DailyReview:
    def __init__(self, cfg: EarnConfig, jdb, kdb, *, root: Path | None = None,
                 state_root: Path | None = None,
                 now: datetime | None = None, session_runner=None, alert=None,
                 git_runner=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT          # the LIVE checkout
        #: the data/state root — where the KILL file is, via ``paths.state_root()``.
        self.state_root = Path(state_root) if state_root is not None else paths.state_root()
        self.now = now or datetime.now(UTC)
        self.day = (gulf_now(self.now) - timedelta(days=1)).strftime("%Y-%m-%d")
        self.run_id = f"daily-{self.day}"
        self.session_runner = session_runner or decision_core.run_stage
        self.alert = alert or (lambda text, sev="info": tg.send(text, sev, conn=kdb))
        self.git = git_runner or self._git
        self.wt: worktree.Worktree | None = None

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True)

    @property
    def cwd(self) -> Path:
        """Where the session works: the worktree when there is one, else the live root."""
        return self.wt.path if self.wt is not None else self.root

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

        self.wt = worktree.create(self.cfg, "daily", self.day, live_root=self.root)
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

    def prompt_path(self) -> Path:
        v2 = self.root / "prompts" / "daily_review.v2.md"
        return v2 if v2.exists() else self.root / "prompts" / "daily_review.v1.md"

    def _session_prompt(self, model: str, run_ids: list[str]) -> str:
        template = self.prompt_path().read_text(encoding="utf-8")
        listed = "\n".join(f"- {r}" for r in run_ids) or "- (none — all graded)"
        return (template.replace("{{DAY}}", self.day)
                .replace("{{MODEL}}", model)
                .replace("{{RUN_ID}}", self.run_id)
                .replace("{{RUN_IDS}}", listed)
                .replace("{{WORKTREE}}", str(self.cwd))
                .replace("{{BRANCH}}", self.wt.branch if self.wt else "")
                .replace("{{STATE_ROOT}}", str(self.root)))

    def skills_for_session(self) -> list[str]:
        return apply_changes.effective_bindings(self.cfg, self.root, "daily_review")

    def run_session(self, model: str, run_ids: list[str]):
        effort = router.resolve(
            "review", models_cfg=router.load_models_cfg(
                self.root / "config" / "models.yaml")).effort
        env = self.wt.env() if self.wt is not None else {}
        return run_session_in(
            self.cwd, session_runner=self.session_runner,
            prompt=self._session_prompt(model, run_ids), model=model, env=env,
            max_turns=self.cfg.daily_review.max_turns, effort=effort,
            max_usd=self.cfg.daily_review.max_budget_usd,
            allowed_tools=["Read", "Grep", "Glob", "Write", "Edit", "Skill",
                           *DAILY_BASH_ALLOWLIST],
            skills=self.skills_for_session(),
            deadline_s=session_deadline_s(self.cfg, "daily_review",
                                          DAILY_POSTFLIGHT_RESERVE_S))

    def _session_outputs_ok(self, run_ids: list[str]) -> bool:
        """Did the session produce BOTH halves — the report and at least one grade?"""
        return self._report_ok() and self._grades_ok(run_ids)

    def _report_ok(self) -> bool:
        """The narrative half. This is the half a rerun cannot improve on."""
        return self.report_path().exists()

    def _grades_ok(self, run_ids: list[str]) -> bool:
        """The grading half, written by the ``post-mortem`` skill, not by this module.

        Nothing in ``runs/`` writes ``decision_grades`` —
        ``.claude/skills/post-mortem/scripts/write_grades.py`` does, from inside the session.
        So a session that runs out of turns before it reaches the skill leaves a real report
        and no grades, and that used to read as an outright failure.
        """
        if not run_ids:
            return True
        return any(self.jdb.execute("SELECT 1 FROM decision_grades WHERE run_id=?",
                                    (rid,)).fetchone() for rid in run_ids)

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

    # ------------------------------------------------------- auto-revert watch

    def _validity_and_breaches(self, start: str, end: str) -> tuple[float | None, int]:
        row = self.jdb.execute(
            "SELECT COUNT(*) AS n, AVG(valid) AS v FROM proposals"
            " WHERE shadow=0 AND ts_utc >= ? AND ts_utc < ?", (start, end)).fetchone()
        validity = (float(row["v"]) * 100.0) if row and row["n"] and row["v"] is not None \
            else None
        b = self.jdb.execute(
            "SELECT COUNT(*) AS n FROM gate_decisions"
            " WHERE severity='breach' AND ts_utc >= ? AND ts_utc < ?",
            (start, end)).fetchone()
        return validity, int(b["n"] if b else 0)

    def auto_revert_candidates(self) -> list[tuple[str, str, dict]]:
        """Changes merged inside the window that made the numbers worse.

        Compares the window *before* the merge with the window *after* it: proposal
        validity rate (percentage points) and gate breaches. Returns the evidence; writing
        the request and executing it are separate steps on purpose.
        """
        ar = self.cfg.autonomy.auto_revert
        if not ar.enabled:
            return []
        window = timedelta(days=int(ar.window_days))
        cutoff = (self.now - window).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.jdb.execute(
            "SELECT change_id, decided_at FROM change_log"
            " WHERE status IN ('auto_merged','approved') AND merge_commit IS NOT NULL"
            " AND decided_at >= ? ORDER BY decided_at", (cutoff,)).fetchall()
        out: list[tuple[str, str, dict]] = []
        for r in rows:
            merged_at = str(r["decided_at"])
            try:
                t = datetime.strptime(merged_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            except ValueError:
                continue
            before_start = (t - window).strftime("%Y-%m-%dT%H:%M:%SZ")
            now_iso = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
            v_before, b_before = self._validity_and_breaches(before_start, merged_at)
            v_after, b_after = self._validity_and_breaches(merged_at, now_iso)
            reasons = []
            if v_before is not None and v_after is not None and \
                    (v_before - v_after) >= float(ar.validity_drop_pct):
                reasons.append(f"validity {v_before:.0f}% -> {v_after:.0f}%"
                               f" (drop >= {ar.validity_drop_pct}pp)")
            if (b_after - b_before) >= int(ar.breach_increase):
                reasons.append(f"gate breaches {b_before} -> {b_after}"
                               f" (+{b_after - b_before})")
            if reasons:
                out.append((r["change_id"], "; ".join(reasons),
                            {"validity_before": v_before, "validity_after": v_after,
                             "breaches_before": b_before, "breaches_after": b_after,
                             "merged_at": merged_at}))
        return out

    def request_auto_reverts(self) -> list[tuple[str, str]]:
        """Write the ``auto_revert_requested`` events. This job never reverts anything."""
        requested: list[tuple[str, str]] = []
        for change_id, reason, evidence in self.auto_revert_candidates():
            already = self.jdb.execute(
                "SELECT 1 FROM change_events WHERE change_id=?"
                " AND event IN ('auto_revert_requested','reverted')",
                (change_id,)).fetchone()
            if already:
                continue
            apply_changes.record_event(
                self.jdb, change_id, "auto_revert_requested", "system:daily_review",
                note=reason, now=self.now)
            self.alert(f"AUTO-REVERT REQUESTED for change {change_id}: {reason}"
                       f" — apply_changes will execute it ({json.dumps(evidence)})",
                       "critical")
            requested.append((change_id, reason))
        return requested

    def merge_tier0(self) -> list[str]:
        if self.wt is None:
            return []
        return apply_changes.merge_tier0(self.wt.branch, self.cfg, self.root)

    def prune_worktrees(self) -> list[str]:
        try:
            if self.wt is not None:
                worktree.remove(self.wt)
            return worktree.prune(self.cfg, live_root=self.root, now=self.now)
        except Exception as exc:  # noqa: BLE001 - pruning must never fail the run
            self.alert(f"worktree pruning failed: {exc}", "warn")
            return []

    def postflight(self, model_used: str, meta, run_ids: list[str],
                   session_status: str) -> None:
        wrong = self.wrong_decisions()
        bad_links = self.citation_lint()
        n_sources = self.update_source_reliability()
        tier0 = self.merge_tier0()
        results = apply_changes.apply_all(
            self.cfg, self.jdb, self.root, self.now, alert=self.alert,
            worktree=self.cwd if self.wt is not None else None,
            skills=self.skills_for_session())
        requested = self.request_auto_reverts()
        self.prune_worktrees()
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
        for cid, reason in requested:
            lines.append(f"- AUTO-REVERT REQUESTED {cid}: {reason}")
        if tier0:
            lines.append(f"- tier-0 commits merged onto the live branch: {len(tier0)}")
        # The profit & gap ledger, once a night. appendix_line never raises: a ledger
        # failure is one line in the appendix, never a failed review.
        lines.append(profit_gaps.appendix_line(self.cfg, self.jdb, self.kdb,
                                               self.state_root, now=self.now))
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
        if killlib.is_engaged(self.cfg, self.state_root):
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
        # Two halves, two different remedies. A missing REPORT means the session produced
        # nothing and the fallback model is worth paying for. Missing GRADES with a report in
        # hand is a different animal: the narrative — the expensive part — is already written,
        # the ungraded run_ids come back on tomorrow's `ungraded_run_ids()` anyway, and
        # rerunning the whole session on the dearer fallback buys one more attempt at the half
        # that already failed on turns. Measured 2026-10-01: three runs were journaled `failed`
        # and rerun for $10.71 while each had written a real report and a grading pack.
        if not res.ok or not self._report_ok():
            self.alert(f"daily review session on {model} produced no report"
                       f" ({res.meta.error}); rerunning on"
                       f" {self.cfg.daily_review.fallback_model} — its changes"
                       f" will be HELD", "warn")
            if self.wt is not None:
                worktree.reset(self.wt)   # the WORKTREE, never the live checkout
            model = self.cfg.daily_review.fallback_model
            res = self.run_session(model, run_ids)
        ungraded = [rid for rid in run_ids
                    if not self.jdb.execute("SELECT 1 FROM decision_grades WHERE run_id=?",
                                            (rid,)).fetchone()]
        if res.ok and self._report_ok() and ungraded:
            # `runs.status` has no 'partial' (journal.sql:40), so the gap rides in `error`
            # where the console and the healthcheck can both see it, and it is alerted.
            self.alert(f"daily review {self.day}: report written but"
                       f" {len(ungraded)} of {len(run_ids)} run_id(s) ungraded"
                       f" — tomorrow's run retries them", "warn")
        status = "success" if (res.ok and self._report_ok()) else "failed"
        if status == "success" and ungraded:
            res.meta.error = (f"ungraded:{len(ungraded)}/{len(run_ids)}"
                              + (f" ({res.meta.error})" if res.meta.error else ""))
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
