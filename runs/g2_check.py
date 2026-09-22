"""G2 gate check: Sleeve A's ≥2-year backtest vs buy-and-hold BTC.

Thresholds (spec §10): max drawdown better than holding BTC; net return >= 60% of
BTC's; modeled costs < 1% of NAV per month. Writes reports/backtests/g2_summary.json
consumed by excel_view's Gates sheet and the review run.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ops.check_gaps import load_candles
from ops.config import REPO_ROOT, load_config

WALKFORWARD = REPO_ROOT / "reports" / "backtests" / "walkforward.json"
OUT = REPO_ROOT / "reports" / "backtests" / "g2_summary.json"


def btc_hold_metrics(candles_1d: pd.DataFrame, start: str, end: str) -> dict:
    """Buy-and-hold return and max drawdown over [start, end] from 1d closes."""
    df = candles_1d[(candles_1d["date"] >= pd.Timestamp(start, tz="UTC"))
                    & (candles_1d["date"] <= pd.Timestamp(end, tz="UTC"))]
    if df.empty:
        raise ValueError(f"no candles in {start}..{end}")
    closes = df["close"].reset_index(drop=True)
    ret_pct = (closes.iloc[-1] / closes.iloc[0] - 1) * 100
    running_max = closes.cummax()
    dd_pct = ((closes / running_max - 1).min()) * -100
    return {"return_pct": float(ret_pct), "max_drawdown_pct": float(dd_pct)}


def evaluate(sleeve: dict, btc: dict, cost_pct_month: float | None) -> dict:
    checks = {
        "dd_better_than_btc": sleeve["max_drawdown_pct"] < btc["max_drawdown_pct"],
        "return_ratio_ok": (
            sleeve["profit_total_pct"] >= 0.6 * btc["return_pct"]
            if btc["return_pct"] > 0 else sleeve["profit_total_pct"] >= btc["return_pct"]
        ),
        "cost_under_1pct_month": (cost_pct_month is not None and cost_pct_month < 1.0),
    }
    return {"checks": checks, "pass": all(checks.values())}


def summarize(walkforward: dict, candles_1d: pd.DataFrame) -> dict:
    ws = walkforward["windows"]
    if not ws:
        raise ValueError("walkforward.json has no windows")
    start, end = ws[0]["start"], ws[-1]["end"]
    # Full-period sleeve metrics: compound the OOS windows.
    compounded = 1.0
    worst_dd = 0.0
    trades = 0
    fees = 0.0
    for w in ws:
        compounded *= 1 + (w["profit_total_pct"] or 0) / 100
        worst_dd = max(worst_dd, w["max_drawdown_pct"] or 0)
        trades += w["trades"] or 0
        fees += w.get("fees_paid") or 0
    sleeve = {"profit_total_pct": (compounded - 1) * 100, "max_drawdown_pct": worst_dd,
              "trades": trades}
    months = max(len(ws) * walkforward.get("oos_months", 6), 1)
    # Cost proxy: per-side fee (incl. modeled slippage) x turnover; freqtrade's own
    # fee accounting lands in fees_paid when exported.
    cost_pct_month = (fees / months) if fees else None
    btc = btc_hold_metrics(candles_1d, start, end)
    verdict = evaluate(sleeve, btc, cost_pct_month)
    return {
        "period": {"start": start, "end": end},
        "sleeve_a": sleeve,
        "btc_hold": btc,
        "cost_pct_month": cost_pct_month,
        **verdict,
    }


def main() -> int:
    cfg = load_config()
    wf = json.loads(WALKFORWARD.read_text())
    candles = load_candles(REPO_ROOT / cfg.paths.data_dir, cfg.exchange.name,
                           cfg.sleeves.benchmark.pair, "1d")
    summary = summarize(wf, candles)
    summary["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["checks"], indent=2))
    print(f"G2 {'PASS' if summary['pass'] else 'FAIL'} -> {OUT}")
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
