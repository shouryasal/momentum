"""Per-asset statistics computed from the candle archive — the numbers the
asset dossiers cite. Pure code: no model, no estimates, stdlib only.

Writes knowledge/assets/<ASSET>-stats.json with:
  top_drawdowns      the 5 deepest close-to-close drawdowns (peak/trough/depth/recovery)
  vol_percentiles    30d realized vol (annualized) p25/p50/p75/p90 + current + rank
  seasonality        mean daily return (%) by calendar month
  correlation        90d daily-return correlation with the OTHER universe asset
  current            last close, drawdown from ATH, data coverage
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402


def daily_closes(kdb, pair: str) -> list[tuple[str, float]]:
    return [(datetime.fromtimestamp(r["open_time"] / 1000, tz=UTC).strftime("%Y-%m-%d"),
             r["close"]) for r in kdb.execute(
        "SELECT open_time, close FROM candles WHERE pair=? AND tf='1d'"
        " AND is_closed=1 ORDER BY open_time", (pair,))]


def returns(closes: list[float]) -> list[float]:
    return [c / p - 1 for p, c in zip(closes, closes[1:], strict=False) if p]


def top_drawdowns(dates: list[str], closes: list[float], n: int = 5) -> list[dict]:
    episodes = []
    peak, peak_i = closes[0], 0
    trough, trough_i = closes[0], 0
    for i, c in enumerate(closes):
        if c >= peak:
            if trough < peak:  # a completed episode recovered at i
                episodes.append({
                    "peak_date": dates[peak_i], "trough_date": dates[trough_i],
                    "depth_pct": round((trough / peak - 1) * 100, 2),
                    "recovery_days": i - peak_i})
            peak, peak_i = c, i
            trough, trough_i = c, i
        elif c < trough:
            trough, trough_i = c, i
    if trough < peak:  # open episode, not yet recovered
        episodes.append({"peak_date": dates[peak_i], "trough_date": dates[trough_i],
                         "depth_pct": round((trough / peak - 1) * 100, 2),
                         "recovery_days": None})
    return sorted(episodes, key=lambda e: e["depth_pct"])[:n]


def rolling_vol(rets: list[float], window: int = 30) -> list[float]:
    out = []
    for i in range(window, len(rets) + 1):
        chunk = rets[i - window:i]
        out.append(statistics.pstdev(chunk) * math.sqrt(365) * 100)
    return out


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def rank_of(vals: list[float], x: float) -> float:
    if not vals:
        return 0.0
    return round(sum(1 for v in vals if v <= x) / len(vals), 3)


def seasonality(dates: list[str], rets: list[float]) -> dict[str, float]:
    by_month: dict[str, list[float]] = {}
    for d, r in zip(dates[1:], rets, strict=False):
        by_month.setdefault(d[5:7], []).append(r)
    return {m: round(statistics.mean(v) * 100, 3) for m, v in sorted(by_month.items())}


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b), 90)
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    try:
        return round(statistics.correlation(a, b), 3)
    except statistics.StatisticsError:
        return None


def compute(kdb, cfg, asset: str) -> dict | None:
    pair = f"{asset}/{cfg.universe.quote}"
    rows = daily_closes(kdb, pair)
    if len(rows) < 40:
        return None
    dates = [d for d, _ in rows]
    closes = [c for _, c in rows]
    rets = returns(closes)
    vols = rolling_vol(rets)
    vols_sorted = sorted(vols)
    cur_vol = vols[-1] if vols else 0.0
    others = [a for a in cfg.universe.assets if a != asset]
    corr = None
    if others:
        other_rows = daily_closes(kdb, f"{others[0]}/{cfg.universe.quote}")
        corr = correlation(rets, returns([c for _, c in other_rows]))
    ath = max(closes)
    return {
        "asset": asset,
        "computed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top_drawdowns": top_drawdowns(dates, closes),
        "vol_percentiles": {
            "p25": round(percentile(vols_sorted, 0.25), 2),
            "p50": round(percentile(vols_sorted, 0.50), 2),
            "p75": round(percentile(vols_sorted, 0.75), 2),
            "p90": round(percentile(vols_sorted, 0.90), 2),
            "current": round(cur_vol, 2),
            "current_rank": rank_of(vols_sorted, cur_vol),
        },
        "seasonality_mean_daily_pct": seasonality(dates, rets),
        f"correlation_90d_{others[0] if others else 'none'}": corr,
        "current": {
            "last_close": closes[-1], "last_date": dates[-1],
            "drawdown_from_ath_pct": round((closes[-1] / ath - 1) * 100, 2),
            "days_of_data": len(dates),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", help="single asset (default: whole universe)")
    args = ap.parse_args()
    cfg = load_config()
    out_dir = REPO_ROOT / "knowledge" / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = [args.asset] if args.asset else list(cfg.universe.assets)
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db, readonly=True) as kdb:
        for asset in assets:
            stats = compute(kdb, cfg, asset)
            if stats is None:
                print(f"{asset}: not enough daily candles — skipped", file=sys.stderr)
                continue
            p = out_dir / f"{asset}-stats.json"
            p.write_text(json.dumps(stats, indent=2) + "\n")
            print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
