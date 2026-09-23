"""Assemble + schema-validate a ``changes/<id>.json``.

TIER 2 (scripts are human-only). Computed fields come ONLY from script artefacts (backtest
results, walkforward.json, the ``replay_runs`` row, skill lint/eval output) — never from
model prose. Everything written here is a **claim**: ``evals/verify_change.py`` recomputes
all of it before ``runs/apply_changes.py`` consults the autonomy matrix, and a claim more
than 10% off the recomputed value is logged as an ``evidence_mismatch`` root cause.

The commit sha is passed in (``--commit``) rather than shelled out for: this script starts
no process, opens no socket and writes nothing outside ``changes/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import jsonschema  # noqa: E402
import yaml  # noqa: E402

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402

CHANGE_SCHEMA = json.loads((REPO_ROOT / "schemas" / "change.json").read_text())


def _json_arg(raw: str | None) -> dict | list | None:
    """A CLI argument that is either a path to JSON or inline JSON."""
    if not raw:
        return None
    p = Path(raw)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else json.loads(raw)


def build_change(args, cfg) -> dict:
    root = Path(args.worktree) if args.worktree else REPO_ROOT
    change: dict = {
        "id": args.id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author_run_id": args.author_run_id,
        "author_model": args.author_model,
        "prompt_version": args.prompt_version,
        "tier": 1,
        "kind": args.kind,
        "target": args.target,
        "what": {"summary": args.summary, "commit": args.commit, "op": args.op},
        "why": args.why,
        "branch": args.branch,
        "status": "proposed",
    }
    if args.worktree:
        change["worktree"] = str(root)
    if args.bind_task:
        change["what"]["bind_task"] = args.bind_task
    if args.revert_of:
        change["revert_of"] = args.revert_of

    costs_path = REPO_ROOT / "config" / "backtest.yaml"
    costs = yaml.safe_load(costs_path.read_text(encoding="utf-8"))["costs"] \
        if costs_path.exists() else {"fee_bps": 0.0, "slippage_bps": 0.0}

    bt = _json_arg(args.backtest_json)
    if bt is not None:
        wf_raw = _json_arg(args.walkforward_json) or {}
        oos = [w.get("profit_total_pct") or 0 for w in (wf_raw.get("windows") or [])]
        change["backtest"] = {
            "timerange": bt.get("timerange", "unknown"),
            "years": bt.get("years", 0),
            "fee_bps": costs["fee_bps"],
            "slippage_bps": costs["slippage_bps"],
            "baseline": bt.get("baseline", {}),
            "candidate": bt.get("candidate", {}),
        }
        change["walk_forward"] = {
            "windows": len(wf_raw.get("windows") or []),
            "scheme": "expanding",
            "in_sample_delta": bt.get("in_sample_delta", 0.0),
            "out_sample_delta": bt.get("out_sample_delta",
                                       sum(oos) / len(oos) if oos else 0.0),
            "pass": bool(bt.get("walk_forward_pass", True)),
        }

    if args.replay_id:
        with db.opened(REPO_ROOT / cfg.paths.journal_db, readonly=True) as jdb:
            row = jdb.execute("SELECT passed, scores_json, days FROM replay_runs"
                              " WHERE replay_id=?", (args.replay_id,)).fetchone()
        if row is None:
            raise SystemExit(f"replay {args.replay_id} not found — run evals.replay first")
        scores = json.loads(row["scores_json"] or "{}")
        change["replay"] = {"replay_id": args.replay_id, "days": row["days"],
                            "scores": scores.get("scores") or {},
                            "baseline_scores": scores.get("baseline") or {},
                            "pass": bool(row["passed"])}

    cf = _json_arg(args.counterfactual_json)
    if cf is not None:
        change["counterfactual"] = cf
    bounds = _json_arg(args.bounds_json)
    if bounds is not None:
        change["bounds_check"] = bounds
    skill_ev = _json_arg(args.skill_evidence_json)
    if skill_ev is not None:
        change["skill_evidence"] = skill_ev
    return change


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True)
    ap.add_argument("--kind", required=True, choices=["params", "prompt", "skill", "model"])
    ap.add_argument("--op", default="edit",
                    choices=["edit", "create", "delete", "bind", "revert"])
    ap.add_argument("--target", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--why", required=True)
    ap.add_argument("--author-run-id", required=True)
    ap.add_argument("--author-model", required=True,
                    help="the model that ACTUALLY served this run (runs.served_model)")
    ap.add_argument("--prompt-version", default="review.v2")
    ap.add_argument("--commit", required=True, help="the one commit this change ships")
    ap.add_argument("--branch", required=True)
    ap.add_argument("--worktree", default=None)
    ap.add_argument("--bind-task", default=None, help="op=bind: the skills.bindings key")
    ap.add_argument("--revert-of", default=None, help="op=revert: the change_id undone")
    ap.add_argument("--replay-id", default=None)
    ap.add_argument("--counterfactual-json", default=None)
    ap.add_argument("--backtest-json", default=None)
    ap.add_argument("--walkforward-json",
                    default=str(REPO_ROOT / "reports" / "backtests" / "walkforward.json"))
    ap.add_argument("--bounds-json", default=None)
    ap.add_argument("--skill-evidence-json", default=None)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = load_config()
    change = build_change(args, cfg)
    jsonschema.validate(change, CHANGE_SCHEMA)
    out = REPO_ROOT / "changes" / f"{args.id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
