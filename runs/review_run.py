"""The Sunday review run (20:00 Gulf), v2 — deterministic preflight → one Agent SDK session
**in a git worktree** → deterministic postflight.

The HIGH bug (spec §11 issue 6): v1 ran ``git checkout -B review/<week>`` and, on a failed
session, ``git reset --hard`` **in the live checkout**. A running system's working tree moved
underneath two freqtrade containers, and ``apply_changes`` then cherry-picked a commit that
was already on HEAD — so a passing change was recorded "rejected".

v2 never touches the live checkout. ``runs.worktree.create()`` gives the session its own
checkout of ``git.live_branch`` with the live data symlinked in and ``EARN_STATE_ROOT``
pointing at the live root, so the session reads the real journal while editing tier-1 files
somewhere safe. Its Bash allowlist names explicit script paths instead of ``python3 *``.
Only ``runs.apply_changes`` — running in the live checkout under the ops lock — may move the
live branch afterwards.

Shadow promotion no longer writes ``config/models-auto.yaml`` directly: a qualifying window
emits a ``kind: model`` change through the gate (which always holds those for a human), and
a "DO NOT PROMOTE" verdict closes the window so auto-shadow is never stuck.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import locks, paths, tg
from runs import apply_changes, decision_core, router, worktree
from runs.common import guard_env, utc_iso

LESSONS_TOOL = ".claude/skills/post-mortem/scripts/lessons_tool.py"
GRADE_INPUTS = ".claude/skills/post-mortem/scripts/grade_inputs.py"
OUTCOME_STATS = ".claude/skills/post-mortem/scripts/outcome_stats.py"

#: Explicit script paths replace ``Bash(python3 *)``, ``Bash(bash *)`` and
#: ``Bash(docker compose *)`` (spec §10 hook hardening). Anything broader is a way out.
REVIEW_BASH_ALLOWLIST = [
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(git diff *)",
    "Bash(git status *)",
    "Bash(git log *)",
    "Bash(git rev-parse *)",
    "Bash(python3 .claude/skills/post-mortem/scripts/*)",
    "Bash(python3 .claude/skills/strategy-lab/scripts/*)",
    "Bash(python3 .claude/skills/skill-smith/scripts/*)",
    "Bash(python3 .claude/skills/tca/scripts/*)",
    "Bash(python3 .claude/skills/risk-gate/scripts/*)",
    "Bash(python3 .claude/skills/asset-dossier/scripts/*)",
    "Bash(python3 -m evals.replay *)",
    "Bash(python3 -m evals.skill_lint *)",
    "Bash(python3 -m evals.skill_eval *)",
    "Bash(pytest .claude/skills/*)",
]

#: Wall clock the session may use: the cron budget minus the postflight reserve.
REVIEW_POSTFLIGHT_RESERVE_S = 900
DAILY_POSTFLIGHT_RESERVE_S = 600


def iso_week(now: datetime) -> str:
    return now.strftime("%G-W%V")


def prev_weeks(week: str, n: int) -> list[str]:
    y, w = int(week[:4]), int(week[6:])
    d = datetime.fromisocalendar(y, w, 1)
    return [(d - timedelta(weeks=i)).strftime("%G-W%V") for i in range(n)]


def session_deadline_s(cfg: EarnConfig, job: str, reserve_s: int) -> int:
    """Job deadline minus the postflight reserve, floored so it stays positive."""
    sched = (cfg.ops.schedules or {}).get(job)
    budget = int(getattr(sched, "deadline_s", 0) or 0)
    return max(300, budget - reserve_s)


def run_session_in(worktree_path: Path, *, session_runner, prompt: str, model: str,
                   env: dict[str, str], **kwargs):
    """Call the session runner, passing ``env`` only when it accepts one (P3 adds it)."""
    try:
        return session_runner(prompt, model=model, cwd=worktree_path, env=env, **kwargs)
    except TypeError:
        return session_runner(prompt, model=model, cwd=worktree_path, **kwargs)


class ReviewRun:
    def __init__(self, cfg: EarnConfig, jdb, kdb, *, root: Path | None = None,
                 state_root: Path | None = None,
                 now: datetime | None = None, session_runner=None, alert=None,
                 git_runner=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT          # the LIVE checkout; never checked out of
        #: the data/state root — where the KILL file is, via ``paths.state_root()``.
        self.state_root = Path(state_root) if state_root is not None else paths.state_root()
        self.now = now or datetime.now(UTC)
        self.week = iso_week(self.now)
        self.run_id = f"review-{self.week}"
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
        self.wt = worktree.create(self.cfg, "review", self.week, live_root=self.root)
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

    def prompt_path(self) -> Path:
        v2 = self.root / "prompts" / "review.v2.md"
        return v2 if v2.exists() else self.root / "prompts" / "review.v1.md"

    def _session_prompt(self, model: str) -> str:
        template = self.prompt_path().read_text(encoding="utf-8")
        wt_path = str(self.cwd)
        return (template.replace("{{WEEK}}", self.week)
                .replace("{{MODEL}}", model)
                .replace("{{RUN_ID}}", self.run_id)
                .replace("{{WORKTREE}}", wt_path)
                .replace("{{BRANCH}}", self.wt.branch if self.wt else "")
                .replace("{{STATE_ROOT}}", str(self.root)))

    def skills_for_session(self) -> list[str]:
        return apply_changes.effective_bindings(self.cfg, self.root, "review")

    def run_session(self, model: str):
        effort = router.resolve("review",
                                models_cfg=router.load_models_cfg(
                                    self.root / "config" / "models.yaml")).effort
        env = self.wt.env() if self.wt is not None else {}
        return run_session_in(
            self.cwd, session_runner=self.session_runner,
            prompt=self._session_prompt(model), model=model, env=env,
            max_turns=self.cfg.review.max_turns, effort=effort,
            max_usd=self.cfg.review.max_budget_usd,
            allowed_tools=["Read", "Grep", "Glob", "Write", "Edit", "Skill",
                           *REVIEW_BASH_ALLOWLIST],
            skills=self.skills_for_session(),
            deadline_s=session_deadline_s(self.cfg, "review_run",
                                          REVIEW_POSTFLIGHT_RESERVE_S))

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
        from evals import metrics as metricslib
        from evals import snapshot as snapshotlib

        agree = 0
        valid = 0
        limit_violations = 0
        for r in pairs:
            valid += bool(r["shadow_valid"])
            if r["shadow_valid"] and r["primary_t"] and r["shadow_t"]:
                pt, st = json.loads(r["primary_t"]), json.loads(r["shadow_t"])
                same = (r["primary_m"] == r["shadow_m"] and all(
                    abs(pt.get(k, 0) - st.get(k, 0)) <= tol for k in set(pt) | set(st)))
                agree += same
                # zero-limit-violations gate against the paired snapshot's limits
                try:
                    snap = snapshotlib.read_snapshot(r["run_id"], root=self.root)
                except snapshotlib.SnapshotError:
                    continue  # no snapshot -> no evidence either way
                if metricslib.violates_limits(st, snap.limits):
                    limit_violations += 1
        n = len(pairs)
        started = datetime.fromisoformat(str(sh["started"])).replace(tzinfo=UTC)
        window_done = (self.now - started).days >= int(sh.get("days", 30))
        stats = {"model": mc["models"].get(sh["model"], sh["model"]), "n": n,
                 "validity_rate": valid / n, "agreement_rate": agree / n,
                 "limit_violations": limit_violations, "window_done": window_done}
        if (window_done and stats["validity_rate"] >= 0.95
                and stats["agreement_rate"] >= 0.8 and limit_violations == 0):
            stats["recommendation"] = "PROMOTE"
            self._propose_promotion(mc, sh, stats)
        elif window_done:
            stats["recommendation"] = "DO NOT PROMOTE"
            self._close_shadow_window(sh, stats)
        else:
            stats["recommendation"] = "continue"
        return stats

    def _propose_promotion(self, mc: dict, sh: dict, stats: dict) -> None:
        """Route the promotion through the change gate instead of writing the overlay.

        The overlay edit is made **in the worktree** and committed there, so the change has
        a real commit the gate can inspect. ``apply_changes`` always holds ``kind: model``
        for a human apply, so a promotion now arrives as a reviewable change with its
        evidence attached rather than as a silent overlay edit nobody asked for.
        """
        key = sh["model"]
        model_id = mc["models"].get(key, key)
        change_id = f"{self.now.strftime('%Y-%m-%d')}-promote-{key}".lower()
        if self.wt is None:
            self.alert(f"shadow window clean for {model_id} but there is no worktree to"
                       f" author the promotion in — nothing proposed", "warn")
            return

        # `tasks.decide.chain`, not `tasks.decide.model`: the v2 overlay whitelist
        # (ops.models_config.apply_overlay) accepts only a chain whose tail matches the
        # base, so the promotion has to carry the base chain's tail with it. Writing
        # `model` made `load_models_cfg()` raise for every consumer that reads through
        # the v2 loader — the console and runs/llm/chain.py — the moment a window closed.
        base = self._models_base()
        try:
            tail = list(base.task("decide").chain[1:]) if base is not None else []
        except Exception:  # noqa: BLE001 - a proposal, not a critical path
            tail = []
        # Declare the alias only when the human file has never heard of it (the usual
        # case: auto-shadow invented it). Re-declaring one models.yaml already owns is
        # exactly what the overlay whitelist refuses, and rightly so.
        needs_declaring = base is None or base.models.get(key) is None

        from runs.maintenance import config_tier

        # Normally a no-op: `start_shadow` already declared this alias in the overlay and
        # `setdefault` leaves its tier alone. The value is the floor for the case where
        # the declaration is somehow gone.
        declaration = {"provider": "claude", "id": model_id,
                       "tier": config_tier(model_id) or 4}

        def mutate(cur: dict) -> dict:
            if needs_declaring:
                cur.setdefault("models", {}).setdefault(key, declaration)
            cur.setdefault("tasks", {}).setdefault("decide", {})["chain"] = [key, *tail]
            cur["shadow"] = {"enabled": False, "model": None, "started": None}
            return cur

        router.write_models_overlay(
            mutate, overlay_path=self.wt.path / "config" / "models-auto.yaml")
        wt_git = subprocess.run(
            ["git", "add", "config/models-auto.yaml"], cwd=self.wt.path,
            capture_output=True, text=True)
        if wt_git.returncode == 0:
            subprocess.run(["git", "commit", "-m",
                            f"propose: decide model -> {model_id} after a clean shadow"
                            f" window"],
                           cwd=self.wt.path, capture_output=True, text=True)
        commit = (subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt.path,
                                 capture_output=True, text=True).stdout or "").strip()
        change = {
            "id": change_id,
            "created_at": utc_iso(self.now),
            "author_run_id": self.run_id,
            "author_model": "code:grade_shadow",
            "prompt_version": "code",
            "tier": 1,
            "kind": "model",
            "target": "config/models-auto.yaml",
            "what": {"summary": f"promote decide model to {model_id}", "commit": commit,
                     "op": "edit"},
            "why": (f"shadow window closed clean: validity {stats['validity_rate']:.0%},"
                    f" agreement {stats['agreement_rate']:.0%}, 0 limit violations over"
                    f" n={stats['n']} paired proposals"),
            "branch": self.wt.branch,
            "worktree": str(self.wt.path),
            "status": "proposed",
        }
        changes_dir = self.root / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / f"{change_id}.json").write_text(
            json.dumps(change, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plog = self.root / "knowledge" / "promotions.jsonl"
        plog.parent.mkdir(parents=True, exist_ok=True)
        with plog.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"proposed_at": utc_iso(self.now), "model": model_id,
                                "change_id": change_id, "evidence": stats}) + "\n")
        apply_changes.record_event(self.jdb, change_id, "proposed", "system:grade_shadow",
                                   note=f"promote decide -> {model_id}", now=self.now)
        self.alert(f"shadow window clean for {model_id}: change {change_id} proposed —"
                   f" model promotions always need your apply", "warn")

    def _models_base(self):  # noqa: ANN201 - ModelsConfig | None
        """``config/models.yaml`` with **no** overlay — the human's own declarations.

        A promotion has to know two things the merged view cannot tell it apart: the tail
        of ``decide``'s chain (the overlay may replace only ``chain[0]``, so the rest has
        to be re-stated verbatim) and whether the shadow alias is one the human already
        declared (in which case the overlay must not re-declare it).
        """
        try:
            from ops.models_config import load_models_cfg

            return load_models_cfg(self.root / "config" / "models.yaml", overlay=None)
        except Exception:  # noqa: BLE001 - a promotion is a proposal, not a critical path
            return None

    def _close_shadow_window(self, sh: dict, stats: dict) -> None:
        """A negative verdict closes the window, so auto-shadow can start a new one."""
        def mutate(cur: dict) -> dict:
            cur["shadow"] = {"enabled": False, "model": None, "started": None}
            return cur

        router.write_models_overlay(
            mutate, overlay_path=self.root / "config" / "models-auto.yaml")
        self.alert(f"shadow window closed without promotion"
                   f" ({stats['validity_rate']:.0%} valid,"
                   f" {stats['agreement_rate']:.0%} agreement,"
                   f" {stats['limit_violations']} limit violations)", "warn")

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
        report.parent.mkdir(parents=True, exist_ok=True)
        if report.exists():
            report.write_text(report.read_text().rstrip("\n") + "\n\n" + "\n".join(extra))
        else:
            report.write_text(f"# Review {self.week} (session produced no report)\n\n"
                              + "\n".join(extra))
        self.refresh_fewshot()
        tier0 = self.merge_tier0()
        results = apply_changes.apply_all(
            self.cfg, self.jdb, self.root, self.now, alert=self.alert,
            worktree=self.cwd if self.wt is not None else None,
            skills=self.skills_for_session())
        reverted = apply_changes.run_auto_reverts(self.cfg, self.jdb, self.root,
                                                  now=self.now, alert=self.alert)
        self.prune_worktrees()
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
        if tier0:
            summary += f"; tier0 merged {len(tier0)}"
        if reverted:
            summary += f"; auto-reverted {len(reverted)}"
        self.alert(f"review {self.week} done on {model_used}: {summary}")

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

    # ------------------------------------------------------------- flow

    def main_flow(self) -> int:
        from ops.lib import kill as killlib

        if killlib.is_engaged(self.cfg, self.state_root):
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
            # explicit rerun on the fallback model — the WORKTREE is reset, never the live
            # checkout; provenance is kept because the branch name does not change.
            self.alert(f"review session on {model} failed"
                       f" ({res.meta.error}); rerunning on"
                       f" {self.cfg.review.fallback_model} — its changes will be HELD",
                       "warn")
            if self.wt is not None:
                worktree.reset(self.wt)
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
