"""The 30-day decision replay harness (spec §8): re-run a candidate prompt/skill/
params/model over the last N decision days with the inputs AS THEY WERE (from the
snapshot store), score with evals/metrics, run each snapshot twice for the
determinism check, and persist a replay_runs row + evals/results/<id>.json.

A change ships only when the replay passes; with fewer than min_snapshots the
result is passed=0 reason=insufficient-snapshots — the change is then HELD, never
auto-merged (correct degradation for the first weeks).

The decide callable is injectable (tests use fakes; live = runs.decision_core.decide
with the model recorded in each snapshot — a replay on a different model compares
nothing, spec §3a — except candidate kind="model").
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path

from evals import metrics
from evals import snapshot as snapshotlib
from ops.config import REPO_ROOT, load_config
from runs import build_prompt
from schemas.proposal import ProposalInvalid, json_schema, validate_proposal


@dataclass(frozen=True)
class CandidateRef:
    kind: str            # prompt | skill | params | model
    ref: str             # prompt version / git ref / params sha / model id


@dataclass
class ReplayResult:
    replay_id: str
    passed: bool
    reason: str
    snapshots_used: int
    scores: metrics.Scores | None
    baseline_scores: metrics.Scores | None
    comparison: dict[str, str]
    cost_usd: float
    #: Per-snapshot attempts of each arm, kept so evals/verify_change.py can recompute the
    #: counterfactual instead of trusting the number a model wrote down.
    baseline_results: list[dict] = dataclass_field(default_factory=list)
    candidate_results: list[dict] = dataclass_field(default_factory=list)
    counterfactual: dict = dataclass_field(default_factory=dict)


def _decide_live(prompt: str, model: str, cwd: Path, output_schema: dict,
                 skills: list[str] | None = None):
    """One replay decision.

    With ``skills`` the call goes through ``run_stage`` with ``Skill`` allowed and the
    working directory set to the candidate worktree — that is the only way a *skill*
    candidate can differ from its baseline (spec §10, HIGH issue 18). Without skills it is
    the read-only ``decide`` path, unchanged.
    """
    from runs import decision_core

    if skills:
        res = decision_core.run_stage(
            prompt, model=model, max_turns=12, max_usd=2.0, cwd=cwd,
            allowed_tools=[*decision_core.READ_ONLY_TOOLS, "Skill"],
            extra_disallowed=["Write", "Bash"], output_schema=output_schema,
            skills=list(skills))
    else:
        res = decision_core.decide(prompt, model=model, cwd=cwd,
                                   output_schema=output_schema)
    return res.text, (res.meta.cost_usd or 0.0)


def _call_decide(decide_fn, prompt: str, model: str, cwd: Path, schema: dict,
                 skills: list[str] | None):
    """Call an injected decide function, passing ``skills`` only if it accepts them."""
    if not skills:
        return decide_fn(prompt, model, cwd, schema)
    try:
        return decide_fn(prompt, model, cwd, schema, skills)
    except TypeError:
        return decide_fn(prompt, model, cwd, schema)


def _run_over_snapshots(snap_dirs, prompt_for, model_for, decide_fn,
                        runs_per_snapshot: int, budget_usd: float,
                        cwd: Path | None = None, skills: list[str] | None = None
                        ) -> tuple[list[dict], dict, dict, dict, float]:
    results, limits_by, states, flags = [], {}, {}, {}
    spent = 0.0
    schema = json_schema()
    for d in snap_dirs:
        snap = snapshotlib.read_snapshot(d)
        limits_by[snap.run_id] = snap.limits
        states[snap.run_id] = snap.inputs["state"]
        flags[snap.run_id] = snap.inputs["flags"]
        prompt = prompt_for(snap)
        attempts = []
        for _ in range(runs_per_snapshot):
            if spent >= budget_usd:
                attempts.append(None)
                continue
            if cwd is not None:
                text, cost = _call_decide(decide_fn, prompt, model_for(snap), Path(cwd),
                                          schema, skills)
            else:
                with tempfile.TemporaryDirectory() as sandbox:
                    text, cost = _call_decide(decide_fn, prompt, model_for(snap),
                                              Path(sandbox), schema, skills)
            spent += cost
            try:
                attempts.append(validate_proposal(text).model_dump() if text else None)
            except ProposalInvalid:
                attempts.append(None)
        results.append({"run_id": snap.run_id, "attempts": attempts})
    return results, limits_by, states, flags, spent


def _rules_targets_for(snap_dirs) -> dict[str, dict[str, float]]:
    """Rules-sleeve targets per snapshot from the stored state (agreement metric)."""
    from strategies.sleeve_common import AssetIndicators, compute_rules_targets

    cfg = load_config()
    out: dict[str, dict[str, float]] = {}
    for d in snap_dirs:
        snap = snapshotlib.read_snapshot(d)
        try:
            state = json.loads(snap.inputs["state"])
            latest = {
                asset: AssetIndicators(
                    close=s.get("close", 0.0), sma_ma=s.get("ma200", 0.0),
                    regime_up=s.get("trend") == "up",
                    rvol_annual=s.get("rvol_20d", 0.0))
                for asset, s in state.get("assets", {}).items()
            }
            out[snap.run_id] = compute_rules_targets(
                latest, cfg.sleeve_a.base_weights,
                {a: cfg.risk.max_weight.get(a, cfg.risk.max_weight["default"])
                 for a in cfg.universe.assets},
                cfg.sleeve_a.vol.target_annual)
        except (json.JSONDecodeError, KeyError):
            out[snap.run_id] = {}
    return out


def replay(candidate: CandidateRef, baseline: CandidateRef, *, days: int = 30,
           runs_per_snapshot: int = 2, budget_usd: float = 8.0,
           jdb: sqlite3.Connection, decide_fn=None, root: Path | None = None,
           now: datetime | None = None, candidate_cwd: Path | None = None,
           skills: list[str] | None = None) -> ReplayResult:
    """Score a candidate against its baseline over the stored snapshots.

    ``candidate_cwd`` is the **candidate worktree**: the candidate arm runs with that
    working directory (and ``skills`` loaded), so a skill or prompt edit that lives only in
    the worktree actually reaches the model. The baseline arm always runs in a throwaway
    sandbox against the live checkout, so the two arms differ by exactly the change.
    """
    cfg = load_config()
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)
    decide_fn = decide_fn or _decide_live
    replay_id = f"rp-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    snap_dirs = snapshotlib.list_snapshots(days, now=now, root=root)

    def persist(result: ReplayResult) -> ReplayResult:
        jdb.execute(
            "INSERT INTO replay_runs(replay_id, started_at, finished_at,"
            " candidate_kind, candidate_ref, baseline_ref, model, days,"
            " snapshots_used, cost_usd, scores_json, passed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (result.replay_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             candidate.kind, candidate.ref, baseline.ref,
             candidate.ref if candidate.kind == "model" else "per-snapshot",
             days, result.snapshots_used, result.cost_usd,
             json.dumps({
                 "reason": result.reason,
                 "scores": result.scores.as_dict() if result.scores else None,
                 "baseline": result.baseline_scores.as_dict() if result.baseline_scores else None,
                 "comparison": result.comparison}),
             int(result.passed)))
        jdb.commit()
        out = root / "evals" / "results" / f"{result.replay_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "replay_id": result.replay_id, "candidate": vars(candidate),
            "baseline": vars(baseline), "passed": result.passed,
            "reason": result.reason, "snapshots": result.snapshots_used,
            "scores": result.scores.as_dict() if result.scores else None},
            indent=2) + "\n")
        return result

    min_snaps = cfg.review.replay.min_snapshots
    if len(snap_dirs) < min_snaps:
        return persist(ReplayResult(replay_id, False,
                                    f"insufficient-snapshots ({len(snap_dirs)}/{min_snaps})",
                                    len(snap_dirs), None, None, {}, 0.0))

    def prompt_baseline(snap):
        bp = build_prompt.build_from_snapshot(snap.path)
        assert bp.text == snap.rendered_prompt, \
            f"builder drift: rebuilt prompt != stored for {snap.run_id}"
        return bp.text

    if candidate.kind == "prompt":
        def prompt_candidate(snap):
            return build_prompt.build_from_snapshot(snap.path,
                                                    prompt_version=candidate.ref).text
    else:
        prompt_candidate = prompt_baseline  # skill/params/model changes reuse the prompt

    def model_baseline(snap):
        return snap.meta.model

    model_candidate = ((lambda snap: candidate.ref) if candidate.kind == "model"
                       else model_baseline)

    tol = cfg.review.replay.target_tolerance
    rules = _rules_targets_for(snap_dirs)
    half = budget_usd / 2
    b_res, b_lim, b_states, b_flags, b_cost = _run_over_snapshots(
        snap_dirs, prompt_baseline, model_baseline, decide_fn, runs_per_snapshot, half)
    c_res, c_lim, c_states, c_flags, c_cost = _run_over_snapshots(
        snap_dirs, prompt_candidate, model_candidate, decide_fn, runs_per_snapshot, half,
        cwd=candidate_cwd, skills=skills)
    b_scores = metrics.score_replay(b_res, limits_by_snapshot=b_lim, states=b_states,
                                    flags=b_flags, rules_targets=rules, tolerance=tol)
    c_scores = metrics.score_replay(c_res, limits_by_snapshot=c_lim, states=c_states,
                                    flags=c_flags, rules_targets=rules, tolerance=tol)
    comparison = metrics.compare(b_scores, c_scores)
    passed, reason = metrics.ship_rule(
        c_scores, comparison, cfg.review.replay.determinism_min_identical)
    cf = metrics.counterfactual(now.strftime("%G-W%V"), b_res, c_res, b_scores, c_scores,
                                tolerance=tol)
    return persist(ReplayResult(replay_id, passed, reason, len(snap_dirs),
                                c_scores, b_scores, comparison, b_cost + c_cost,
                                baseline_results=b_res, candidate_results=c_res,
                                counterfactual=cf))


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ops import db

    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, help="kind:ref, e.g. prompt:research.v2")
    ap.add_argument("--baseline", required=True, help="kind:ref, e.g. prompt:research.v1")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--budget-usd", type=float, default=8.0)
    args = ap.parse_args(argv)
    cfg = load_config()
    ck, cr = args.candidate.split(":", 1)
    bk, br = args.baseline.split(":", 1)
    with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb:
        res = replay(CandidateRef(ck, cr), CandidateRef(bk, br), days=args.days,
                     budget_usd=args.budget_usd, jdb=jdb)
    print(f"{res.replay_id}: {'PASS' if res.passed else 'FAIL'} ({res.reason})")
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
