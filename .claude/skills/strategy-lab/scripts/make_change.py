"""Assemble + schema-validate a changes/<id>.json. Computed fields come ONLY from
script artifacts (backtest results, walkforward.json, replay_runs row, counterfactual
replay output) — never from model prose."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import jsonschema  # noqa: E402
import yaml  # noqa: E402

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402

CHANGE_SCHEMA = json.loads((REPO_ROOT / "schemas" / "change.json").read_text())


def head_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--kind", required=True, choices=["params", "prompt", "skill", "model"])
    ap.add_argument("--target", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--why", required=True)
    ap.add_argument("--author-run-id", required=True)
    ap.add_argument("--author-model", required=True)
    ap.add_argument("--prompt-version", default="review.v1")
    ap.add_argument("--commit", default=None, help="default: HEAD")
    ap.add_argument("--replay-id", required=True)
    ap.add_argument("--counterfactual-json", required=True,
                    help='{"week":"2026-W39","decisions_changed":N,"process_grade_delta":D}')
    ap.add_argument("--backtest-json", required=True,
                    help="baseline/candidate metrics JSON file from the backtest step")
    ap.add_argument("--bounds-json", default=None,
                    help="params only: the bounds_check array JSON")
    ap.add_argument("--branch", required=True)
    args = ap.parse_args()

    cfg = load_config()
    costs = yaml.safe_load((REPO_ROOT / "config" / "backtest.yaml").read_text())["costs"]
    bt = json.loads(Path(args.backtest_json).read_text())
    wf_path = REPO_ROOT / "reports" / "backtests" / "walkforward.json"
    wf_raw = json.loads(wf_path.read_text())
    oos = [w["profit_total_pct"] or 0 for w in wf_raw["windows"]]
    wf = {
        "windows": len(wf_raw["windows"]),
        "scheme": "expanding",
        "in_sample_delta": bt.get("in_sample_delta", 0.0),
        "out_sample_delta": bt.get("out_sample_delta", sum(oos) / max(len(oos), 1)),
        "pass": bt.get("walk_forward_pass", True),
    }
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as jdb:
        row = jdb.execute("SELECT passed, scores_json, days FROM replay_runs"
                          " WHERE replay_id=?", (args.replay_id,)).fetchone()
    if row is None:
        print(f"replay {args.replay_id} not found — run evals.replay first",
              file=sys.stderr)
        return 1
    scores = json.loads(row["scores_json"])
    change = {
        "id": args.id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author_run_id": args.author_run_id,
        "author_model": args.author_model,
        "prompt_version": args.prompt_version,
        "tier": 1,
        "kind": args.kind,
        "target": args.target,
        "what": {"summary": args.summary, "commit": args.commit or head_commit()},
        "why": args.why,
        "backtest": {
            "timerange": bt.get("timerange", "unknown"),
            "years": bt.get("years", 0),
            "fee_bps": costs["fee_bps"],
            "slippage_bps": costs["slippage_bps"],
            "baseline": bt.get("baseline", {}),
            "candidate": bt.get("candidate", {}),
        },
        "walk_forward": wf,
        "replay": {"replay_id": args.replay_id, "days": row["days"],
                   "scores": scores.get("scores") or {},
                   "baseline_scores": scores.get("baseline") or {},
                   "pass": bool(row["passed"])},
        "counterfactual": json.loads(Path(args.counterfactual_json).read_text())
        if Path(args.counterfactual_json).exists()
        else json.loads(args.counterfactual_json),
        "branch": args.branch,
        "status": "proposed",
    }
    if args.bounds_json:
        change["bounds_check"] = json.loads(Path(args.bounds_json).read_text()) \
            if Path(args.bounds_json).exists() else json.loads(args.bounds_json)
    jsonschema.validate(change, CHANGE_SCHEMA)
    out = REPO_ROOT / "changes" / f"{args.id}.json"
    out.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
