"""Build the P&L-FREE grading pack for the week's decisions (process grading comes
first; outcome numbers live in outcome_stats.py and are opened only afterwards)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402

FORBIDDEN_KEYS = ("pnl", "profit", "nav", "outcome")


def build_pack(jdb, week_start: datetime, week_end: datetime) -> dict:
    proposals = [dict(r) for r in jdb.execute(
        "SELECT run_id, shadow, module, targets_json, exposure_scale, confidence,"
        " abstain, horizon_days, rationale_json, invalidation, valid, invalid_reason,"
        " hard_case_flags_json, consumed_status FROM proposals"
        " WHERE ts_utc >= ? AND ts_utc < ? AND shadow = 0",
        (week_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
         week_end.strftime("%Y-%m-%dT%H:%M:%SZ")))]
    gate = [dict(r) for r in jdb.execute(
        "SELECT ts_utc, sleeve, pair, intent, allowed, reason, severity"
        " FROM gate_decisions WHERE ts_utc >= ? AND ts_utc < ?"
        " AND (allowed = 0 OR severity = 'breach')",
        (week_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
         week_end.strftime("%Y-%m-%dT%H:%M:%SZ")))]
    runs = [dict(r) for r in jdb.execute(
        "SELECT run_id, stage, status, escalated, escalation_reasons, error"
        " FROM runs WHERE started_utc >= ? AND started_utc < ? AND kind='research'",
        (week_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
         week_end.strftime("%Y-%m-%dT%H:%M:%SZ")))]
    pack = {
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": week_end.strftime("%Y-%m-%d"),
        "decisions": proposals,
        "gate_events": gate,
        "run_events": runs,
        "snapshots_dir": "journal/snapshots/",
    }
    text = json.dumps(pack).lower()
    for key in FORBIDDEN_KEYS:
        assert f'"{key}' not in text, f"grading pack must be P&L-free (found {key})"
    return pack


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--day", help="grade ONE Gulf day (YYYY-MM-DD) instead of a week")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cfg = load_config()
    if args.day:
        # Gulf day D = [D 00:00+04, D+1 00:00+04) = [D-1 20:00Z, D 20:00Z)
        start = (datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=UTC)
                 - timedelta(hours=4))
        end = start + timedelta(days=1)
    else:
        end = (datetime.strptime(args.week_end, "%Y-%m-%d").replace(tzinfo=UTC)
               if args.week_end else datetime.now(UTC))
        start = end - timedelta(days=7)
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as jdb:
        pack = build_pack(jdb, start, end)
    out = json.dumps(pack, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out + "\n")
        print(f"wrote {args.out}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
