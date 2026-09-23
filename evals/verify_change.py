"""Recompute the evidence for a tier-1 change — never trust what the model wrote (spec §10).

HIGH issue 17 was that ``apply_changes`` gated on ``changes/<id>.json``: the very file the
session authored. A model that wanted a change merged only had to write flattering numbers.

From v2 the claimed numbers are stored (``change_log.claimed_evidence_json``) and displayed
beside the verified ones, but **only what this module recomputes can gate anything**:

===========  =============================================================================
kind         recomputed here
===========  =============================================================================
all          the commit touches nothing but the declared target; no tier-2 path;
             ``author_model`` equals ``runs.served_model`` for ``author_run_id``; a
             ``kind: skill`` commit that also rewrites its own ``tests/**`` or
             ``evals/**`` is **held** — evidence and the thing judged cannot share an
             author
params       bounds and max-step recomputed from ``git show <commit>:<target>`` against the
             live file; a costed backtest over ``change_gates.backtest_min_years``;
             walk-forward candidate vs baseline out-of-sample delta
prompt/skill ``evals/replay.py`` with **cwd = the candidate worktree** and the stage's bound
             skills allowed, so the arms actually differ; the counterfactual comes from the
             replay output; scanner/validator prompts also get ``evals/signal_replay.py``
skill.*      ``evals/skill_lint.py`` + the skill's own pytest + ``evals/skill_eval.py``
             pass rate >= ``skills.eval.min_pass_rate``
===========  =============================================================================

Every expensive step goes through :class:`Runners`, so tests inject fakes and nothing here
starts docker or talks to a model by accident. A runner that is not available yields a
``hold`` — an unverifiable change waits for a human, it never merges.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig

sys.path.insert(0, str(REPO_ROOT / ".claude" / "hooks"))
from tier2_paths import is_tier2  # noqa: E402

#: A claimed number more than this far from the verified one is flagged red in the UI and
#: written as a ``root_cause_events`` row with ``recurrence_key = 'evidence_mismatch'``.
MISMATCH_PCT = 0.10

PASS, FAIL, HOLD, SKIP = "pass", "fail", "hold", "skip"


@dataclass(frozen=True)
class Check:
    name: str
    verdict: str          # pass | fail | hold | skip
    detail: str = ""
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail,
                "data": self.data}


@dataclass
class VerifyResult:
    verdict: str                       # pass | reject | hold
    reason: str
    checks: list[Check] = field(default_factory=list)
    verified: dict = field(default_factory=dict)
    mismatches: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason,
                "checks": [c.as_dict() for c in self.checks],
                "verified": self.verified, "mismatches": self.mismatches}


# --------------------------------------------------------------------------- runners


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def show_file(root: Path, commit: str, rel: str) -> str | None:
    r = _git(root, "show", f"{commit}:{rel}")
    return r.stdout if r.returncode == 0 else None


def commit_files(root: Path, commit: str) -> list[str]:
    r = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line]


def commit_exists(root: Path, commit: str) -> bool:
    return _git(root, "cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def _docker_backtest(*, cwd: Path, timerange: str, strategy: str, fee: float) -> dict:
    """The real costed backtest: freqtrade in docker, from ``cwd`` (live root or worktree).

    The candidate arm runs in the candidate worktree, whose ``config/params-sleeve-*.json``
    already carries the proposed values — that is what makes the arms differ.
    """
    from runs import walk_forward as wf

    subprocess.run(
        ["docker", "compose", "run", "--rm", f"freqtrade-{strategy[-1].lower()}",
         "backtesting", "--strategy", strategy,
         "--config", f"/freqtrade/earn-config/freqtrade-{strategy[-1].lower()}.json",
         "--timerange", timerange, "--fee", str(fee), "--enable-protections",
         "--export", "trades",
         "--backtest-directory", "/freqtrade/user_data/backtest_results"],
        cwd=cwd / "ops", check=True, capture_output=True, text=True)
    z = wf.newest_result_zip(cwd / "ft_userdata" / strategy[-1].lower() / "backtest_results")
    if z is None:
        raise RuntimeError("no freqtrade backtest result produced")
    return wf.parse_backtest_zip(z)


def _walk_forward_report(*, cwd: Path) -> dict:
    """Read the walk-forward artefact the driver wrote, per arm."""
    p = cwd / "reports" / "backtests" / "walkforward.json"
    if not p.exists():
        raise FileNotFoundError(f"{p} missing — run runs/walk_forward.py first")
    return json.loads(p.read_text(encoding="utf-8"))


@dataclass
class Runners:
    """Every expensive recomputation, injectable.

    ``backtest(arm, cwd, timerange, fee)`` returns freqtrade metrics;
    ``walk_forward(arm, cwd)`` returns ``{"windows": [...]}``;
    ``replay(change, candidate_cwd, skills)`` returns an ``evals.replay.ReplayResult``;
    ``signal_replay(change, candidate_cwd)`` returns ``{"precision", "recall", "n"}``;
    ``skill_lint(skill_dir)``/``skill_tests(skill_dir, cwd)``/``skill_eval(skill_dir, cwd)``
    wrap :mod:`evals.skill_lint` and :mod:`evals.skill_eval`.
    """

    backtest: Callable[..., dict] | None = None
    walk_forward: Callable[..., dict] | None = None
    replay: Callable[..., Any] | None = None
    signal_replay: Callable[..., dict] | None = None
    skill_lint: Callable[..., Any] | None = None
    skill_tests: Callable[..., tuple[bool, str]] | None = None
    skill_eval: Callable[..., Any] | None = None

    @classmethod
    def default(cls, *, live_root: Path | None = None, cfg: EarnConfig | None = None
                ) -> Runners:
        """The production runners.

        The skill runners execute **model-authored** code, so they carry a
        :class:`evals.skill_eval.Sandbox` decided here, once, from
        ``security.agent_user`` / ``security.agent_cli_wrapper``. With neither configured
        the sandbox refuses and both runners return a refusal, which
        :func:`check_skill` turns into a hold — the gate never runs a candidate's own
        tests with the owner's privileges in order to decide whether to trust it.
        """
        from evals import skill_eval as skill_eval_mod
        from evals import skill_lint as skill_lint_mod

        root = Path(live_root) if live_root is not None else REPO_ROOT
        sandbox = skill_eval_mod.sandbox_for(cfg, root=root)

        def backtest(arm: str, cwd: Path, timerange: str, fee: float, strategy: str) -> dict:
            return _docker_backtest(cwd=cwd, timerange=timerange, strategy=strategy, fee=fee)

        def walk_forward(arm: str, cwd: Path) -> dict:
            return _walk_forward_report(cwd=cwd)

        def replay_runner(change: dict, candidate_cwd: Path | None, skills: list[str],
                          *, jdb, cfg) -> Any:
            from evals.replay import CandidateRef
            from evals.replay import replay as run_replay

            kind = "prompt" if change["kind"] == "prompt" else "skill"
            return run_replay(
                CandidateRef(kind, change["target"]),
                CandidateRef(kind, "live"),
                days=cfg.review.replay.days,
                runs_per_snapshot=cfg.review.replay.runs_per_snapshot,
                budget_usd=cfg.review.replay.budget_usd, jdb=jdb,
                candidate_cwd=candidate_cwd, skills=skills)

        def signal_replay(change: dict, candidate_cwd: Path | None) -> dict:
            from evals import signal_replay as sr  # noqa: PLC0415 - P4 owns this module

            return sr.score_prompt(change["target"], candidate_cwd=candidate_cwd)

        def skill_lint(skill_dir: Path, existing: set[str]) -> Any:
            return skill_lint_mod.lint_skill(skill_dir, existing_names=existing)

        def skill_tests(skill_dir: Path, cwd: Path, timeout_s: int) -> tuple[bool, str]:
            return skill_eval_mod.run_tests(skill_dir, cwd=cwd, timeout_s=timeout_s,
                                            repo_root=cwd, sandbox=sandbox)

        def skill_eval(skill_dir: Path, cwd: Path, timeout_s: int) -> Any:
            # trusted_root is the LIVE checkout: the fixtures that score the candidate
            # live in evals/ (tier 2), where no session can have written them.
            return skill_eval_mod.evaluate(skill_dir, cwd=cwd, timeout_s=timeout_s,
                                           repo_root=cwd, sandbox=sandbox,
                                           trusted_root=root)

        return cls(backtest=backtest, walk_forward=walk_forward, replay=replay_runner,
                   signal_replay=signal_replay, skill_lint=skill_lint,
                   skill_tests=skill_tests, skill_eval=skill_eval)


# --------------------------------------------------------------------------- helpers


def change_op(change: dict) -> str:
    return str((change.get("what") or {}).get("op") or "edit")


#: Fields that identify a list entry, best first. A claimed list and a recomputed list
#: are the same evidence in a different order, so the comparator keys entries by their
#: identity rather than by position: ``bounds_check[sleeve_a.vol.target_annual].new``.
LIST_KEY_FIELDS = ("param", "signal_id", "id", "name", "key", "window", "field")


def _list_key(entry: Any, index: int) -> str:
    """The stable name of one list entry: its identity field, else its position."""
    if isinstance(entry, dict):
        for field in LIST_KEY_FIELDS:
            value = entry.get(field)
            if isinstance(value, str) and value:
                return value
    return str(index)


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Nested dict/list -> ``{"a.b.c": leaf, "a.list[key].c": leaf}``.

    Lists are descended into, not treated as leaves. ``find_mismatches`` only compares
    numeric leaves, so a list that stayed whole was a list compared against a list and
    silently skipped — and ``bounds_check`` is exactly that shape. A model could claim
    any ``old``/``new``/``max_step`` arithmetic it liked and no mismatch row was ever
    raised. The gate itself never trusted the claim (it reads only the recomputation),
    but "evidence is recomputed, never trusted" also means the divergence is *visible*.
    """
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        if not obj:
            out[prefix] = list(obj)
        for i, item in enumerate(obj):
            key = f"{prefix}[{_list_key(item, i)}]" if prefix else f"[{_list_key(item, i)}]"
            out.update(flatten(item, key))
    else:
        out[prefix] = obj
    return out


def bounds_key_for(cfg: EarnConfig, sleeve: str, dotted: str) -> str | None:
    """Map a params-file leaf to its ``bounds:`` key (``sleeve_a.vol.target_annual`` …)."""
    for candidate in (f"sleeve_{sleeve}.{dotted}", f"execution.{dotted}", dotted):
        if candidate in cfg.bounds:
            return candidate
    return None


def relative_delta(claimed: float, verified: float) -> float:
    denom = max(abs(verified), 1e-9)
    return abs(claimed - verified) / denom


def find_mismatches(claimed: dict, verified: dict, *, pct: float = MISMATCH_PCT
                    ) -> list[dict]:
    """Numeric leaves present in both that differ by more than ``pct`` (relative)."""
    c, v = flatten(claimed), flatten(verified)
    out: list[dict] = []
    for key, cv in c.items():
        vv = v.get(key)
        if not isinstance(cv, (int, float)) or not isinstance(vv, (int, float)):
            continue
        if isinstance(cv, bool) or isinstance(vv, bool):
            if cv != vv:
                out.append({"field": key, "claimed": cv, "verified": vv, "delta_pct": 100.0})
            continue
        d = relative_delta(float(cv), float(vv))
        if d > pct:
            out.append({"field": key, "claimed": cv, "verified": vv,
                        "delta_pct": round(d * 100, 2)})
    return out


# --------------------------------------------------------------------------- checks


#: Sub-paths of a skill that ARE the evidence: its pytest suite and its eval cases. A
#: commit that rewrites them is grading its own homework.
SELF_EVIDENCE_DIRS = ("tests", "evals")


def self_authored_evidence(target: str, files: list[str]) -> list[str]:
    """The evidence files a skill commit touched, if any.

    ``.claude/skills/<name>/tests/**`` and ``evals/cases.yaml`` are tier 1, so one commit
    could replace the skill body *and* replace its assertions with ``assert True`` — and
    ``skill_tests`` would dutifully report PASS. The suite's own fixture did exactly that.
    Evidence and the thing being judged cannot have the same author, so such a commit is
    held for a human rather than being scored.
    """
    base = str(target).rstrip("/")
    hits: list[str] = []
    for f in files:
        rel = f[len(base) + 1:] if f.startswith(base + "/") else f
        head = rel.split("/", 1)[0]
        if head in SELF_EVIDENCE_DIRS and "/" in rel:
            hits.append(f)
    return hits


def check_commit_scope(change: dict, live_root: Path) -> Check:
    """The commit must exist and touch nothing outside the declared target."""
    commit = (change.get("what") or {}).get("commit") or ""
    if not commit or not commit_exists(live_root, commit):
        return Check("commit_scope", FAIL, f"commit {commit or '(none)'} not in this repo")
    files = commit_files(live_root, commit)
    if not files:
        return Check("commit_scope", FAIL, "commit touches no files")
    tier2 = [f for f in files if is_tier2(f)]
    if tier2:
        return Check("commit_scope", FAIL, f"commit touches tier-2 paths: {tier2}",
                     {"files": files})
    target = str(change["target"]).rstrip("/")
    outside = [f for f in files if not (f == target or f.startswith(target + "/"))]
    if outside:
        return Check("commit_scope", FAIL,
                     f"commit touches paths outside the declared target: {outside}",
                     {"files": files, "target": target})
    if str(change.get("kind")) == "skill":
        own = self_authored_evidence(target, files)
        if own:
            return Check("commit_scope", HOLD,
                         f"the commit rewrites its own evidence ({own}) — a human decides"
                         " whether the new assertions are honest",
                         {"files": files, "target": target, "self_evidence": own})
    return Check("commit_scope", PASS, f"{len(files)} file(s) under {target}",
                 {"files": files})


def check_author_model(change: dict, jdb) -> Check:
    """``author_model`` must be the model the journal says actually served the run."""
    claimed_author = str(change.get("author_model") or "")
    if claimed_author.startswith("code:"):
        # A deterministic author (e.g. review_run's shadow grader) makes no model claim,
        # so there is nothing to compare against runs.served_model.
        return Check("author_model", PASS, claimed_author, {"served": claimed_author})
    run_id = change.get("author_run_id")
    if not run_id:
        return Check("author_model", HOLD, "no author_run_id on the change")
    row = jdb.execute(
        "SELECT requested_model, served_model FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return Check("author_model", HOLD, f"no journal row for author_run_id {run_id}")
    served = row["served_model"]
    claimed = change.get("author_model")
    if not served:
        return Check("author_model", HOLD, f"run {run_id} recorded no served_model")
    if served != claimed:
        return Check("author_model", HOLD,
                     f"change says {claimed}, journal says {run_id} was served by {served}",
                     {"claimed": claimed, "served": served})
    return Check("author_model", PASS, served, {"served": served})


def recompute_bounds(change: dict, cfg: EarnConfig, live_root: Path) -> Check:
    """Bounds and max-step recomputed from the commit, not from ``bounds_check``."""
    target = str(change["target"])
    commit = (change.get("what") or {}).get("commit") or ""
    candidate_raw = show_file(live_root, commit, target)
    if candidate_raw is None:
        return Check("bounds", FAIL, f"{target} not present in commit {commit[:8]}")
    live_path = live_root / target
    if live_path.exists():
        baseline_raw = live_path.read_text(encoding="utf-8")
    else:
        baseline_raw = show_file(live_root, f"{commit}^", target) or "{}"
    try:
        cand = json.loads(candidate_raw)
        base = json.loads(baseline_raw)
    except json.JSONDecodeError as exc:
        return Check("bounds", FAIL, f"{target} is not valid JSON: {exc}")

    sleeve = str(cand.get("sleeve") or base.get("sleeve") or "a").lower()
    cflat = flatten(cand.get("params") or {})
    bflat = flatten(base.get("params") or {})
    changed = [k for k in sorted(set(cflat) | set(bflat)) if cflat.get(k) != bflat.get(k)]
    if not changed:
        return Check("bounds", FAIL, "the commit changes no parameter value")
    if len(changed) > cfg.review.change_gates.max_param_changes_per_month:
        pass  # the monthly budget is a separate gate; size alone is not a rejection
    recomputed: list[dict] = []
    for dotted in changed:
        new, old = cflat.get(dotted), bflat.get(dotted)
        if not isinstance(new, (int, float)) or isinstance(new, bool):
            return Check("bounds", FAIL, f"{dotted} is not a numeric parameter")
        key = bounds_key_for(cfg, sleeve, dotted)
        if key is None:
            return Check("bounds", FAIL, f"no bounds defined for {dotted}")
        b = cfg.bounds[key]
        old_f = float(old) if isinstance(old, (int, float)) else float("nan")
        entry = {"param": key, "old": old_f, "new": float(new), "min": b.min,
                 "max": b.max, "max_step": b.max_step, "ok": True}
        if not (b.min <= float(new) <= b.max):
            entry["ok"] = False
            recomputed.append(entry)
            return Check("bounds", FAIL,
                         f"{key} {new} outside [{b.min},{b.max}]", {"bounds": recomputed})
        if abs(float(new) - old_f) > b.max_step + 1e-12:
            entry["ok"] = False
            recomputed.append(entry)
            return Check("bounds", FAIL,
                         f"{key} step {abs(float(new) - old_f):g} > {b.max_step}",
                         {"bounds": recomputed})
        recomputed.append(entry)
    return Check("bounds", PASS, f"{len(recomputed)} parameter(s) within bounds",
                 {"bounds": recomputed})


def check_backtest(change: dict, cfg: EarnConfig, live_root: Path,
                   worktree: Path | None, runners: Runners, costs: dict | None) -> Check:
    """Costed backtest over >= ``change_gates.backtest_min_years``, both arms."""
    if runners.backtest is None:
        return Check("backtest", HOLD, "no backtest runner available")
    if worktree is None:
        return Check("backtest", HOLD, "no candidate worktree to backtest")
    years = int(cfg.review.change_gates.backtest_min_years)
    end = datetime.now(UTC).date()
    start = end.replace(year=end.year - years)
    timerange = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    fee_bps = float((costs or {}).get("fee_bps", 0.0))
    slip_bps = float((costs or {}).get("slippage_bps", 0.0))
    fee = (fee_bps + slip_bps) / 10000
    sleeve = "a" if "sleeve-a" in str(change["target"]) else "b"
    strategy = f"Sleeve{sleeve.upper()}"
    try:
        baseline = runners.backtest("baseline", live_root, timerange, fee, strategy)
        candidate = runners.backtest("candidate", worktree, timerange, fee, strategy)
    except Exception as exc:  # noqa: BLE001 - an unrunnable backtest holds, never merges
        return Check("backtest", HOLD, f"backtest failed: {type(exc).__name__}: {exc}")
    data = {"timerange": timerange, "years": float(years), "fee_bps": fee_bps,
            "slippage_bps": slip_bps, "baseline": baseline, "candidate": candidate}
    b_dd = float(baseline.get("max_drawdown_pct") or 0.0)
    c_dd = float(candidate.get("max_drawdown_pct") or 0.0)
    b_ret = float(baseline.get("profit_total_pct") or 0.0)
    c_ret = float(candidate.get("profit_total_pct") or 0.0)
    data["return_delta_pct"] = round(c_ret - b_ret, 4)
    data["drawdown_delta_pct"] = round(c_dd - b_dd, 4)
    if c_ret < b_ret and c_dd > b_dd:
        return Check("backtest", FAIL,
                     f"candidate is worse on both axes (return {c_ret:g} < {b_ret:g},"
                     f" drawdown {c_dd:g} > {b_dd:g})", data)
    return Check("backtest", PASS,
                 f"return {c_ret:g}% vs {b_ret:g}%, drawdown {c_dd:g}% vs {b_dd:g}%", data)


def _oos_mean(report: dict) -> float:
    wins = report.get("windows") or []
    vals = [float(w.get("profit_total_pct") or 0.0) for w in wins]
    return sum(vals) / len(vals) if vals else 0.0


def check_walk_forward(change: dict, cfg: EarnConfig, live_root: Path,
                       worktree: Path | None, runners: Runners) -> Check:
    """Out-of-sample delta recomputed from both arms' walk-forward artefacts."""
    if runners.walk_forward is None:
        return Check("walk_forward", HOLD, "no walk-forward runner available")
    if worktree is None:
        return Check("walk_forward", HOLD, "no candidate worktree to walk forward")
    try:
        baseline = runners.walk_forward("baseline", live_root)
        candidate = runners.walk_forward("candidate", worktree)
    except Exception as exc:  # noqa: BLE001
        return Check("walk_forward", HOLD,
                     f"walk-forward failed: {type(exc).__name__}: {exc}")
    delta = round(_oos_mean(candidate) - _oos_mean(baseline), 4)
    windows = len(candidate.get("windows") or [])
    floor = cfg.review.change_gates.walk_forward_min_out_sample_delta
    data = {"windows": windows, "scheme": "expanding", "out_sample_delta": delta,
            "in_sample_delta": None, "pass": delta >= floor}
    if windows == 0:
        return Check("walk_forward", HOLD, "no walk-forward windows", data)
    if delta < floor:
        return Check("walk_forward", FAIL,
                     f"out-of-sample delta {delta} < {floor}", data)
    return Check("walk_forward", PASS, f"out-of-sample delta {delta} over {windows} windows",
                 data)


def check_replay(change: dict, cfg: EarnConfig, jdb, worktree: Path | None,
                 runners: Runners, skills: list[str]) -> tuple[Check, Check]:
    """Replay in the candidate worktree, and the counterfactual derived from its output."""
    if runners.replay is None:
        return (Check("replay", HOLD, "no replay runner available"),
                Check("counterfactual", SKIP, "replay did not run"))
    if worktree is None:
        return (Check("replay", HOLD, "no candidate worktree to replay in"),
                Check("counterfactual", SKIP, "replay did not run"))
    try:
        res = runners.replay(change, worktree, skills, jdb=jdb, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        return (Check("replay", HOLD, f"replay failed: {type(exc).__name__}: {exc}"),
                Check("counterfactual", SKIP, "replay did not run"))
    replay_id = getattr(res, "replay_id", None)
    passed = bool(getattr(res, "passed", False))
    scores = getattr(res, "scores", None)
    data = {"replay_id": replay_id, "pass": passed,
            "days": getattr(res, "snapshots_used", 0),
            "scores": scores.as_dict() if scores is not None else {},
            "baseline_scores": (getattr(res, "baseline_scores", None).as_dict()
                                if getattr(res, "baseline_scores", None) is not None else {})}
    if not passed:
        replay_check = Check("replay", HOLD,
                             f"replay did not pass: {getattr(res, 'reason', '')}", data)
    else:
        replay_check = Check("replay", PASS, str(getattr(res, "reason", "ok")), data)

    cf = dict(getattr(res, "counterfactual", None) or {})
    if not cf:
        return replay_check, Check("counterfactual", HOLD,
                                   "replay produced no counterfactual")
    if cf.get("decisions_changed", 0) == 0 and abs(cf.get("process_grade_delta", 0.0)) < 1e-9:
        return replay_check, Check(
            "counterfactual", FAIL,
            "counterfactual-null: the change alters neither a decision nor the process",
            cf)
    return replay_check, Check(
        "counterfactual", PASS,
        f"{cf['decisions_changed']} decision(s) changed,"
        f" process delta {cf['process_grade_delta']}", cf)


def check_signal_replay(change: dict, worktree: Path | None, runners: Runners) -> Check:
    """Scanner/validator prompt changes get precision/recall against resolved outcomes."""
    target = str(change["target"])
    if not any(tok in target for tok in ("scan", "validate")):
        return Check("signal_replay", SKIP, "not a scanner/validator prompt")
    if runners.signal_replay is None:
        return Check("signal_replay", SKIP, "no signal-replay runner available")
    try:
        data = runners.signal_replay(change, worktree)
    except ImportError:
        return Check("signal_replay", SKIP, "evals/signal_replay.py is not installed yet")
    except Exception as exc:  # noqa: BLE001
        return Check("signal_replay", HOLD,
                     f"signal replay failed: {type(exc).__name__}: {exc}")
    return Check("signal_replay", PASS,
                 f"precision {data.get('precision')}, recall {data.get('recall')}", data)


def check_skill(change: dict, cfg: EarnConfig, live_root: Path, worktree: Path | None,
                runners: Runners) -> list[Check]:
    """Lint + the skill's own tests + its eval pass rate, all in the candidate worktree."""
    root = worktree or live_root
    target = str(change["target"]).rstrip("/")
    skill_dir = root / target
    out: list[Check] = []
    if not skill_dir.is_dir():
        return [Check("skill_lint", FAIL, f"{target} is not a directory in the candidate")]
    existing = {p.name for p in skill_dir.parent.iterdir() if p.is_dir()}

    if runners.skill_lint is None:
        out.append(Check("skill_lint", HOLD, "no lint runner available"))
    else:
        res = runners.skill_lint(skill_dir, existing)
        findings = [f.as_dict() if hasattr(f, "as_dict") else dict(f)
                    for f in getattr(res, "findings", [])]
        if getattr(res, "ok", False):
            out.append(Check("skill_lint", PASS, "clean", {"findings": findings}))
        else:
            errs = [f for f in findings if f.get("severity") == "error"]
            out.append(Check("skill_lint", FAIL,
                             "; ".join(f"{f['code']}: {f['message']}" for f in errs[:3]),
                             {"findings": findings}))

    timeout_s = int(cfg.skills.eval.timeout_s)
    if runners.skill_tests is None:
        out.append(Check("skill_tests", HOLD, "no test runner available"))
    else:
        ok, log = runners.skill_tests(skill_dir, root, timeout_s)
        # "refused" is not "failed": the suite was never run, because running
        # model-authored pytest needs containment this host does not have. An
        # unverifiable change holds for a human; it must never reject (which would throw
        # a good change away) and must never pass.
        if not ok and str(log).startswith(skill_eval_refused()):
            out.append(Check("skill_tests", HOLD, str(log)))
        else:
            out.append(Check("skill_tests", PASS if ok else FAIL,
                             "pytest passed" if ok else f"pytest failed: {log[-400:]}"))

    if runners.skill_eval is None:
        out.append(Check("skill_eval", HOLD, "no eval runner available"))
    else:
        res = runners.skill_eval(skill_dir, root, timeout_s)
        rate = float(getattr(res, "pass_rate", 0.0))
        floor = float(cfg.skills.eval.min_pass_rate)
        data = res.as_dict() if hasattr(res, "as_dict") else {"pass_rate": rate}
        refused = str(getattr(res, "refused", "") or "")
        if refused:
            out.append(Check("skill_eval", HOLD, refused, data))
        else:
            out.append(Check("skill_eval", PASS if rate + 1e-9 >= floor else FAIL,
                             f"pass rate {rate:.0%} vs floor {floor:.0%}", data))
    return out


def skill_eval_refused() -> str:
    from evals.skill_eval import REFUSED

    return REFUSED


# --------------------------------------------------------------------------- entry point


def _worst(checks: list[Check]) -> tuple[str, str]:
    """A FAIL rejects, a HOLD holds, otherwise it passes. First offender wins the reason."""
    for c in checks:
        if c.verdict == FAIL:
            return "reject", f"{c.name}: {c.detail}"
    for c in checks:
        if c.verdict == HOLD:
            return "hold", f"{c.name}: {c.detail}"
    return "pass", "ok"


def claimed_evidence(change: dict) -> dict:
    """Everything the model asserted — stored, shown, and never used as a gate."""
    out = {}
    for key in ("bounds_check", "backtest", "walk_forward", "replay", "counterfactual",
                "skill_evidence", "signal_replay"):
        if key in change:
            out[key] = change[key]
    return out


def verify(change: dict, cfg: EarnConfig, jdb, *, live_root: Path,
           worktree: Path | None = None, runners: Runners | None = None,
           costs: dict | None = None, skills: list[str] | None = None,
           now: datetime | None = None) -> VerifyResult:
    """Recompute every piece of evidence this change's kind requires."""
    runners = runners or Runners.default(live_root=live_root, cfg=cfg)
    now = now or datetime.now(UTC)
    kind = str(change["kind"])
    op = change_op(change)
    checks: list[Check] = [check_commit_scope(change, live_root),
                           check_author_model(change, jdb)]
    verified: dict = {}

    if kind == "params":
        bounds = recompute_bounds(change, cfg, live_root)
        checks.append(bounds)
        if bounds.verdict == PASS:
            verified["bounds_check"] = bounds.data.get("bounds", [])
            bt = check_backtest(change, cfg, live_root, worktree, runners, costs)
            checks.append(bt)
            if bt.data:
                verified["backtest"] = bt.data
            wf = check_walk_forward(change, cfg, live_root, worktree, runners)
            checks.append(wf)
            if wf.data:
                verified["walk_forward"] = wf.data
    elif kind in ("prompt", "skill"):
        if kind == "skill" and op == "bind":
            checks.append(Check("bind", HOLD,
                                "binding a skill to a production task needs a human"))
        else:
            replay_check, cf_check = check_replay(change, cfg, jdb, worktree, runners,
                                                  skills or [])
            checks.extend([replay_check, cf_check])
            if replay_check.data:
                verified["replay"] = replay_check.data
            if cf_check.data:
                verified["counterfactual"] = cf_check.data
            if kind == "prompt":
                sr = check_signal_replay(change, worktree, runners)
                checks.append(sr)
                if sr.verdict == PASS and sr.data:
                    verified["signal_replay"] = sr.data
            else:
                skill_checks = check_skill(change, cfg, live_root, worktree, runners)
                checks.extend(skill_checks)
                verified["skill_evidence"] = {
                    "lint_ok": any(c.name == "skill_lint" and c.verdict == PASS
                                   for c in skill_checks),
                    "tests_ok": any(c.name == "skill_tests" and c.verdict == PASS
                                    for c in skill_checks),
                    "eval_pass_rate": next(
                        (float(c.data.get("pass_rate", 0.0)) for c in skill_checks
                         if c.name == "skill_eval"), 0.0),
                }
    elif kind == "model":
        checks.append(Check("model_promotion", HOLD,
                            "model promotion always requires a human apply"))

    verdict, reason = _worst(checks)
    mismatches = find_mismatches(claimed_evidence(change), verified)
    verified["verified_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return VerifyResult(verdict, reason, checks, verified, mismatches)
