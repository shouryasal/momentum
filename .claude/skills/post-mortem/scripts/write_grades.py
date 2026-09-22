"""Validate the model's grading JSON (schemas/grading.json) and write
decision_grades + root_cause_events rows — the model NEVER writes the DB directly.
Rubric weights are applied here (code), not by the model."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import jsonschema  # noqa: E402

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402

GRADING_SCHEMA = json.loads((REPO_ROOT / "schemas" / "grading.json").read_text())

# rubric weights (sum 100): the score is COMPUTED from the booleans
WEIGHTS = {
    "thesis_consistent": 25,
    "invalidation_stated_respected": 20,
    "checklist_completed": 15,
    "flags_honored": 15,
    "abstain_when_stale": 15,
    "lessons_applied": 10,
}


def rubric_score(rubric: dict) -> int:
    return sum(w for k, w in WEIGHTS.items() if rubric.get(k))


def write(jdb, grading: dict, grader_model: str, grader_run_id: str,
          now: datetime, outcomes: dict[str, dict] | None = None) -> int:
    jsonschema.validate(grading, GRADING_SCHEMA)
    week = grading["review_week"]
    n = 0
    for g in grading["grades"]:
        computed = rubric_score(g["rubric"])
        if abs(computed - g["process_grade"]) > 5:
            raise ValueError(
                f"{g['run_id']}: stated grade {g['process_grade']} != rubric score"
                f" {computed} (+/-5) — the score is computed, not asserted")
        o = (outcomes or {}).get(g["run_id"]) or {}
        jdb.execute(
            "INSERT OR REPLACE INTO decision_grades(run_id, graded_at, review_week,"
            " process_grade, process_rubric_json, outcome_vs_rules_bps,"
            " outcome_vs_btc_bps, outcome_resolved_at, outcome_grade, grader_model,"
            " grader_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (g["run_id"], now.strftime("%Y-%m-%dT%H:%M:%SZ"), week, computed,
             json.dumps(g["rubric"]), o.get("vs_rules_bps"), o.get("vs_btc_bps"),
             o.get("resolved_at"), o.get("outcome_grade"), grader_model,
             grader_run_id))
        n += 1
    for rc in grading["root_causes"]:
        jdb.execute(
            "INSERT OR REPLACE INTO root_cause_events(event_id, review_week, kind,"
            " ref, cause, recurrence_key, fix_path, learn_eligible, eligibility_rule,"
            " evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rc["event_id"], week, rc["kind"], rc.get("ref"), rc["cause"],
             rc["recurrence_key"], rc["fix_path"], int(rc["learn_eligible"]),
             rc["eligibility_rule"], json.dumps(rc["evidence"])))
    jdb.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("grading_json", help="path to the model's grading output")
    ap.add_argument("--grader-model", required=True)
    ap.add_argument("--grader-run-id", required=True)
    ap.add_argument("--outcomes-json", default=None)
    args = ap.parse_args()
    cfg = load_config()
    grading = json.loads(Path(args.grading_json).read_text())
    outcomes = None
    if args.outcomes_json:
        data = json.loads(Path(args.outcomes_json).read_text())
        outcomes = {o["run_id"]: o for o in data.get("per_decision", [])}
    with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb:
        n = write(jdb, grading, args.grader_model, args.grader_run_id,
                  datetime.now(UTC), outcomes)
    print(f"wrote {n} decision grades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
