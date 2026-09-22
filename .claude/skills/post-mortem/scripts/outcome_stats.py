"""Outcome grading, computed by CODE: each resolved decision vs the rules sleeve and
vs holding BTC over its stated horizon (nav_daily). Lesson-citable statistics are
REFUSED below the 30-decision floor (spec §7) — per-decision grades are still
returned for the grades table."""

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

MIN_DECISIONS_FOR_STATS = 30


def _nav(jdb, sleeve: str, date: str) -> float | None:
    row = jdb.execute("SELECT nav_usdt FROM nav_daily WHERE sleeve=? AND date_utc<=?"
                      " ORDER BY date_utc DESC LIMIT 1", (sleeve, date)).fetchone()
    return row["nav_usdt"] if row else None


def outcome_for(jdb, run_id: str, horizon_days: int,
                now: datetime) -> dict | None:
    """bps vs rules sleeve and vs benchmark over the horizon; None until resolved."""
    try:
        start_dt = datetime.fromisoformat(run_id).astimezone(UTC)
    except ValueError:
        return None
    end_dt = start_dt + timedelta(days=horizon_days)
    if end_dt > now:
        return None  # horizon not resolved yet
    d0, d1 = start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
    navs = {}
    for sleeve in ("a", "b", "benchmark"):
        n0, n1 = _nav(jdb, sleeve, d0), _nav(jdb, sleeve, d1)
        if not n0 or not n1:
            return None
        navs[sleeve] = n1 / n0 - 1
    vs_rules_bps = (navs["b"] - navs["a"]) * 1e4
    vs_btc_bps = (navs["b"] - navs["benchmark"]) * 1e4
    grade = "better" if vs_rules_bps > 10 else ("worse" if vs_rules_bps < -10 else "par")
    return {"run_id": run_id, "resolved_at": d1, "vs_rules_bps": round(vs_rules_bps, 1),
            "vs_btc_bps": round(vs_btc_bps, 1), "outcome_grade": grade,
            "beat_benchmark": vs_btc_bps > 0}


def build(jdb, now: datetime) -> dict:
    proposals = jdb.execute(
        "SELECT run_id, horizon_days, confidence FROM proposals"
        " WHERE shadow=0 AND valid=1 ORDER BY ts_utc").fetchall()
    outcomes = []
    for p in proposals:
        o = outcome_for(jdb, p["run_id"], p["horizon_days"] or 7, now)
        if o:
            o["confidence"] = p["confidence"]
            outcomes.append(o)
    n = len(outcomes)
    stats_citable = n >= MIN_DECISIONS_FOR_STATS
    result = {
        "resolved_decisions": n,
        "stats_citable": stats_citable,
        "per_decision": outcomes,
    }
    if stats_citable:
        beat_rules = sum(1 for o in outcomes if o["vs_rules_bps"] > 0)
        result["stats"] = {
            "n": n,
            "beat_rules_rate": round(beat_rules / n, 3),
            "beat_btc_rate": round(sum(1 for o in outcomes if o["vs_btc_bps"] > 0) / n, 3),
            "median_vs_rules_bps": sorted(o["vs_rules_bps"] for o in outcomes)[n // 2],
        }
    else:
        result["stats"] = None
        result["refusal"] = (f"outcome statistics need >= {MIN_DECISIONS_FOR_STATS}"
                             f" resolved decisions (have {n}); a lesson may NOT cite outcomes yet")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as jdb:
        result = build(jdb, datetime.now(UTC))
    out = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(out + "\n")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
