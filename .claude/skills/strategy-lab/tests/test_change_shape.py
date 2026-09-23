"""A make_change-shaped proposal validates against schemas/change.json (v2).

The v2 schema deliberately stops *requiring* the evidence blocks: they are claims now, and
``evals/verify_change.py`` recomputes everything before the autonomy matrix is consulted.
What the schema still pins down is identity, scope and the op.
"""

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
    "prompt_version": "review.v2",
    "tier": 1,
    "kind": "params",
    "target": "config/params-sleeve-a.json",
    "what": {"summary": "vol target 0.30 -> 0.25", "commit": "deadbeef1234",
             "op": "edit"},
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
    "worktree": "/home/user/earn-worktrees/review-2026-W39",
    "status": "proposed",
}


def _copy(**over):
    change = json.loads(json.dumps(EXAMPLE))
    change.update(over)
    return change


def test_example_validates():
    jsonschema.validate(EXAMPLE, SCHEMA)


def test_three_params_rejected():
    bad = _copy(bounds_check=EXAMPLE["bounds_check"] * 3)
    try:
        jsonschema.validate(bad, SCHEMA)
        raise AssertionError("should reject >2 bounds entries")
    except jsonschema.ValidationError:
        pass


def test_a_change_without_claimed_evidence_still_validates():
    """The evidence blocks are claims; verify_change recomputes them either way."""
    lean = _copy()
    for key in ("bounds_check", "backtest", "walk_forward", "replay", "counterfactual"):
        lean.pop(key)
    jsonschema.validate(lean, SCHEMA)


def test_every_op_is_accepted():
    for op in ("edit", "create", "delete", "bind", "revert"):
        change = _copy()
        change["what"]["op"] = op
        jsonschema.validate(change, SCHEMA)


def test_an_unknown_op_is_rejected():
    change = _copy()
    change["what"]["op"] = "merge"
    try:
        jsonschema.validate(change, SCHEMA)
        raise AssertionError("should reject an unknown op")
    except jsonschema.ValidationError:
        pass


def test_a_skill_bind_carries_its_task():
    change = _copy(kind="skill", target=".claude/skills/trade-forensics")
    change["what"] = {"summary": "bind to the review task", "commit": "deadbeef1234",
                      "op": "bind", "bind_task": "review"}
    jsonschema.validate(change, SCHEMA)


def test_an_empty_commit_is_rejected():
    change = _copy()
    change["what"]["commit"] = ""
    try:
        jsonschema.validate(change, SCHEMA)
        raise AssertionError("a change must name the commit it ships")
    except jsonschema.ValidationError:
        pass


def test_the_v2_statuses_are_accepted():
    for status in ("proposed", "verifying", "auto_merged", "approved", "rejected",
                   "held", "reverted", "superseded"):
        jsonschema.validate(_copy(status=status), SCHEMA)
