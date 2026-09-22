"""Render reports/tca-weekly.md from the journal TCA tables + the freeze verdict."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402
from ops.lib import flags as flagslib  # noqa: E402


def render(jdb, cfg, now: datetime) -> str:
    costs = yaml.safe_load((REPO_ROOT / "config" / "backtest.yaml").read_text())["costs"]
    lines = [f"# TCA weekly — {now.strftime('%Y-%m-%d')}", "",
             f"Assumptions (backtest.yaml): fee {costs['fee_bps']} bps + slippage"
             f" {costs['slippage_bps']} bps"
             f" (measured month: {costs.get('measured_month') or 'not yet calibrated'})", "",
             "| sleeve | window | fills | fee med | slip med | total med |",
             "|---|---|---|---|---|---|"]
    rows = jdb.execute("SELECT * FROM tca_rolling WHERE day ="
                       " (SELECT MAX(day) FROM tca_rolling) ORDER BY sleeve, window")
    for r in rows:
        lines.append(f"| {r['sleeve']} | {r['window']} | {r['n_fills']} |"
                     f" {r['fee_bps_med'] or 0:.1f} | {r['slip_bps_med'] or 0:.1f} |"
                     f" {r['total_bps_med'] or 0:.1f} |")
    unrec = jdb.execute("SELECT COUNT(*) AS n FROM tca_fill_costs"
                        " WHERE status='unreconciled'").fetchone()["n"]
    lines += ["", f"Unreconciled fills (no quote within {cfg.tca.quote_fallback_max_s}s): {unrec}"]
    frozen = flagslib.tier1_frozen(REPO_ROOT / cfg.paths.flags_file, now)
    lines += ["", f"**Freeze verdict**: {'FROZEN — tier-1 parameter changes held' if frozen else 'clear'}"]
    breach = jdb.execute("SELECT value FROM tca_state WHERE key='breach_since'").fetchone()
    if breach:
        lines.append(f"Cost breach running since {breach['value']}"
                     f" (freeze at {cfg.tca.freeze_days} days).")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/tca-weekly.md")
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
