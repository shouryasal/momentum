"""A make_change-shaped proposal validates against schemas/change.json."""

import json
import sys
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

SCHEMA = json.loads((REPO_ROOT / "schemas" / "change.json").read_text())

EXAMPLE = {
    "id": "2026-09-27-vol-target-down",
    "created_at": "2026-09-27T16:30:00Z",
    "author_run_id": "review-2026-W39",
    "author_model": "claude-fable-5-1",
    "prompt_version": "review.v1",
    "tier": 1,
    "kind": "params",
    "target": "config/params-sleeve-a.json",
    "what": {"summary": "vol target 0.30 -> 0.25", "commit": "deadbeef"},
    "why": "3 repeated reasoning root causes on high-vol whipsaw (events e1,e2,e3)",
    "bounds_check": [{"param": "sleeve_a.vol.target_annual", "old": 0.30, "new": 0.25,
                      "min": 0.10, "max": 0.50, "max_step": 0.05, "ok": True}],
    "backtest": {"timerange": "20240901-20260901", "years": 2.0, "fee_bps": 10.0,
                 "slippage_bps": 5.0, "baseline": {"ret": 40.0}, "candidate": {"ret": 43.0}},
    "walk_forward": {"windows": 8, "scheme": "expanding", "in_sample_delta": 2.0,
                     "out_sample_delta": 1.5, "pass": True},
    "replay": {"replay_id": "rp-x", "days": 30, "scores": {}, "baseline_scores": {},
               "pass": True},
    "counterfactual": {"week": "2026-W39", "decisions_changed": 2,
                       "process_grade_delta": 4.0},
    "branch": "review/2026-W39",
    "status": "proposed",
}


def test_example_validates():
    jsonschema.validate(EXAMPLE, SCHEMA)


def test_three_params_rejected():
    bad = json.loads(json.dumps(EXAMPLE))
    bad["bounds_check"] = bad["bounds_check"] * 3
    try:
        jsonschema.validate(bad, SCHEMA)
        raise AssertionError("should reject >2 bounds entries")
    except jsonschema.ValidationError:
        pass
