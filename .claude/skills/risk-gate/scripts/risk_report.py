"""Render reports/risk-weekly.md; every limit printed is read from earn.yaml LIVE
(anti-drift by construction) and cross-checked against the generated riskgate.json."""

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


def render(jdb, cfg, now: datetime) -> str:
    # anti-drift: the generated gate config must value-match earn.yaml right now
    rg = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    assert rg["risk"]["max_gross_exposure"] == cfg.risk.max_gross_exposure
    assert rg["risk"]["max_weight"] == cfg.risk.max_weight
    assert rg["risk"]["daily_loss_stop"] == cfg.risk.daily_loss_stop

    since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = cfg.risk
    lines = [f"# Risk weekly — {now.strftime('%Y-%m-%d')}", "",
             "| control | limit |", "|---|---|",
             f"| max weight | BTC {r.max_weight['BTC']}, others {r.max_weight['default']} |",
             f"| gross exposure / USDT floor | {r.max_gross_exposure} / {r.usdt_floor} |",
             f"| daily / monthly stop | -{r.daily_loss_stop:.0%} (lock {r.daily_stop_lock_hours}h)"
             f" / -{r.monthly_loss_stop:.0%} (human resume) |",
             f"| trades per day | {r.max_trades_per_day} |",
             f"| stoploss guard / cooldown | {r.stoploss_guard.count} in"
             f" {r.stoploss_guard.window_hours}h -> {r.stoploss_guard.lock_hours}h lock /"
             f" {r.cooldown_candles} candles |",
             f"| staleness / min notional | {r.staleness_minutes} min /"
             f" {r.min_notional_usdt} USDT |",
             "", "## Rejections this week (by rule)", "",
             "| rule | count | severity |", "|---|---|---|"]
    rows = jdb.execute(
        "SELECT reason, severity, COUNT(*) AS n FROM gate_decisions"
        " WHERE allowed=0 AND ts_utc >= ? GROUP BY reason, severity ORDER BY n DESC",
        (since,))
    any_row = False
    for row in rows:
        any_row = True
        lines.append(f"| {row['reason']} | {row['n']} | {row['severity']} |")
    if not any_row:
        lines.append("| (none) | 0 | — |")
    breaches = jdb.execute(
        "SELECT COUNT(*) AS n FROM gate_decisions WHERE severity='breach'"
        " AND ts_utc >= ?", (since,)).fetchone()["n"]
    locks = jdb.execute("SELECT sleeve, key, value FROM risk_state WHERE key IN"
                        " ('monthly_locked','locked_until')").fetchall()
    lines += ["", f"Breaches (must be zero for G4): **{breaches}**"]
    for lk in locks:
        lines.append(f"Active lock: sleeve {lk['sleeve']} {lk['key']}={lk['value']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/risk-weekly.md")
    args = ap.parse_args()
    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as jdb:
        text = render(jdb, cfg, datetime.now(UTC))
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
