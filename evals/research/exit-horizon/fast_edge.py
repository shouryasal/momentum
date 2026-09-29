"""MEASURE 3 part three — the per-trade gross edge of the shipped fast (1h) entry rule.

Research only. Reads the feather candle store read-only.

The question is the cost-floor question, not a Sharpe question: a round trip costs 0.30%
of the position (10 bps fee + 5 bps slippage per side) and the gate's ``risk.min_edge``
refuses a plan whose smallest booked target does not clear 3x that (0.90%). So for the
shipped SleeveFast rule at 1h — EMA6 > EMA18 AND close > prior 3-bar high AND
0.15% <= ATR14/close <= 7.0% — this measures the MEAN GROSS return per episode at each
holding period, and prints it against those two floors.

Non-overlapping episodes only: after an entry the rule cannot fire again until the hold is
over, which is what a real book does (one position per pair).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROUND_TRIP = 0.0030
MIN_EDGE = 0.0090          # risk.min_edge: 3 x the round trip
HOLD_HOURS = (1, 2, 4, 6, 8, 12, 24, 48, 96, 240, 480)


def _repo_on_path() -> None:
    for p in Path(__file__).resolve().parents:
        if (p / "runs" / "features" / "trend.py").exists():
            sys.path.insert(0, str(p))
            return
    raise SystemExit("repo root not found")


_repo_on_path()


def fast_signal(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    ema_f = c.ewm(span=6, adjust=False, min_periods=6).mean()
    ema_s = c.ewm(span=18, adjust=False, min_periods=18).mean()
    pc = c.shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    atr_pct = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / c
    brk = df["high"].rolling(3, min_periods=3).max().shift(1)
    return ((ema_f > ema_s) & (c > brk) & (atr_pct >= 0.0015)
            & (atr_pct <= 0.070)).fillna(False)


def episodes(close: np.ndarray, sig: np.ndarray, bars: int) -> np.ndarray:
    """Gross returns of non-overlapping hold-``bars`` episodes."""
    out, i, n = [], 0, len(close)
    while i < n - bars:
        if sig[i] and close[i] > 0 and np.isfinite(close[i + bars]):
            out.append(close[i + bars] / close[i] - 1.0)
            i += bars
        else:
            i += 1
    return np.asarray(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="/home/shourya/earn-run/data/binance")
    ap.add_argument("--out", default="fast_edge.json")
    a = ap.parse_args()
    root = Path(a.data_root)

    files = sorted(root.glob("*_USDT-1h.feather"))
    groups = {"core (BTC, ETH)": [f for f in files if f.name.split("_")[0] in ("BTC", "ETH")],
              "all 1h pairs in the store": files}
    res: dict = {"round_trip_pct": ROUND_TRIP * 100, "min_edge_pct": MIN_EDGE * 100,
                 "groups": {}}
    for gname, fs in groups.items():
        loaded = []
        for f in fs:
            df = pd.read_feather(f)
            col = "date" if "date" in df.columns else df.columns[0]
            df = df.rename(columns={col: "date"}).sort_values("date").set_index("date")
            if len(df) < 500:
                continue
            loaded.append((f.name.split("_")[0], df[["high", "low", "close"]].astype(float)))
        per = {}
        for hh in HOLD_HOURS:
            allr = []
            for _, df in loaded:
                s = fast_signal(df).to_numpy()
                allr.append(episodes(df["close"].to_numpy(), s, hh))
            r = np.concatenate([x for x in allr if len(x)])
            if len(r) < 50:
                continue
            per[str(hh)] = {
                "trades": int(len(r)),
                "gross_mean_pct": round(float(r.mean()) * 100, 4),
                "gross_median_pct": round(float(np.median(r)) * 100, 4),
                "net_mean_pct": round((float(r.mean()) - ROUND_TRIP) * 100, 4),
                "win_rate_gross": round(float((r > 0).mean()), 4),
                "win_rate_net": round(float((r > ROUND_TRIP).mean()), 4),
                "x_of_round_trip": round(float(r.mean()) / ROUND_TRIP, 2),
                "x_of_min_edge": round(float(r.mean()) / MIN_EDGE, 2),
                "edge_shortfall_pct": round((ROUND_TRIP - float(r.mean())) * 100, 4),
            }
        res["groups"][gname] = {"pairs": len(loaded), "by_hold": per}

    Path(a.out).write_text(json.dumps(res, indent=1))
    for gname, g in res["groups"].items():
        print(f"\n== shipped 1h fast rule, {gname} ({g['pairs']} pairs)")
        print("  need 0.300% gross to break even, 0.900% to clear the gate's min_edge")
        print("  hold |  trades |  gross% | median% |    net% | win(g) | win(net) | x cost | x min_edge")
        for hh, s in g["by_hold"].items():
            lab = f"{hh}h" if int(hh) < 24 else f"{int(hh)//24}d"
            print(f"{lab:>6} | {s['trades']:>7} | {s['gross_mean_pct']:>7} | "
                  f"{s['gross_median_pct']:>7} | {s['net_mean_pct']:>7} | "
                  f"{s['win_rate_gross']:>6} | {s['win_rate_net']:>8} | "
                  f"{s['x_of_round_trip']:>6} | {s['x_of_min_edge']:>10}")


if __name__ == "__main__":
    main()
